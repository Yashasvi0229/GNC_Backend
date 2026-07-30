"""
FastAPI application entry point.

Run locally:
    uvicorn app.main:app --reload --port 8000

On Render (see render.yaml):
    uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import settings
from app.core.logging import configure_logging, get_logger
from app.core.middleware import register_exception_handlers, register_middleware
from app.database import dispose_engine

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Runs on startup / shutdown. Configure logging, clean up connections."""
    configure_logging()
    log.info(
        "app_starting",
        env=settings.app_env,
        version=settings.app_version,
    )
    yield
    log.info("app_shutting_down")
    await dispose_engine()


def create_app() -> FastAPI:
    """Build and return the FastAPI app. Called once at import time below."""
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        lifespan=lifespan,
    )

    register_middleware(app)
    register_exception_handlers(app)

    # ---- Routers ----
    # Health is available at both `/health` (for Render's healthcheck)
    # and `/api/health` (for the frontend's /api proxy).
    app.include_router(health_router)
    app.include_router(health_router, prefix="/api")

    # Future routers (Step 3 onwards):
    # app.include_router(auth_router,     prefix="/api/auth",     tags=["auth"])
    # app.include_router(users_router,    prefix="/api/users",    tags=["users"])
    # app.include_router(invoices_router, prefix="/api/invoices", tags=["invoices"])
    # ...

    return app


app = create_app()
