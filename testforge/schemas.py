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


class ProjectConfig(BaseModel):
    image_digest: str
    recipe: list[RecipeStep]
    required_labels: dict[str, str] = Field(default_factory=dict)
    cpu_millis: int = Field(default=1000, ge=100)
    memory_mb: int = Field(default=1024, ge=128)
    artifact_paths: list[str] = Field(default_factory=list)
    parameter_schema: dict[str, Any] = Field(default_factory=dict)

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
        return self


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    repository_url: str
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


class ExecutionCreate(BaseModel):
    project_id: str
    release_id: str | None = None
    source_revision: str = Field(min_length=7, max_length=200)
    parameters: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=8, max_length=200)


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


class LeaseWork(BaseModel):
    lease_id: str
    lease_token: str
    generation: int
    expires_at: datetime
    execution: ExecutionView


class LeaseHeartbeat(BaseModel):
    lease_token: str
    generation: int


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
