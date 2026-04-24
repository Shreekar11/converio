"""FastAPI application entry point for Converio."""

import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.api.v1.middleware.auth import JWTAuthenticationMiddleware
from app.api.v1.router import api_router
from app.core.config import settings
from app.utils.logging import get_logger

LOGGER = get_logger(__name__, level=settings.log_level)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

async def init_database(auto_migrate: bool = False, drop_existing: bool = False):
    if auto_migrate:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _run_migrations)


def _run_migrations():
    from alembic.config import Config

    from alembic import command

    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "head")


async def close_database():
    from app.core.database import engine

    await engine.dispose()


# ---------------------------------------------------------------------------
# Neo4j helpers
# ---------------------------------------------------------------------------

async def init_neo4j(ensure_constraints: bool = False):
    from app.core.neo4j_client import Neo4jClientManager

    if ensure_constraints:
        await Neo4jClientManager.ensure_constraints()


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager."""
    LOGGER.info(
        "Starting application",
        extra={
            "app_name": settings.app_name,
            "version": settings.app_version,
            "environment": settings.environment,
        },
    )

    lock_file = "/tmp/converio_init.lock"
    should_initialize = not os.path.exists(lock_file)

    if should_initialize:
        LOGGER.info("First startup detected, running migrations...")

        try:
            await asyncio.wait_for(
                init_database(auto_migrate=True, drop_existing=False),
                timeout=settings.db_init_timeout,
            )
            LOGGER.info("Database initialized successfully")
        except TimeoutError:
            LOGGER.error(f"Database initialization timed out after {settings.db_init_timeout}s")
        except Exception as e:
            LOGGER.error(f"Database initialization failed: {e}", exc_info=True)

        try:
            await asyncio.wait_for(
                init_neo4j(ensure_constraints=True),
                timeout=20.0,
            )
            LOGGER.info("Neo4j initialized successfully")
        except TimeoutError:
            LOGGER.error("Neo4j initialization timed out after 20s")
        except Exception as e:
            LOGGER.error(f"Neo4j initialization failed: {e}", exc_info=True)

        try:
            with open(lock_file, "w") as f:
                f.write("initialized")
        except Exception as e:
            LOGGER.warning(f"Failed to create init lock file: {e}")
    else:
        LOGGER.info("Reload detected, skipping migrations...")

    yield

    LOGGER.info("Shutting down application")
    try:
        await close_database()
    except Exception as e:
        LOGGER.error("Error closing database", exc_info=True, extra={"error": str(e)})


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="AI-native talent matching engine",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)


# Middleware order (innermost → outermost):
# 1. Correlation ID (innermost — @app.middleware decorator)
# 2. JWTAuthenticationMiddleware
# 3. CORSMiddleware (outermost — add_middleware call)

@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid4()))
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    return response


app.add_middleware(JWTAuthenticationMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Correlation-ID"],
)

# Routers
app.include_router(api_router, prefix=settings.api_v1_prefix)


class RootResponse(BaseModel):
    message: str = Field(..., description="Service status message")
    version: str = Field(..., description="Running application version")
    docs: str = Field(..., description="Path to the interactive API docs")
    health: str = Field(..., description="Path to the health check endpoint")


@app.get(
    "/",
    response_model=RootResponse,
    tags=["Root"],
    summary="Root endpoint",
    operation_id="get_root",
)
async def root() -> RootResponse:
    return RootResponse(
        message="Converio API is running",
        version=settings.app_version,
        docs="/docs",
        health=f"{settings.api_v1_prefix}/health",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )
