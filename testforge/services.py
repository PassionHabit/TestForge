import hashlib
import json
import secrets
from datetime import UTC, timedelta
from typing import Any

from jsonschema import ValidationError, validate
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from testforge.config import Settings
from testforge.models import (
    Agent,
    ArtifactBlob,
    ArtifactManifest,
    DeliveryOutbox,
    Execution,
    ExecutionEvent,
    ExecutionStatus,
    Lease,
    LeaseStatus,
    Project,
    ProjectRelease,
    PublicService,
    ResultKind,
    ServiceRelease,
    utcnow,
)
from testforge.schemas import (
    AgentRegistration,
    ExecutionCreate,
    FinalizeRequest,
    ProjectConfig,
    ServiceConfig,
)
from testforge.state import ensure_transition


class ConflictError(ValueError):
    pass


class InputValidationError(ValueError):
    pass


def canonical_hash(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()


def add_event(
    db: Session, execution: Execution, event_type: str, payload: dict | None = None
) -> None:
    db.add(ExecutionEvent(execution_id=execution.id, event_type=event_type, payload=payload or {}))


def transition(db: Session, execution: Execution, target: ExecutionStatus, **payload: Any) -> None:
    ensure_transition(execution.status, target)
    previous = execution.status
    execution.status = target
    execution.updated_at = utcnow()
    if target is ExecutionStatus.RUNNING and execution.started_at is None:
        execution.started_at = utcnow()
    if target in {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}:
        execution.finished_at = utcnow()
    add_event(
        db, execution, "STATE_CHANGED", {"from": previous.value, "to": target.value, **payload}
    )


def publish_project(db: Session, project: Project) -> ProjectRelease:
    config = ProjectConfig.model_validate(project.draft_config).model_dump(mode="json")
    for binding in config.get("services", []):
        if db.get(ServiceRelease, binding["service_release_id"]) is None:
            raise InputValidationError(
                f"public service release not found: {binding['service_release_id']}"
            )
    latest = db.scalar(
        select(func.max(ProjectRelease.version)).where(ProjectRelease.project_id == project.id)
    )
    release = ProjectRelease(
        project_id=project.id,
        version=(latest or 0) + 1,
        snapshot={
            "name": project.name,
            "repository_url": project.repository_url,
            "source_type": project.source_type,
            "framework": project.framework,
            "owner": project.owner,
            "concurrency_limit": project.concurrency_limit,
            "config": config,
        },
        snapshot_hash="",
    )
    release.snapshot_hash = canonical_hash(release.snapshot)
    db.add(release)
    db.flush()
    project.current_release_id = release.id
    project.updated_at = utcnow()
    db.commit()
    db.refresh(release)
    return release


def publish_service(db: Session, service: PublicService) -> ServiceRelease:
    config = ServiceConfig.model_validate(service.draft_config).model_dump(mode="json")
    latest = db.scalar(
        select(func.max(ServiceRelease.version)).where(ServiceRelease.service_id == service.id)
    )
    release = ServiceRelease(
        service_id=service.id,
        version=(latest or 0) + 1,
        snapshot={
            "name": service.name,
            "service_type": service.service_type,
            "owner": service.owner,
            "config": config,
        },
        snapshot_hash="",
    )
    release.snapshot_hash = canonical_hash(release.snapshot)
    db.add(release)
    db.flush()
    service.current_release_id = release.id
    service.updated_at = utcnow()
    db.commit()
    db.refresh(release)
    return release


def create_execution(db: Session, data: ExecutionCreate) -> Execution:
    existing = db.scalar(select(Execution).where(Execution.idempotency_key == data.idempotency_key))
    if existing:
        if existing.project_id != data.project_id:
            raise ConflictError("idempotency key already belongs to another project")
        return existing

    project = db.get(Project, data.project_id)
    if project is None:
        raise LookupError("project not found")
    release_id = data.release_id or project.current_release_id
    release = db.get(ProjectRelease, release_id) if release_id else None
    if release is None or release.project_id != project.id:
        raise LookupError("published project release not found")
    config = ProjectConfig.model_validate(release.snapshot["config"])
    try:
        validate(instance=data.parameters, schema=config.parameter_schema or {"type": "object"})
    except ValidationError as exc:
        raise InputValidationError(f"invalid execution parameters: {exc.message}") from exc
    resolved_services = []
    for binding in config.services:
        service_release = db.get(ServiceRelease, binding.service_release_id)
        if service_release is None:
            raise LookupError(f"public service release not found: {binding.service_release_id}")
        resolved_services.append(
            {
                **binding.model_dump(mode="json"),
                "service": service_release.snapshot,
                "service_snapshot_hash": service_release.snapshot_hash,
            }
        )
    context = {
        "project_release_version": release.version,
        "project_snapshot_hash": release.snapshot_hash,
        "repository_url": release.snapshot["repository_url"],
        "source_type": release.snapshot.get("source_type", "git"),
        "source_revision": data.source_revision,
        "image_digest": config.image_digest,
        "recipe": [step.model_dump(mode="json") for step in config.recipe],
        "artifact_paths": config.artifact_paths,
        "services": resolved_services,
        "execution_kind": config.execution_kind,
    }
    execution = Execution(
        project_id=project.id,
        release_id=release.id,
        idempotency_key=data.idempotency_key,
        status=ExecutionStatus.QUEUED,
        result_kind=ResultKind.NOT_EVALUATED,
        source_revision=data.source_revision,
        image_digest=config.image_digest,
        context=context,
        parameters=data.parameters,
        required_labels=config.required_labels,
        cpu_millis=config.cpu_millis,
        memory_mb=config.memory_mb,
        callback_url=data.callback_url,
    )
    db.add(execution)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ConflictError("duplicate idempotency key") from exc
    add_event(db, execution, "EXECUTION_CREATED")
    db.commit()
    db.refresh(execution)
    return execution


def register_agent(db: Session, data: AgentRegistration) -> Agent:
    agent = db.scalar(select(Agent).where(Agent.name == data.name))
    if agent is None:
        agent = Agent(name=data.name, **data.model_dump(exclude={"name"}))
        db.add(agent)
    else:
        for field, value in data.model_dump(exclude={"name"}).items():
            setattr(agent, field, value)
        agent.last_seen_at = utcnow()
    db.commit()
    db.refresh(agent)
    return agent


def labels_match(required: dict[str, str], offered: dict[str, str]) -> bool:
    return all(offered.get(key) == value for key, value in required.items())


def _active_count(db: Session, agent_id: str) -> int:
    return (
        db.scalar(
            select(func.count(Lease.id)).where(
                Lease.agent_id == agent_id,
                Lease.status.in_([LeaseStatus.OFFERED, LeaseStatus.CLAIMED]),
            )
        )
        or 0
    )


def _reserved_resources(db: Session, agent_id: str) -> tuple[int, int]:
    values = db.execute(
        select(
            func.coalesce(func.sum(Execution.cpu_millis), 0),
            func.coalesce(func.sum(Execution.memory_mb), 0),
        )
        .join(Lease, Lease.execution_id == Execution.id)
        .where(
            Lease.agent_id == agent_id,
            Lease.status.in_([LeaseStatus.OFFERED, LeaseStatus.CLAIMED]),
            Execution.status.in_(
                [
                    ExecutionStatus.PREPARING,
                    ExecutionStatus.ASSIGNED,
                    ExecutionStatus.RUNNING,
                    ExecutionStatus.FINALIZING,
                ]
            ),
        )
    ).one()
    return int(values[0]), int(values[1])


def _project_active_count(db: Session, project_id: str) -> int:
    return (
        db.scalar(
            select(func.count(Execution.id)).where(
                Execution.project_id == project_id,
                Execution.status.in_(
                    [
                        ExecutionStatus.PREPARING,
                        ExecutionStatus.ASSIGNED,
                        ExecutionStatus.RUNNING,
                        ExecutionStatus.FINALIZING,
                        ExecutionStatus.RECONCILING,
                    ]
                ),
            )
        )
        or 0
    )


def claim_work(
    db: Session, agent: Agent, settings: Settings
) -> tuple[Lease, str, Execution] | None:
    agent.last_seen_at = utcnow()
    if agent.maintenance or _active_count(db, agent.id) >= agent.max_concurrency:
        db.commit()
        return None

    used_cpu, used_memory = _reserved_resources(db, agent.id)
    available_cpu = agent.cpu_millis - used_cpu
    available_memory = agent.memory_mb - used_memory
    queued = db.scalars(
        select(Execution)
        .where(Execution.status == ExecutionStatus.QUEUED)
        .order_by(Execution.created_at)
        .with_for_update(skip_locked=True)
    ).all()
    selected = None
    for execution in queued:
        project = db.get(Project, execution.project_id)
        if (
            labels_match(execution.required_labels, agent.labels)
            and execution.cpu_millis <= available_cpu
            and execution.memory_mb <= available_memory
            and project is not None
            and _project_active_count(db, project.id) < project.concurrency_limit
        ):
            selected = execution
            break
    if selected is None:
        db.commit()
        return None

    transition(db, selected, ExecutionStatus.PREPARING)
    selected.lease_generation += 1
    token = secrets.token_urlsafe(32)
    lease = Lease(
        execution_id=selected.id,
        agent_id=agent.id,
        generation=selected.lease_generation,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        status=LeaseStatus.CLAIMED,
        claimed_at=utcnow(),
        heartbeat_at=utcnow(),
        expires_at=utcnow() + timedelta(seconds=settings.lease_seconds),
    )
    db.add(lease)
    db.flush()
    transition(
        db, selected, ExecutionStatus.ASSIGNED, agent_id=agent.id, generation=lease.generation
    )
    db.commit()
    db.refresh(lease)
    db.refresh(selected)
    return lease, token, selected


def verify_lease(db: Session, execution: Execution, token: str, generation: int) -> Lease:
    lease = db.scalar(
        select(Lease).where(
            Lease.execution_id == execution.id,
            Lease.generation == generation,
            Lease.status == LeaseStatus.CLAIMED,
        )
    )
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    if lease is None or not secrets.compare_digest(lease.token_hash, token_hash):
        raise PermissionError("invalid lease token or generation")
    expires_at = lease.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < utcnow():
        raise PermissionError("lease expired")
    return lease


def heartbeat_lease(db: Session, lease: Lease, settings: Settings) -> None:
    lease.heartbeat_at = utcnow()
    lease.expires_at = utcnow() + timedelta(seconds=settings.lease_seconds)
    db.commit()


def finalize_execution(
    db: Session, execution: Execution, lease: Lease, request: FinalizeRequest
) -> Execution:
    if execution.status is ExecutionStatus.RUNNING:
        transition(db, execution, ExecutionStatus.FINALIZING)
    if execution.status is not ExecutionStatus.FINALIZING:
        raise ConflictError("execution is not ready to finalize")

    items = [item.model_dump(mode="json") for item in request.artifacts]
    for item in items:
        if not (item.get("uri") or "").startswith("artifact://"):
            continue
        blob = db.scalar(
            select(ArtifactBlob).where(
                ArtifactBlob.execution_id == execution.id,
                ArtifactBlob.path == item["path"],
            )
        )
        if (
            blob is None
            or blob.sha256 != item["sha256"]
            or blob.size != item["size"]
            or blob.storage_uri != item["uri"]
        ):
            raise ConflictError(f"artifact upload does not match manifest: {item['path']}")
    manifest = ArtifactManifest(
        execution_id=execution.id,
        items=items,
        content_hash=canonical_hash(items),
    )
    db.add(manifest)
    execution.process_exit_code = request.process_exit_code
    execution.result_kind = request.result_kind
    execution.artifacts_sealed = True
    execution.cleanup_complete = request.cleanup_complete
    execution.delivery_complete = request.delivery_complete
    execution.failure_reason = request.failure_reason
    lease.status = LeaseStatus.RELEASED

    if execution.callback_url:
        execution.delivery_complete = False
        db.add(
            DeliveryOutbox(
                execution_id=execution.id,
                destination_url=execution.callback_url,
                payload={
                    "event": "execution.finished",
                    "execution_id": execution.id,
                    "status": execution.status.value,
                    "result_kind": execution.result_kind.value,
                    "process_exit_code": execution.process_exit_code,
                    "artifact_manifest_hash": manifest.content_hash,
                },
            )
        )
    else:
        if request.result_kind is ResultKind.CANCELLED or execution.cancel_requested:
            terminal = ExecutionStatus.CANCELLED
        elif (
            request.result_kind is ResultKind.PASSED
            and request.cleanup_complete
            and request.delivery_complete
        ):
            terminal = ExecutionStatus.COMPLETED
        else:
            terminal = ExecutionStatus.FAILED
        transition(db, execution, terminal)
    db.commit()
    db.refresh(execution)
    return execution


def reconcile_expired_leases(db: Session) -> int:
    leases = db.scalars(select(Lease).where(Lease.status == LeaseStatus.CLAIMED)).all()
    reconciled = 0
    for lease in leases:
        expires_at = lease.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at >= utcnow():
            continue
        execution = db.get(Execution, lease.execution_id)
        lease.status = LeaseStatus.EXPIRED
        if execution and execution.status in {ExecutionStatus.ASSIGNED, ExecutionStatus.RUNNING}:
            transition(db, execution, ExecutionStatus.RECONCILING, generation=lease.generation)
            reconciled += 1
    db.commit()
    return reconciled


def recover_observed_runtime(
    db: Session,
    agent: Agent,
    execution_id: str,
    observed_generation: int,
    settings: Settings,
) -> tuple[Lease, str, Execution] | None:
    execution = db.get(Execution, execution_id)
    if execution is None or execution.status is not ExecutionStatus.RECONCILING:
        return None
    previous = db.scalar(
        select(Lease).where(
            Lease.execution_id == execution_id,
            Lease.agent_id == agent.id,
            Lease.generation == observed_generation,
            Lease.status == LeaseStatus.EXPIRED,
        )
    )
    if previous is None or observed_generation != execution.lease_generation:
        return None
    execution.lease_generation += 1
    token = secrets.token_urlsafe(32)
    lease = Lease(
        execution_id=execution.id,
        agent_id=agent.id,
        generation=execution.lease_generation,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        status=LeaseStatus.CLAIMED,
        claimed_at=utcnow(),
        heartbeat_at=utcnow(),
        expires_at=utcnow() + timedelta(seconds=settings.lease_seconds),
    )
    db.add(lease)
    transition(
        db,
        execution,
        ExecutionStatus.RUNNING,
        recovered_from_generation=observed_generation,
        agent_id=agent.id,
    )
    db.commit()
    db.refresh(lease)
    db.refresh(execution)
    return lease, token, execution


def resolve_reconciliation(
    db: Session, execution: Execution, action: str, reason: str
) -> Execution:
    if execution.status is not ExecutionStatus.RECONCILING:
        raise ConflictError("execution is not reconciling")
    execution.failure_reason = reason
    if action == "requeue_unstarted":
        if execution.started_at is not None:
            raise ConflictError("a started execution cannot be automatically re-run")
        transition(db, execution, ExecutionStatus.QUEUED, resolution=action, reason=reason)
    elif action == "mark_cancelled":
        execution.result_kind = ResultKind.CANCELLED
        transition(db, execution, ExecutionStatus.CANCELLED, resolution=action, reason=reason)
    else:
        execution.result_kind = ResultKind.INFRA_FAILED
        transition(db, execution, ExecutionStatus.FAILED, resolution=action, reason=reason)
    db.commit()
    db.refresh(execution)
    return execution
