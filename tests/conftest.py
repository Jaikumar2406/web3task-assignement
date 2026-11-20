"""Shared test setup applied before application modules are imported."""

from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg2://webhook:webhook@localhost:5432/webhooks"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")

from app.database.connection import Base, get_db
from app.main import app
from app.models.delivery_log import DeliveryLog
from app.models.webhook import WebhookJob


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Give each test its own in-memory database with both tables created."""
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine, tables=[WebhookJob.__table__, DeliveryLog.__table__])
    with Session(test_engine) as session:
        yield session
    test_engine.dispose()


@pytest.fixture
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """Point the app's DB dependency at the test session, without running lifespan."""

    def override_get_db() -> Session:
        return db_session

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    yield test_client
    app.dependency_overrides.clear()
