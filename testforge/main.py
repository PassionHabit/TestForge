from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from testforge import __version__
from testforge.api import router
from testforge.config import get_settings
from testforge.database import create_schema


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_schema()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="TestForge API",
        version=__version__,
        description="Reliable automation test execution control plane",
        lifespan=lifespan,
    )
    app.include_router(router)
    return app


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run("testforge.main:app", host=settings.api_host, port=settings.api_port)


if __name__ == "__main__":
    run()
