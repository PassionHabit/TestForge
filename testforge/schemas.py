from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from testforge.models import ExecutionStatus, ResultKind


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RecipeStep(BaseModel):
    phase: Literal["prepare", "test", "collect", "cleanup"]
    command: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=3600, ge=1, le=172800)
    continue_on_failure: bool = False


class ServiceBinding(BaseModel):
    service_release_id: str
    binding_key: str = Field(min_length=1, max_length=200)
    phase: Literal["before", "during", "after"]
    parameters: dict[str, Any] = Field(default_factory=dict)
    failure_policy: Literal["abort", "continue"] = "abort"


class ProjectConfig(BaseModel):
    image_digest: str
    recipe: list[RecipeStep]
    required_labels: dict[str, str] = Field(default_factory=dict)
    cpu_millis: int = Field(default=1000, ge=100)
    memory_mb: int = Field(default=1024, ge=128)
    artifact_paths: list[str] = Field(default_factory=list)
    parameter_schema: dict[str, Any] = Field(default_factory=dict)
    services: list[ServiceBinding] = Field(default_factory=list)
    execution_kind: Literal["docker", "native"] = "docker"

    @field_validator("image_digest")
    @classmethod
    def require_digest(cls, value: str) -> str:
        if "@sha256:" not in value and not value.startswith("sha256:"):
            raise ValueError("image_digest must pin an immutable sha256 digest")
        return value

    @model_validator(mode="after")
    def validate_linear_recipe(self):
        phase_order = {"prepare": 0, "test": 1, "collect": 2, "cleanup": 3}
        phases = [phase_order[step.phase] for step in self.recipe]
        if not any(step.phase == "test" for step in self.recipe):
            raise ValueError("recipe must contain at least one test step")
        if phases != sorted(phases):
            raise ValueError("recipe phases must follow prepare -> test -> collect -> cleanup")
        if (
            self.execution_kind == "native"
            and self.required_labels.get("executor_pool") != "native"
        ):
            raise ValueError("native execution requires executor_pool=native")
        return self


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    repository_url: str
    source_type: Literal["git", "svn"] = "git"
    framework: str
    owner: str
    concurrency_limit: int = Field(default=1, ge=1, le=200)
    draft_config: ProjectConfig


class ProjectUpdate(BaseModel):
    owner: str | None = None
    concurrency_limit: int | None = Field(default=None, ge=1, le=200)
    draft_config: ProjectConfig | None = None


class ProjectView(ORMModel):
    id: str
    name: str
    repository_url: str
    source_type: str
    framework: str
    owner: str
    concurrency_limit: int
    draft_config: dict[str, Any]
    current_release_id: str | None
    created_at: datetime
    updated_at: datetime


class ReleaseView(ORMModel):
    id: str
    project_id: str
    version: int
    snapshot: dict[str, Any]
    snapshot_hash: str
    created_at: datetime


class ExecutorCreate(BaseModel):
    name: str
    kind: Literal["docker", "native"] = "docker"
    image_digest: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    cpu_millis: int = Field(default=1000, ge=100)
    memory_mb: int = Field(default=1024, ge=128)
    max_concurrency: int = Field(default=1, ge=1, le=200)


class ExecutorView(ORMModel):
    id: str
    name: str
    kind: str
    image_digest: str | None
    labels: dict[str, str]
    cpu_millis: int
    memory_mb: int
    max_concurrency: int
    maintenance: bool


class ImageBuildCreate(BaseModel):
    repository_url: str
    source_revision: str = Field(min_length=1, max_length=200)
    dockerfile: str = Field(min_length=1, max_length=100_000)
    requested_by: str


class ImageBuildResult(BaseModel):
    status: Literal["PUBLISHED", "FAILED"]
    image_digest: str | None = None
    scan_passed: bool
    failure_reason: str | None = None

    @model_validator(mode="after")
    def validate_published_image(self):
        if self.status == "PUBLISHED":
            if not self.scan_passed:
                raise ValueError("a published build must pass security scanning")
            if not self.image_digest or "@sha256:" not in self.image_digest:
                raise ValueError("a published build requires an immutable image digest")
        return self


class ImageBuildView(ORMModel):
    id: str
    repository_url: str
    source_revision: str
    dockerfile: str
    requested_by: str
    status: str
    image_digest: str | None
    scan_passed: bool | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime


class ServiceConfig(BaseModel):
    create_url: str
    health_url: str | None = None
    cleanup_url: str
    status_url: str | None = None
    auth_header_env: str | None = None
    timeout_seconds: int = Field(default=30, ge=1, le=300)
    retries: int = Field(default=2, ge=0, le=10)


class ServiceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    service_type: str = Field(min_length=2, max_length=80)
    owner: str
    draft_config: ServiceConfig


class ServiceUpdate(BaseModel):
    owner: str | None = None
    draft_config: ServiceConfig | None = None


class ServiceView(ORMModel):
    id: str
    name: str
    service_type: str
    owner: str
    draft_config: dict[str, Any]
    current_release_id: str | None
    created_at: datetime
    updated_at: datetime


class ServiceReleaseView(ORMModel):
    id: str
    service_id: str
    version: int
    snapshot: dict[str, Any]
    snapshot_hash: str
    created_at: datetime


class ExecutionCreate(BaseModel):
    project_id: str
    release_id: str | None = None
    source_revision: str = Field(min_length=1, max_length=200)
    parameters: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=200)
    callback_url: str | None = None


class ExecutionView(ORMModel):
    id: str
    project_id: str
    release_id: str
    idempotency_key: str
    status: ExecutionStatus
    result_kind: ResultKind
    source_revision: str
    image_digest: str
    context: dict[str, Any]
    parameters: dict[str, Any]
    required_labels: dict[str, str]
    cpu_millis: int
    memory_mb: int
    attempt_no: int
    lease_generation: int
    cancel_requested: bool
    process_exit_code: int | None
    artifacts_sealed: bool
    cleanup_complete: bool
    delivery_complete: bool
    failure_reason: str | None
    callback_url: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


class AgentRegistration(BaseModel):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)
    cpu_millis: int = Field(default=4000, ge=100)
    memory_mb: int = Field(default=8192, ge=128)
    max_concurrency: int = Field(default=1, ge=1, le=200)


class AgentView(ORMModel):
    id: str
    name: str
    labels: dict[str, str]
    cpu_millis: int
    memory_mb: int
    max_concurrency: int
    maintenance: bool
    last_seen_at: datetime


class AgentMaintenance(BaseModel):
    maintenance: bool


class ObservedRuntime(BaseModel):
    execution_id: str
    generation: int = Field(ge=1)
    container_name: str


class ReconcileRequest(BaseModel):
    runtimes: list[ObservedRuntime] = Field(default_factory=list)


class ReconcileResolution(BaseModel):
    action: Literal["requeue_unstarted", "mark_failed", "mark_cancelled"]
    reason: str = Field(min_length=3, max_length=1000)


class LeaseWork(BaseModel):
    lease_id: str
    lease_token: str
    generation: int
    expires_at: datetime
    execution: ExecutionView


class LeaseHeartbeat(BaseModel):
    lease_token: str
    generation: int


class ServicePhaseRequest(LeaseHeartbeat):
    phase: Literal["before", "during", "after"]


class StateReport(LeaseHeartbeat):
    status: Literal["RUNNING", "FINALIZING"]


class LogChunkCreate(LeaseHeartbeat):
    sequence: int = Field(ge=0)
    stream: Literal["stdout", "stderr", "system"]
    content: str


class LogChunkView(ORMModel):
    sequence: int
    stream: str
    content: str
    created_at: datetime


class ArtifactItem(BaseModel):
    path: str
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    uri: str | None = None


class FinalizeRequest(LeaseHeartbeat):
    process_exit_code: int | None = None
    result_kind: ResultKind
    artifacts: list[ArtifactItem] = Field(default_factory=list)
    cleanup_complete: bool
    delivery_complete: bool
    failure_reason: str | None = None


class CancelResponse(BaseModel):
    execution_id: str
    cancel_requested: bool


class HealthResponse(BaseModel):
    status: str
    version: str
