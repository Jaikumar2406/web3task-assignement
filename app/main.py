"""FastAPI application factory and global configuration."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import health_router, webhooks_router
from app.config import settings
from app.database.connection import init_db

logging.basicConfig(level=settings.log_level.upper())


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Create missing tables before the API starts serving requests."""
    init_db()
    yield


def create_app() -> FastAPI:
    """Build the API application and register its routers."""
    application = FastAPI(title="Webhook Dispatcher", version="0.1.0", lifespan=lifespan)
    application.include_router(health_router)
    application.include_router(webhooks_router)
    return application


app = create_app()
