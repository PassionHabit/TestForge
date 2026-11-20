from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from testforge import __version__
from testforge.config import Settings, get_settings
from testforge.database import get_db
from testforge.delivery import dispatch_pending
from testforge.integrations import IntegrationFailure, cleanup_services, start_service_phase
from testforge.models import (
    Agent,
    ArtifactManifest,
    AuditLog,
    Execution,
    ExecutionEvent,
    ExecutionStatus,
    Executor,
    ImageBuild,
    LogChunk,
    Project,
    ProjectRelease,
    PublicService,
    ResultKind,
    ServiceRelease,
    utcnow,
)
from testforge.redaction import redact_log
from testforge.schemas import (
    AgentMaintenance,
    AgentRegistration,
    AgentView,
    ArtifactItem,
    CancelResponse,
    ExecutionCreate,
    ExecutionView,
    ExecutorCreate,
    ExecutorView,
    FinalizeRequest,
    HealthResponse,
    ImageBuildCreate,
    ImageBuildResult,
    ImageBuildView,
    LeaseHeartbeat,
    LeaseWork,
    LogChunkCreate,
    LogChunkView,
    ProjectCreate,
    ProjectUpdate,
    ProjectView,
    ReconcileRequest,
    ReconcileResolution,
    ReleaseView,
    ServiceCreate,
    ServicePhaseRequest,
    ServiceReleaseView,
    ServiceUpdate,
    ServiceView,
    StateReport,
)
from testforge.services import (
    ConflictError,
    InputValidationError,
    add_event,
    claim_work,
    create_execution,
    finalize_execution,
    heartbeat_lease,
    publish_project,
    publish_service,
    reconcile_expired_leases,
    recover_observed_runtime,
    register_agent,
    resolve_reconciliation,
    transition,
    verify_lease,
)
from testforge.state import InvalidTransition
from testforge.storage import InvalidArtifactPath, store_artifact

router = APIRouter(prefix="/api/v1")
Db = Annotated[Session, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_or_404(db: Session, model: type, object_id: str):
    value = db.get(model, object_id)
    if value is None:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return value


def lease_or_403(db: Session, execution: Execution, token: str, generation: int):
    try:
        return verify_lease(db, execution, token, generation)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@router.get("/ready", response_model=HealthResponse, tags=["system"])
def ready(db: Db) -> HealthResponse:
    db.execute(text("SELECT 1"))
    return HealthResponse(status="ready", version=__version__)


@router.post(
    "/projects", response_model=ProjectView, status_code=status.HTTP_201_CREATED, tags=["projects"]
)
def create_project(request: ProjectCreate, db: Db) -> Project:
    project = Project(**request.model_dump(mode="json"))
    db.add(project)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="project name already exists") from exc
    db.refresh(project)
    return project


@router.get("/projects", response_model=list[ProjectView], tags=["projects"])
def list_projects(db: Db) -> list[Project]:
    return list(db.scalars(select(Project).order_by(Project.created_at)).all())


@router.get("/projects/{project_id}", response_model=ProjectView, tags=["projects"])
def get_project(project_id: str, db: Db) -> Project:
    return get_or_404(db, Project, project_id)


@router.patch("/projects/{project_id}", response_model=ProjectView, tags=["projects"])
def update_project(project_id: str, request: ProjectUpdate, db: Db) -> Project:
    project = get_or_404(db, Project, project_id)
    for field, value in request.model_dump(exclude_none=True, mode="json").items():
        setattr(project, field, value)
    project.updated_at = utcnow()
    db.commit()
    db.refresh(project)
    return project


@router.post(
    "/projects/{project_id}/releases",
    response_model=ReleaseView,
    status_code=status.HTTP_201_CREATED,
    tags=["projects"],
)
def release_project(project_id: str, db: Db) -> ProjectRelease:
    try:
        return publish_project(db, get_or_404(db, Project, project_id))
    except InputValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/projects/{project_id}/releases", response_model=list[ReleaseView], tags=["projects"])
def list_releases(project_id: str, db: Db) -> list[ProjectRelease]:
    get_or_404(db, Project, project_id)
    return list(
        db.scalars(
            select(ProjectRelease)
            .where(ProjectRelease.project_id == project_id)
            .order_by(ProjectRelease.version)
        ).all()
    )


@router.post(
    "/executors",
    response_model=ExecutorView,
    status_code=status.HTTP_201_CREATED,
    tags=["executors"],
)
def create_executor(request: ExecutorCreate, db: Db) -> Executor:
    executor = Executor(**request.model_dump(mode="json"))
    db.add(executor)
    db.commit()
    db.refresh(executor)
    return executor


@router.get("/executors", response_model=list[ExecutorView], tags=["executors"])
def list_executors(db: Db) -> list[Executor]:
    return list(db.scalars(select(Executor).order_by(Executor.name)).all())


@router.post(
    "/image-builds",
    response_model=ImageBuildView,
    status_code=status.HTTP_201_CREATED,
    tags=["image builds"],
)
def create_image_build(request: ImageBuildCreate, db: Db) -> ImageBuild:
    build = ImageBuild(**request.model_dump())
    db.add(build)
    db.commit()
    db.refresh(build)
    return build


@router.get("/image-builds", response_model=list[ImageBuildView], tags=["image builds"])
def list_image_builds(db: Db, build_status: Annotated[str | None, Query(alias="status")] = None):
    query = select(ImageBuild).order_by(ImageBuild.created_at)
    if build_status:
        query = query.where(ImageBuild.status == build_status)
    return list(db.scalars(query).all())


@router.post(
    "/image-builds/{build_id}/result",
    response_model=ImageBuildView,
    tags=["image builds"],
)
def record_image_build_result(build_id: str, request: ImageBuildResult, db: Db) -> ImageBuild:
    build = get_or_404(db, ImageBuild, build_id)
    if build.status not in {"QUEUED", "BUILDING"}:
        raise HTTPException(status_code=409, detail="image build is already terminal")
    build.status = request.status
    build.image_digest = request.image_digest
    build.scan_passed = request.scan_passed
    build.failure_reason = request.failure_reason
    build.updated_at = utcnow()
    db.commit()
    db.refresh(build)
    return build


@router.post(
    "/services",
    response_model=ServiceView,
    status_code=status.HTTP_201_CREATED,
    tags=["public services"],
)
def create_service(request: ServiceCreate, db: Db) -> PublicService:
    service = PublicService(**request.model_dump(mode="json"))
    db.add(service)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="service name already exists") from exc
    db.refresh(service)
    return service


@router.get("/services", response_model=list[ServiceView], tags=["public services"])
def list_services(db: Db) -> list[PublicService]:
    return list(db.scalars(select(PublicService).order_by(PublicService.name)).all())


@router.patch("/services/{service_id}", response_model=ServiceView, tags=["public services"])
def update_service(service_id: str, request: ServiceUpdate, db: Db) -> PublicService:
    service = get_or_404(db, PublicService, service_id)
    for field, value in request.model_dump(exclude_none=True, mode="json").items():
        setattr(service, field, value)
    service.updated_at = utcnow()
    db.commit()
    db.refresh(service)
    return service


@router.post(
    "/services/{service_id}/releases",
    response_model=ServiceReleaseView,
    status_code=status.HTTP_201_CREATED,
    tags=["public services"],
)
def release_service(service_id: str, db: Db) -> ServiceRelease:
    return publish_service(db, get_or_404(db, PublicService, service_id))


@router.get(
    "/services/{service_id}/releases",
    response_model=list[ServiceReleaseView],
    tags=["public services"],
)
def list_service_releases(service_id: str, db: Db) -> list[ServiceRelease]:
    get_or_404(db, PublicService, service_id)
    return list(
        db.scalars(
            select(ServiceRelease)
            .where(ServiceRelease.service_id == service_id)
            .order_by(ServiceRelease.version)
        ).all()
    )


@router.post(
    "/executions",
    response_model=ExecutionView,
    status_code=status.HTTP_201_CREATED,
    tags=["executions"],
)
def submit_execution(request: ExecutionCreate, db: Db, response: Response) -> Execution:
    try:
        existing = db.scalar(
            select(Execution).where(Execution.idempotency_key == request.idempotency_key)
        )
        execution = create_execution(db, request)
        if existing:
            response.status_code = status.HTTP_200_OK
        return execution
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InputValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/executions", response_model=list[ExecutionView], tags=["executions"])
def list_executions(
    db: Db,
    execution_status: Annotated[ExecutionStatus | None, Query(alias="status")] = None,
    project_id: str | None = None,
) -> list[Execution]:
    query = select(Execution).order_by(Execution.created_at.desc())
    if execution_status:
        query = query.where(Execution.status == execution_status)
    if project_id:
        query = query.where(Execution.project_id == project_id)
    return list(db.scalars(query).all())


@router.get("/executions/{execution_id}", response_model=ExecutionView, tags=["executions"])
def get_execution(execution_id: str, db: Db) -> Execution:
    return get_or_404(db, Execution, execution_id)


@router.post(
    "/executions/{execution_id}/cancel", response_model=CancelResponse, tags=["executions"]
)
def cancel_execution(execution_id: str, db: Db) -> CancelResponse:
    execution = get_or_404(db, Execution, execution_id)
    if execution.status in {
        ExecutionStatus.COMPLETED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
    }:
        raise HTTPException(status_code=409, detail="execution is already terminal")
    execution.cancel_requested = True
    if execution.status is ExecutionStatus.QUEUED:
        execution.result_kind = ResultKind.CANCELLED
        transition(db, execution, ExecutionStatus.CANCELLED)
    else:
        add_event(db, execution, "CANCEL_REQUESTED")
    db.commit()
    return CancelResponse(execution_id=execution.id, cancel_requested=True)


@router.get(
    "/executions/{execution_id}/logs", response_model=list[LogChunkView], tags=["executions"]
)
def get_logs(execution_id: str, db: Db, after_sequence: int = -1) -> list[LogChunk]:
    get_or_404(db, Execution, execution_id)
    return list(
        db.scalars(
            select(LogChunk)
            .where(LogChunk.execution_id == execution_id, LogChunk.sequence > after_sequence)
            .order_by(LogChunk.sequence)
        ).all()
    )


@router.get(
    "/executions/{execution_id}/artifacts", response_model=list[ArtifactItem], tags=["executions"]
)
def get_artifacts(execution_id: str, db: Db) -> list[dict]:
    get_or_404(db, Execution, execution_id)
    manifest = db.scalar(
        select(ArtifactManifest).where(ArtifactManifest.execution_id == execution_id)
    )
    return manifest.items if manifest else []


@router.put(
    "/executions/{execution_id}/artifact-content/{artifact_path:path}",
    status_code=status.HTTP_201_CREATED,
    tags=["agent protocol"],
)
async def upload_artifact_content(
    execution_id: str,
    artifact_path: str,
    request: Request,
    db: Db,
    settings: AppSettings,
    x_lease_token: Annotated[str, Header()],
    x_lease_generation: Annotated[int, Header()],
) -> dict:
    execution = get_or_404(db, Execution, execution_id)
    lease_or_403(db, execution, x_lease_token, x_lease_generation)
    try:
        blob = store_artifact(db, settings, execution_id, artifact_path, await request.body())
    except InvalidArtifactPath as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "path": blob.path,
        "size": blob.size,
        "sha256": blob.sha256,
        "uri": blob.storage_uri,
    }


@router.get("/executions/{execution_id}/events", tags=["executions"])
def get_events(execution_id: str, db: Db) -> list[dict]:
    get_or_404(db, Execution, execution_id)
    events = db.scalars(
        select(ExecutionEvent)
        .where(ExecutionEvent.execution_id == execution_id)
        .order_by(ExecutionEvent.created_at)
    ).all()
    return [
        {
            "id": event.id,
            "type": event.event_type,
            "payload": event.payload,
            "created_at": event.created_at,
        }
        for event in events
    ]


@router.post("/agents/register", response_model=AgentView, tags=["agents"])
def agent_register(request: AgentRegistration, db: Db) -> Agent:
    return register_agent(db, request)


@router.post("/agents/{agent_id}/heartbeat", response_model=AgentView, tags=["agents"])
def agent_heartbeat(agent_id: str, db: Db) -> Agent:
    agent = get_or_404(db, Agent, agent_id)
    agent.last_seen_at = utcnow()
    db.commit()
    db.refresh(agent)
    return agent


@router.get("/agents", response_model=list[AgentView], tags=["agents"])
def list_agents(db: Db) -> list[Agent]:
    return list(db.scalars(select(Agent).order_by(Agent.name)).all())


@router.patch("/agents/{agent_id}/maintenance", response_model=AgentView, tags=["agents"])
def set_agent_maintenance(agent_id: str, request: AgentMaintenance, db: Db) -> Agent:
    agent = get_or_404(db, Agent, agent_id)
    agent.maintenance = request.maintenance
    db.commit()
    db.refresh(agent)
    return agent


@router.post("/agents/{agent_id}/reconcile", response_model=list[LeaseWork], tags=["agents"])
def reconcile_agent(
    agent_id: str, request: ReconcileRequest, db: Db, settings: AppSettings
) -> list[LeaseWork]:
    agent = get_or_404(db, Agent, agent_id)
    agent.last_seen_at = utcnow()
    recovered: list[LeaseWork] = []
    for runtime in request.runtimes:
        result = recover_observed_runtime(
            db, agent, runtime.execution_id, runtime.generation, settings
        )
        if result:
            lease, token, execution = result
            recovered.append(
                LeaseWork(
                    lease_id=lease.id,
                    lease_token=token,
                    generation=lease.generation,
                    expires_at=lease.expires_at,
                    execution=ExecutionView.model_validate(execution),
                )
            )
    db.commit()
    return recovered


@router.post("/agents/{agent_id}/claim", response_model=LeaseWork | None, tags=["agents"])
def agent_claim(agent_id: str, db: Db, settings: AppSettings, response: Response):
    agent = get_or_404(db, Agent, agent_id)
    claimed = claim_work(db, agent, settings)
    if claimed is None:
        response.status_code = status.HTTP_204_NO_CONTENT
        return None
    lease, token, execution = claimed
    return LeaseWork(
        lease_id=lease.id,
        lease_token=token,
        generation=lease.generation,
        expires_at=lease.expires_at,
        execution=ExecutionView.model_validate(execution),
    )


@router.post("/executions/{execution_id}/lease/heartbeat", tags=["agent protocol"])
def execution_heartbeat(
    execution_id: str, request: LeaseHeartbeat, db: Db, settings: AppSettings
) -> dict:
    execution = get_or_404(db, Execution, execution_id)
    lease = lease_or_403(db, execution, request.lease_token, request.generation)
    heartbeat_lease(db, lease, settings)
    return {
        "expires_at": lease.expires_at,
        "cancel_requested": execution.cancel_requested,
        "status": execution.status,
    }


@router.post("/executions/{execution_id}/services/start", tags=["agent protocol"])
def start_execution_services(execution_id: str, request: ServicePhaseRequest, db: Db) -> dict:
    execution = get_or_404(db, Execution, execution_id)
    lease_or_403(db, execution, request.lease_token, request.generation)
    try:
        instances = start_service_phase(db, execution, request.phase)
    except IntegrationFailure as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"started": len(instances), "phase": request.phase}


@router.post("/executions/{execution_id}/services/cleanup", tags=["agent protocol"])
def cleanup_execution_services(
    execution_id: str, request: LeaseHeartbeat, db: Db
) -> dict[str, int]:
    execution = get_or_404(db, Execution, execution_id)
    lease_or_403(db, execution, request.lease_token, request.generation)
    return cleanup_services(db, execution)


@router.post(
    "/executions/{execution_id}/state", response_model=ExecutionView, tags=["agent protocol"]
)
def report_state(execution_id: str, request: StateReport, db: Db) -> Execution:
    execution = get_or_404(db, Execution, execution_id)
    lease_or_403(db, execution, request.lease_token, request.generation)
    try:
        transition(db, execution, ExecutionStatus(request.status))
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    db.refresh(execution)
    return execution


@router.post(
    "/executions/{execution_id}/logs",
    status_code=status.HTTP_201_CREATED,
    tags=["agent protocol"],
)
def append_log(execution_id: str, request: LogChunkCreate, db: Db, settings: AppSettings) -> dict:
    execution = get_or_404(db, Execution, execution_id)
    lease_or_403(db, execution, request.lease_token, request.generation)
    if len(request.content.encode()) > settings.log_chunk_max_bytes:
        raise HTTPException(status_code=413, detail="log chunk exceeds configured size")
    existing = db.scalar(
        select(LogChunk).where(
            LogChunk.execution_id == execution_id, LogChunk.sequence == request.sequence
        )
    )
    if existing:
        return {"accepted": True, "duplicate": True}
    db.add(
        LogChunk(
            execution_id=execution_id,
            sequence=request.sequence,
            stream=request.stream,
            content=redact_log(request.content, settings.log_redaction_regex),
        )
    )
    db.commit()
    return {"accepted": True, "duplicate": False}


@router.post(
    "/executions/{execution_id}/finalize", response_model=ExecutionView, tags=["agent protocol"]
)
def finalize(execution_id: str, request: FinalizeRequest, db: Db) -> Execution:
    execution = get_or_404(db, Execution, execution_id)
    lease = lease_or_403(db, execution, request.lease_token, request.generation)
    try:
        return finalize_execution(db, execution, lease, request)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/admin/reconcile", tags=["system"])
def reconcile(db: Db) -> dict[str, int | datetime]:
    return {"reconciled": reconcile_expired_leases(db), "at": utcnow()}


@router.post("/admin/deliveries/dispatch", tags=["system"])
def dispatch_deliveries(db: Db, settings: AppSettings, limit: int = 100) -> dict[str, int]:
    return dispatch_pending(db, settings, min(max(limit, 1), 500))


@router.get("/metrics", include_in_schema=False)
def metrics(db: Db) -> Response:
    execution_counts = db.execute(
        select(Execution.status, func.count(Execution.id)).group_by(Execution.status)
    ).all()
    agent_total = db.scalar(select(func.count(Agent.id))) or 0
    lines = [
        "# HELP testforge_executions Current executions by state.",
        "# TYPE testforge_executions gauge",
    ]
    for execution_status, count in execution_counts:
        lines.append(f'testforge_executions{{status="{execution_status.value}"}} {count}')
    lines.extend(
        [
            "# HELP testforge_agents Registered agents.",
            "# TYPE testforge_agents gauge",
            f"testforge_agents {agent_total}",
        ]
    )
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@router.get("/admin/audit", tags=["system"])
def list_audit_log(db: Db, limit: int = 100) -> list[dict]:
    rows = db.scalars(
        select(AuditLog).order_by(AuditLog.created_at.desc()).limit(min(max(limit, 1), 1000))
    ).all()
    return [
        {
            "id": row.id,
            "actor": row.actor,
            "method": row.method,
            "path": row.path,
            "status_code": row.status_code,
            "request_id": row.request_id,
            "created_at": row.created_at,
        }
        for row in rows
    ]


@router.post(
    "/admin/executions/{execution_id}/resolve",
    response_model=ExecutionView,
    tags=["system"],
)
def resolve_execution(execution_id: str, request: ReconcileResolution, db: Db) -> Execution:
    execution = get_or_404(db, Execution, execution_id)
    try:
        return resolve_reconciliation(db, execution, request.action, request.reason)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
