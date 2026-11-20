"""Phase 1 API tests: ingestion, validation, persistence and status lookup."""

from __future__ import annotations

import time
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from pytest_httpserver import HTTPServer
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models.delivery_log import DeliveryLog
from app.models.webhook import WebhookJob

VALID_REQUEST = {
    "destinationUrl": "https://example.com/hooks",
    "eventId": "evt_1",
    "payload": {"event": "user.signup", "userId": "usr_999"},
}
DISPATCH_URL = "/api/webhooks/dispatch"


def test_valid_request_returns_202(client: TestClient, monkeypatch: MonkeyPatch) -> None:
    enqueue = MagicMock()
    monkeypatch.setattr("app.api.routes.deliver_webhook.apply_async", enqueue)

    response = client.post(DISPATCH_URL, json=VALID_REQUEST)

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["jobId"].startswith("job_")
    enqueue.assert_called_once()


@pytest.mark.parametrize(
    "body",
    [
        {"destinationUrl": "ftp://example.com", "eventId": "evt_1", "payload": {}},
        {"destinationUrl": "https://example.com", "eventId": "", "payload": {}},
        {"destinationUrl": "https://example.com", "payload": {}},
        {"destinationUrl": "https://example.com", "eventId": "evt_1", "payload": ["bad"]},
    ],
)
def test_invalid_request_returns_422(client: TestClient, body: dict[str, object]) -> None:
    response = client.post(DISPATCH_URL, json=body)

    assert response.status_code == 422


def test_event_is_persisted_before_enqueue(
    client: TestClient,
    db_session: Session,
    monkeypatch: MonkeyPatch,
) -> None:
    enqueued: list[UUID] = []

    def record_enqueue(args: list[str], **kwargs: object) -> None:
        job_id = UUID(args[0])
        assert db_session.get(WebhookJob, job_id) is not None
        enqueued.append(job_id)

    monkeypatch.setattr("app.api.routes.deliver_webhook.apply_async", record_enqueue)

    response = client.post(DISPATCH_URL, json=VALID_REQUEST)

    assert response.status_code == 202
    assert len(enqueued) == 1
    job = db_session.get(WebhookJob, enqueued[0])
    assert job is not None
    assert job.status == "queued"
    assert job.event_id == "evt_1"
    assert job.payload == VALID_REQUEST["payload"]
    assert job.attempt_count == 0


def test_oversized_payload_returns_413(client: TestClient, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "max_payload_bytes", 32)
    request = {**VALID_REQUEST, "payload": {"blob": "x" * 100}}

    response = client.post(DISPATCH_URL, json=request)

    assert response.status_code == 413


def test_enqueue_failure_returns_503_and_keeps_job(
    client: TestClient,
    db_session: Session,
    monkeypatch: MonkeyPatch,
) -> None:
    def fail_enqueue(args: list[str], **kwargs: object) -> None:
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr("app.api.routes.deliver_webhook.apply_async", fail_enqueue)

    response = client.post(DISPATCH_URL, json=VALID_REQUEST)

    assert response.status_code == 503
    job = db_session.query(WebhookJob).one()
    assert job.status == "queued"


def test_database_failure_returns_503(
    client: TestClient,
    db_session: Session,
    monkeypatch: MonkeyPatch,
) -> None:
    def fail_commit() -> None:
        raise SQLAlchemyError("database unavailable")

    monkeypatch.setattr(db_session, "commit", fail_commit)

    response = client.post(DISPATCH_URL, json=VALID_REQUEST)

    assert response.status_code == 503


def test_status_endpoint_returns_job_with_ordered_attempts(
    client: TestClient,
    db_session: Session,
) -> None:
    job = WebhookJob(
        event_id="evt_2",
        destination_url="https://example.com/hooks",
        payload={},
        status="success",
        attempt_count=2,
    )
    db_session.add(job)
    db_session.commit()
    db_session.add_all(
        [
            DeliveryLog(
                job_id=job.id,
                attempt_number=1,
                status="retrying",
                response_code=503,
                error_message=None,
            ),
            DeliveryLog(
                job_id=job.id,
                attempt_number=2,
                status="success",
                response_code=200,
                error_message=None,
            ),
        ]
    )
    db_session.commit()

    response = client.get(f"/api/webhooks/jobs/job_{job.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["jobId"] == f"job_{job.id}"
    assert body["eventId"] == "evt_2"
    assert body["attemptCount"] == 2
    assert [attempt["attemptNumber"] for attempt in body["attempts"]] == [1, 2]
    assert body["attempts"][0]["responseCode"] == 503


def test_unknown_job_returns_404(client: TestClient) -> None:
    response = client.get("/api/webhooks/jobs/job_00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404


@pytest.mark.parametrize("job_id", ["missing-prefix", "job_not-a-uuid"])
def test_malformed_job_id_returns_422(client: TestClient, job_id: str) -> None:
    response = client.get(f"/api/webhooks/jobs/{job_id}")

    assert response.status_code == 422


@pytest.mark.parametrize(
    "destination",
    [
        "http://127.0.0.1/hook",
        "http://10.0.0.5/hook",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost/hook",
        "http://user:pass@example.com/hook",
    ],
)
def test_unsafe_destination_returns_422(client: TestClient, destination: str) -> None:
    response = client.post(DISPATCH_URL, json={**VALID_REQUEST, "destinationUrl": destination})

    assert response.status_code == 422


def test_private_destination_allowed_when_flag_enabled(
    client: TestClient, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "allow_private_destinations", True)
    monkeypatch.setattr("app.api.routes.deliver_webhook.apply_async", MagicMock())

    response = client.post(
        DISPATCH_URL, json={**VALID_REQUEST, "destinationUrl": "http://127.0.0.1:9999/hook"}
    )

    assert response.status_code == 202


def test_oversized_url_returns_422(client: TestClient) -> None:
    destination = "https://example.com/" + "a" * 3000

    response = client.post(DISPATCH_URL, json={**VALID_REQUEST, "destinationUrl": destination})

    assert response.status_code == 422


def test_slow_destination_does_not_block_dispatch(
    client: TestClient, httpserver: HTTPServer, monkeypatch: MonkeyPatch
) -> None:
    httpserver.expect_request("/slow").respond_with_data("ok")
    monkeypatch.setattr(settings, "allow_private_destinations", True)
    monkeypatch.setattr("app.api.routes.deliver_webhook.apply_async", MagicMock())

    started = time.monotonic()
    response = client.post(
        DISPATCH_URL, json={**VALID_REQUEST, "destinationUrl": httpserver.url_for("/slow")}
    )

    assert response.status_code == 202
    assert time.monotonic() - started < 1.0
    assert not httpserver.log
