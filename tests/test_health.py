"""Smoke tests for the Phase 0 application skeleton."""

from __future__ import annotations

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.main import app

client = TestClient(app)


def test_health_returns_ok_when_dependencies_are_available(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr("app.api.routes.database_is_available", lambda: True)
    monkeypatch.setattr("app.api.routes.redis_is_available", lambda: True)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_returns_503_when_a_dependency_is_unavailable(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr("app.api.routes.database_is_available", lambda: True)
    monkeypatch.setattr("app.api.routes.redis_is_available", lambda: False)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
