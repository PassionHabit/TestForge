import asyncio
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import select

from testforge import __version__
from testforge.api import router
from testforge.config import get_settings
from testforge.database import SessionLocal, create_schema
from testforge.delivery import dispatch_pending
from testforge.integrations import cleanup_services
from testforge.models import AuditLog, Execution, ExecutionStatus, ServiceInstance
from testforge.services import reconcile_expired_leases


def run_maintenance_cycle() -> None:
    settings = get_settings()
    with SessionLocal() as db:
        reconcile_expired_leases(db)
        execution_ids = db.scalars(
            select(ServiceInstance.execution_id)
            .join(Execution, Execution.id == ServiceInstance.execution_id)
            .where(
                ServiceInstance.cleanup_status.in_(["PENDING", "FAILED"]),
                Execution.status.in_(
                    [
                        ExecutionStatus.COMPLETED,
                        ExecutionStatus.FAILED,
                        ExecutionStatus.CANCELLED,
                    ]
                ),
            )
            .distinct()
        ).all()
        for execution_id in execution_ids:
            execution = db.get(Execution, execution_id)
            if execution:
                cleanup_services(db, execution)
        dispatch_pending(db, settings)


async def maintenance_loop() -> None:
    settings = get_settings()
    while True:
        await asyncio.sleep(settings.maintenance_interval_seconds)
        try:
            await asyncio.to_thread(run_maintenance_cycle)
        except Exception:
            # A failed integration is retried on the next cycle and must not stop the API.
            pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_schema()
    settings = get_settings()
    task = (
        asyncio.create_task(maintenance_loop()) if settings.enable_background_maintenance else None
    )
    try:
        yield
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def create_app() -> FastAPI:
    app = FastAPI(
        title="TestForge API",
        version=__version__,
        description="Reliable automation test execution control plane",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def security_and_audit(request: Request, call_next):
        settings = get_settings()
        path = request.url.path
        public = path in {
            "/",
            "/api/v1/health",
            "/api/v1/ready",
            "/docs",
            "/openapi.json",
            "/redoc",
        }
        agent_suffixes = (
            "/heartbeat",
            "/claim",
            "/reconcile",
            "/lease/heartbeat",
            "/state",
            "/logs",
            "/finalize",
            "/services/start",
            "/services/cleanup",
        )
        agent_route = (
            path == "/api/v1/agents/register"
            or path.startswith("/api/v1/executions/")
            and (path.endswith(agent_suffixes) or "/artifact-content/" in path)
            or path.startswith("/api/v1/agents/")
            and request.method == "POST"
        )
        expected_key = settings.agent_api_key if agent_route else settings.control_api_key
        authorization = request.headers.get("authorization", "")
        supplied_key = request.headers.get("x-testforge-key")
        if authorization.lower().startswith("bearer "):
            supplied_key = authorization[7:]
        if (
            not public
            and expected_key
            and (not supplied_key or not secrets.compare_digest(supplied_key, expected_key))
        ):
            return JSONResponse({"detail": "invalid API credential"}, status_code=401)

        request_id = request.headers.get("x-request-id", str(uuid.uuid4()))[:100]
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            try:
                with SessionLocal() as db:
                    db.add(
                        AuditLog(
                            actor=request.headers.get("x-actor", "anonymous")[:200],
                            method=request.method,
                            path=path[:1000],
                            status_code=response.status_code,
                            request_id=request_id,
                        )
                    )
                    db.commit()
            except Exception:
                # Audit storage must not turn a successful execution update into a retry.
                pass
        return response

    app.include_router(router)

    @app.get("/", include_in_schema=False)
    def console() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    return app


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run("testforge.main:app", host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    run()
