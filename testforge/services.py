import hashlib
import json
import secrets
from datetime import UTC, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from testforge.config import Settings
from testforge.models import (
    Agent,
    ArtifactManifest,
    Execution,
    ExecutionEvent,
    ExecutionStatus,
    Lease,
    LeaseStatus,
    Project,
    ProjectRelease,
    ResultKind,
    utcnow,
)
from testforge.schemas import AgentRegistration, ExecutionCreate, FinalizeRequest, ProjectConfig
from testforge.state import ensure_transition


class ConflictError(ValueError):
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
    latest = db.scalar(
        select(func.max(ProjectRelease.version)).where(ProjectRelease.project_id == project.id)
    )
    release = ProjectRelease(
        project_id=project.id,
        version=(latest or 0) + 1,
        snapshot={
            "name": project.name,
            "repository_url": project.repository_url,
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
    context = {
        "project_release_version": release.version,
        "project_snapshot_hash": release.snapshot_hash,
        "repository_url": release.snapshot["repository_url"],
        "source_revision": data.source_revision,
        "image_digest": config.image_digest,
        "recipe": [step.model_dump(mode="json") for step in config.recipe],
        "artifact_paths": config.artifact_paths,
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
            and execution.cpu_millis <= agent.cpu_millis
            and execution.memory_mb <= agent.memory_mb
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
