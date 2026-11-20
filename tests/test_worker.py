"""Phase 2/3 worker tests: delivery, retries, logs, rate limiting and recovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
import respx
from pytest import MonkeyPatch
from sqlalchemy.orm import Session

from app.database.connection import utcnow
from app.models.delivery_log import DeliveryLog
from app.models.webhook import WebhookJob
from app.workers.tasks import deliver_webhook, sweep_stale_jobs

DESTINATION = "https://example.com/hooks"


class ScheduledCalls:
    """Captured ``apply_async`` calls for countdown and job-id assertions."""

    def __init__(self) -> None:
        self.job_ids: list[str] = []
        self.countdowns: list[int] = []


def make_job(
    db: Session,
    *,
    status: str = "queued",
    attempt_count: int = 0,
    updated_at: datetime | None = None,
) -> WebhookJob:
    """Insert one queued-like job and return it with a real UUID."""
    job = WebhookJob(
        event_id="evt_retry",
        destination_url=DESTINATION,
        payload={"event": "test"},
        status=status,
        attempt_count=attempt_count,
        updated_at=updated_at or utcnow(),
    )
    db.add(job)
    db.commit()
    return job


def fresh_job(db: Session, job_id: UUID) -> WebhookJob:
    """Reload a job so assertions see state committed by the worker."""
    job = db.get(WebhookJob, job_id)
    assert job is not None
    return job


@pytest.fixture
def patch_worker_db(db_session: Session, monkeypatch: MonkeyPatch) -> Session:
    """Point the task at a test DB and bypass limiter/DNS side effects by default."""

    def session_factory() -> Session:
        return db_session

    def allow_all(origin: str) -> tuple[bool, float]:
        return True, 0.0

    def skip_ssrf_check(url: str, **kwargs: object) -> str:
        return url

    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr("app.workers.tasks.rate_limiter.acquire", allow_all)
    monkeypatch.setattr(
        "app.services.dispatcher.security.validate_destination_url", skip_ssrf_check
    )
    return db_session


@pytest.fixture
def scheduled(monkeypatch: MonkeyPatch) -> ScheduledCalls:
    """Capture reschedule calls so no Celery worker or sleeping is needed."""
    calls = ScheduledCalls()

    def fake_apply_async(args: list[str], countdown: int = 0) -> None:
        calls.job_ids.append(args[0])
        calls.countdowns.append(countdown)

    monkeypatch.setattr(deliver_webhook, "apply_async", fake_apply_async)
    return calls


@pytest.fixture
def enqueued(scheduled: ScheduledCalls) -> list[int]:
    """Expose captured countdowns with the same list identity used by tests."""
    return scheduled.countdowns


@respx.mock
def test_successful_delivery_marks_job_success_and_logs_attempt(
    patch_worker_db: Session, enqueued: list[int]
) -> None:
    respx.post(DESTINATION).mock(return_value=httpx.Response(200))
    job_id = make_job(patch_worker_db).id

    deliver_webhook(str(job_id))

    refreshed = fresh_job(patch_worker_db, job_id)
    assert refreshed.status == "success"
    assert refreshed.attempt_count == 1
    log = patch_worker_db.query(DeliveryLog).one()
    assert log.attempt_number == 1
    assert log.status == "success"
    assert log.response_code == 200
    assert log.error_message is None
    assert enqueued == []


@respx.mock
def test_every_attempt_is_logged(patch_worker_db: Session, enqueued: list[int]) -> None:
    responses = iter([httpx.Response(500), httpx.Response(200)])
    respx.post(DESTINATION).mock(side_effect=lambda request: next(responses))
    job_id = make_job(patch_worker_db).id

    deliver_webhook(str(job_id))
    deliver_webhook(str(job_id))

    logs = patch_worker_db.query(DeliveryLog).order_by(DeliveryLog.attempt_number).all()
    assert [(log.attempt_number, log.status) for log in logs] == [
        (1, "retrying"),
        (2, "success"),
    ]
    assert enqueued == [2]


@respx.mock
def test_500_triggers_retry_with_2s_countdown(
    patch_worker_db: Session, enqueued: list[int]
) -> None:
    respx.post(DESTINATION).mock(return_value=httpx.Response(500))
    job_id = make_job(patch_worker_db).id

    deliver_webhook(str(job_id))

    assert fresh_job(patch_worker_db, job_id).status == "retrying"
    assert enqueued == [2]
    log = patch_worker_db.query(DeliveryLog).one()
    assert log.status == "retrying"
    assert log.response_code == 500


@respx.mock
def test_429_triggers_retry(patch_worker_db: Session, enqueued: list[int]) -> None:
    respx.post(DESTINATION).mock(return_value=httpx.Response(429))
    job_id = make_job(patch_worker_db).id

    deliver_webhook(str(job_id))

    assert fresh_job(patch_worker_db, job_id).status == "retrying"
    assert enqueued == [2]
    assert patch_worker_db.query(DeliveryLog).one().response_code == 429


@respx.mock
def test_timeout_triggers_retry_with_null_response_code(
    patch_worker_db: Session, enqueued: list[int]
) -> None:
    respx.post(DESTINATION).mock(side_effect=httpx.ReadTimeout("read timed out"))
    job_id = make_job(patch_worker_db).id

    deliver_webhook(str(job_id))

    assert fresh_job(patch_worker_db, job_id).status == "retrying"
    assert enqueued == [2]
    log = patch_worker_db.query(DeliveryLog).one()
    assert log.response_code is None
    assert log.error_message == "read timed out"


@respx.mock
def test_transport_error_triggers_retry(patch_worker_db: Session, enqueued: list[int]) -> None:
    respx.post(DESTINATION).mock(side_effect=httpx.ConnectError("connection refused"))
    job_id = make_job(patch_worker_db).id

    deliver_webhook(str(job_id))

    assert fresh_job(patch_worker_db, job_id).status == "retrying"
    assert enqueued == [2]
    assert "connection refused" in patch_worker_db.query(DeliveryLog).one().error_message


@respx.mock
def test_fails_twice_then_succeeds_with_2s_and_4s_countdowns(
    patch_worker_db: Session, enqueued: list[int]
) -> None:
    responses = iter([httpx.Response(500), httpx.Response(503), httpx.Response(200)])
    respx.post(DESTINATION).mock(side_effect=lambda request: next(responses))
    job_id = make_job(patch_worker_db).id

    for _ in range(3):
        deliver_webhook(str(job_id))

    refreshed = fresh_job(patch_worker_db, job_id)
    assert refreshed.status == "success"
    assert refreshed.attempt_count == 3
    assert enqueued == [2, 4]
    logs = patch_worker_db.query(DeliveryLog).order_by(DeliveryLog.attempt_number).all()
    assert [log.status for log in logs] == ["retrying", "retrying", "success"]


@respx.mock
def test_permanent_failure_exhausts_after_four_attempts(
    patch_worker_db: Session, enqueued: list[int]
) -> None:
    respx.post(DESTINATION).mock(return_value=httpx.Response(500))
    job_id = make_job(patch_worker_db).id

    for _ in range(4):
        deliver_webhook(str(job_id))

    refreshed = fresh_job(patch_worker_db, job_id)
    assert refreshed.status == "failed"
    assert refreshed.attempt_count == 4
    assert enqueued == [2, 4, 8]
    logs = patch_worker_db.query(DeliveryLog).order_by(DeliveryLog.attempt_number).all()
    assert len(logs) == 4
    assert [log.status for log in logs] == ["retrying", "retrying", "retrying", "failed"]


@respx.mock
def test_permanent_statuses_are_not_retried(patch_worker_db: Session, enqueued: list[int]) -> None:
    codes = iter([400, 404, 302])
    respx.post(DESTINATION).mock(side_effect=lambda request: httpx.Response(next(codes)))
    last_job_id: UUID = make_job(patch_worker_db).id

    for _ in range(3):
        last_job_id = make_job(patch_worker_db).id
        deliver_webhook(str(last_job_id))
        assert fresh_job(patch_worker_db, last_job_id).status == "failed"

    assert fresh_job(patch_worker_db, last_job_id).attempt_count == 1
    assert enqueued == []
    assert patch_worker_db.query(DeliveryLog).count() == 3


@respx.mock
def test_terminal_job_is_not_delivered(patch_worker_db: Session, enqueued: list[int]) -> None:
    job_id = make_job(patch_worker_db, status="success", attempt_count=1).id

    deliver_webhook(str(job_id))

    assert len(respx.calls) == 0
    assert patch_worker_db.query(DeliveryLog).count() == 0
    assert enqueued == []


@respx.mock
def test_outbound_headers_and_payload_are_sent(
    patch_worker_db: Session, enqueued: list[int]
) -> None:
    route = respx.post(DESTINATION).mock(return_value=httpx.Response(204))
    job_id = make_job(patch_worker_db).id

    deliver_webhook(str(job_id))

    request = route.calls[0].request
    assert request.headers["X-Webhook-Job-Id"] == f"job_{job_id}"
    assert request.headers["X-Webhook-Event-Id"] == "evt_retry"
    assert request.headers["X-Webhook-Attempt"] == "1"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == {"event": "test"}


def test_unexpected_exception_marks_job_failed(
    patch_worker_db: Session, enqueued: list[int], monkeypatch: MonkeyPatch
) -> None:
    def explode(job: WebhookJob, attempt: int) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("app.workers.tasks.dispatcher.send", explode)
    job_id = make_job(patch_worker_db).id

    deliver_webhook(str(job_id))

    assert fresh_job(patch_worker_db, job_id).status == "failed"
    log = patch_worker_db.query(DeliveryLog).one()
    assert log.status == "failed"
    assert log.error_message == "boom"
    assert enqueued == []


@respx.mock
def test_rate_limit_deferral_consumes_no_attempt(
    patch_worker_db: Session,
    scheduled: ScheduledCalls,
    enqueued: list[int],
    monkeypatch: MonkeyPatch,
) -> None:
    origins: list[str] = []

    def defer(origin: str) -> tuple[bool, float]:
        origins.append(origin)
        return False, 3.5

    monkeypatch.setattr("app.workers.tasks.rate_limiter.acquire", defer)
    job_id = make_job(patch_worker_db).id

    deliver_webhook(str(job_id))

    refreshed = fresh_job(patch_worker_db, job_id)
    assert refreshed.status == "queued"
    assert refreshed.attempt_count == 0
    assert patch_worker_db.query(DeliveryLog).count() == 0
    assert origins == ["https://example.com:443"]
    assert scheduled.job_ids == [str(job_id)]
    assert enqueued == [3.5]
    assert len(respx.calls) == 0


@respx.mock
def test_unsafe_destination_fails_before_an_attempt(
    patch_worker_db: Session,
    enqueued: list[int],
    monkeypatch: MonkeyPatch,
) -> None:
    def block(url: str, **kwargs: object) -> str:
        raise ValueError("private destinations are not allowed")

    monkeypatch.setattr("app.services.dispatcher.security.validate_destination_url", block)
    job_id = make_job(patch_worker_db).id

    deliver_webhook(str(job_id))

    refreshed = fresh_job(patch_worker_db, job_id)
    assert refreshed.status == "failed"
    assert refreshed.attempt_count == 0
    assert patch_worker_db.query(DeliveryLog).count() == 0
    assert enqueued == []
    assert len(respx.calls) == 0


def test_sweeper_requeues_stale_job(patch_worker_db: Session, scheduled: ScheduledCalls) -> None:
    stale = utcnow() - timedelta(seconds=300)
    job_id = make_job(patch_worker_db, status="retrying", attempt_count=1, updated_at=stale).id

    requeued = sweep_stale_jobs()

    refreshed = fresh_job(patch_worker_db, job_id)
    assert requeued == 1
    assert refreshed.status == "queued"
    assert refreshed.updated_at.replace(tzinfo=UTC) > stale
    assert scheduled.job_ids == [str(job_id)]


def test_sweeper_marks_exhausted_stale_job_failed(
    patch_worker_db: Session, scheduled: ScheduledCalls
) -> None:
    stale = utcnow() - timedelta(seconds=300)
    job_id = make_job(patch_worker_db, status="processing", attempt_count=4, updated_at=stale).id

    requeued = sweep_stale_jobs()

    assert requeued == 0
    assert fresh_job(patch_worker_db, job_id).status == "failed"
    assert scheduled.job_ids == []


def test_sweeper_ignores_terminal_jobs(patch_worker_db: Session, scheduled: ScheduledCalls) -> None:
    stale = utcnow() - timedelta(seconds=300)
    make_job(patch_worker_db, status="success", attempt_count=1, updated_at=stale)

    assert sweep_stale_jobs() == 0
    assert scheduled.job_ids == []
