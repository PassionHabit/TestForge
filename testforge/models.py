import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from testforge.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def uuid4str() -> str:
    return str(uuid.uuid4())


class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RECONCILING = "RECONCILING"


class ResultKind(StrEnum):
    PASSED = "PASSED"
    TEST_FAILED = "TEST_FAILED"
    INFRA_FAILED = "INFRA_FAILED"
    CANCELLED = "CANCELLED"
    NOT_EVALUATED = "NOT_EVALUATED"


class LeaseStatus(StrEnum):
    OFFERED = "OFFERED"
    CLAIMED = "CLAIMED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    repository_url: Mapped[str] = mapped_column(String(1000))
    framework: Mapped[str] = mapped_column(String(50))
    owner: Mapped[str] = mapped_column(String(200))
    concurrency_limit: Mapped[int] = mapped_column(Integer, default=1)
    draft_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    current_release_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProjectRelease(Base):
    __tablename__ = "project_releases"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_project_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    project: Mapped[Project] = relationship()


class Executor(Base):
    __tablename__ = "executors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(30), default="docker")
    image_digest: Mapped[str | None] = mapped_column(String(300), nullable=True)
    labels: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    cpu_millis: Mapped[int] = mapped_column(Integer, default=1000)
    memory_mb: Mapped[int] = mapped_column(Integer, default=1024)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1)
    maintenance: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    labels: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    cpu_millis: Mapped[int] = mapped_column(Integer)
    memory_mb: Mapped[int] = mapped_column(Integer)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1)
    maintenance: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Execution(Base):
    __tablename__ = "executions"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_execution_idempotency"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    release_id: Mapped[str] = mapped_column(ForeignKey("project_releases.id"))
    idempotency_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[ExecutionStatus] = mapped_column(Enum(ExecutionStatus), index=True)
    result_kind: Mapped[ResultKind] = mapped_column(
        Enum(ResultKind), default=ResultKind.NOT_EVALUATED
    )
    source_revision: Mapped[str] = mapped_column(String(200))
    image_digest: Mapped[str] = mapped_column(String(300))
    context: Mapped[dict[str, Any]] = mapped_column(JSON)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    required_labels: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    cpu_millis: Mapped[int] = mapped_column(Integer, default=1000)
    memory_mb: Mapped[int] = mapped_column(Integer, default=1024)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    lease_generation: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    process_exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    artifacts_sealed: Mapped[bool] = mapped_column(Boolean, default=False)
    cleanup_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    delivery_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    release: Mapped[ProjectRelease] = relationship()


class Lease(Base):
    __tablename__ = "leases"
    __table_args__ = (UniqueConstraint("execution_id", "generation", name="uq_lease_generation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"), index=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    token_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[LeaseStatus] = mapped_column(Enum(LeaseStatus))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LogChunk(Base):
    __tablename__ = "log_chunks"
    __table_args__ = (UniqueConstraint("execution_id", "sequence", name="uq_log_sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    stream: Mapped[str] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ArtifactManifest(Base):
    __tablename__ = "artifact_manifests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"), unique=True)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64))
    sealed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExecutionEvent(Base):
    __tablename__ = "execution_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid4str)
    execution_id: Mapped[str] = mapped_column(ForeignKey("executions.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
