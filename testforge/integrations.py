import os
import time
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from testforge.models import Execution, ServiceInstance, utcnow


class IntegrationFailure(RuntimeError):
    pass


def _headers(config: dict[str, Any], idempotency_key: str) -> dict[str, str]:
    headers = {"Idempotency-Key": idempotency_key}
    secret_env = config.get("auth_header_env")
    if secret_env:
        secret = os.environ.get(secret_env)
        if not secret:
            raise IntegrationFailure(
                f"service credential environment variable is missing: {secret_env}"
            )
        headers["Authorization"] = secret
    return headers


def _request_with_retry(
    method: str,
    url: str,
    *,
    config: dict[str, Any],
    idempotency_key: str,
    payload: dict[str, Any],
) -> httpx.Response:
    error: Exception | None = None
    for attempt in range(config.get("retries", 2) + 1):
        try:
            response = httpx.request(
                method,
                url,
                json=payload,
                headers=_headers(config, idempotency_key),
                timeout=config.get("timeout_seconds", 30),
            )
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            error = exc
            if attempt < config.get("retries", 2):
                time.sleep(min(2**attempt, 5))
    raise IntegrationFailure(str(error))


def start_service_phase(db: Session, execution: Execution, phase: str) -> list[ServiceInstance]:
    started: list[ServiceInstance] = []
    for binding in execution.context.get("services", []):
        if binding["phase"] != phase:
            continue
        existing = db.scalar(
            select(ServiceInstance).where(
                ServiceInstance.execution_id == execution.id,
                ServiceInstance.binding_key == binding["binding_key"],
            )
        )
        if existing and existing.status == "HEALTHY":
            started.append(existing)
            continue
        instance = existing or ServiceInstance(
            execution_id=execution.id,
            service_release_id=binding["service_release_id"],
            binding_key=binding["binding_key"],
        )
        db.add(instance)
        config = binding["service"]["config"]
        payload = {
            "execution_id": execution.id,
            "binding_key": binding["binding_key"],
            "parameters": binding.get("parameters", {}),
        }
        try:
            response = _request_with_retry(
                "POST",
                config["create_url"],
                config=config,
                idempotency_key=f"{execution.id}:{binding['binding_key']}:create",
                payload=payload,
            )
            body = response.json() if response.content else {}
            instance.external_instance_id = str(body.get("instance_id", instance.id))
            instance.status = "STARTED"
            health_url = config.get("health_url")
            if health_url:
                health_url = health_url.format(
                    instance_id=quote(instance.external_instance_id, safe="")
                )
                _request_with_retry(
                    "GET",
                    health_url,
                    config=config,
                    idempotency_key=f"{execution.id}:{binding['binding_key']}:health",
                    payload=payload,
                )
            instance.status = "HEALTHY"
            instance.updated_at = utcnow()
            db.commit()
            started.append(instance)
        except (IntegrationFailure, ValueError) as exc:
            instance.status = "FAILED"
            instance.last_error = str(exc)[:2000]
            instance.updated_at = utcnow()
            db.commit()
            if binding.get("failure_policy", "abort") == "abort":
                raise IntegrationFailure(f"service {binding['binding_key']} failed: {exc}") from exc
    return started


def cleanup_services(db: Session, execution: Execution) -> dict[str, int]:
    instances = db.scalars(
        select(ServiceInstance)
        .where(ServiceInstance.execution_id == execution.id)
        .order_by(ServiceInstance.created_at.desc())
    ).all()
    cleaned = failed = 0
    bindings = {
        binding["binding_key"]: binding for binding in execution.context.get("services", [])
    }
    for instance in instances:
        if instance.cleanup_status == "COMPLETED":
            cleaned += 1
            continue
        binding = bindings.get(instance.binding_key)
        if not binding:
            instance.cleanup_status = "FAILED"
            instance.last_error = "service binding snapshot is missing"
            failed += 1
            continue
        config = binding["service"]["config"]
        cleanup_url = config["cleanup_url"].format(
            instance_id=quote(instance.external_instance_id or "", safe="")
        )
        try:
            _request_with_retry(
                "POST",
                cleanup_url,
                config=config,
                idempotency_key=f"{execution.id}:{instance.binding_key}:cleanup",
                payload={
                    "execution_id": execution.id,
                    "instance_id": instance.external_instance_id,
                    "binding_key": instance.binding_key,
                },
            )
            instance.cleanup_status = "COMPLETED"
            cleaned += 1
        except IntegrationFailure as exc:
            instance.cleanup_status = "FAILED"
            instance.last_error = str(exc)[:2000]
            failed += 1
        instance.updated_at = utcnow()
    db.commit()
    return {"cleaned": cleaned, "failed": failed}
