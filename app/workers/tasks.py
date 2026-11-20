"""Celery task definitions for delivery, rate limiting and recovery."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from app.config import settings
from app.database.connection import SessionLocal, utcnow
from app.models.delivery_log import DeliveryLog
from app.models.webhook import WebhookJob
from app.services import dispatcher, rate_limiter, retry, security
from app.services.dispatcher import Outcome, OutcomeKind, truncate_error
from app.workers.celery_app import celery_app

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
TERMINAL_STATUSES = {"success", "failed"}
STALE_STATUSES = {"queued", "retrying", "processing"}


def _write_log(db: Session, job: WebhookJob, attempt: int, outcome: Outcome, status: str) -> None:
    """Record one attempt; the caller commits it atomically with the job status."""
    db.add(
        DeliveryLog(
            job_id=job.id,
            attempt_number=attempt,
            status=status,
            response_code=outcome.response_code,
            error_message=outcome.error_message,
        )
    )


def _emit(
    event: str,
    level: int,
    job_id: str,
    event_id: str,
    attempt: int,
    origin: str,
    outcome: Outcome,
    status: str,
    delay: int | None = None,
) -> None:
    """Emit one structured log line for a classified delivery attempt."""
    logger.log(
        level,
        event,
        extra={
            "job_id": job_id,
            "event_id": event_id,
            "status": status,
            "attempt": attempt,
            "origin": origin,
            "response_code": outcome.response_code,
            "error_message": outcome.error_message,
            "latency_ms": outcome.latency_ms,
            "next_delay_seconds": delay,
        },
    )


def _fail_before_attempt(db: Session, job: WebhookJob, public_id: str, message: str) -> None:
    """Terminalize a job that cannot be sent, without creating a phantom attempt."""
    job.status = "failed"
    db.commit()
    logger.error(
        "webhook.unsendable",
        extra={
            "job_id": public_id,
            "event_id": job.event_id,
            "status": "failed",
            "error_message": truncate_error(message),
        },
    )


def _rate_limit_allows(
    db: Session, job: WebhookJob, job_id: str, public_id: str, origin: str
) -> bool:
    """Claim a token or reschedule the job without consuming a delivery attempt."""
    allowed, wait = rate_limiter.acquire(origin)
    if allowed:
        return True
    job.status = "queued"
    job.updated_at = utcnow()
    db.commit()
    logger.warning(
        "webhook.rate_limited",
        extra={
            "job_id": public_id,
            "event_id": job.event_id,
            "status": "queued",
            "origin": origin,
            "wait_seconds": wait,
        },
    )
    deliver_webhook.apply_async(args=[job_id], countdown=max(1.0, wait))
    return False


def _begin_attempt(db: Session, job: WebhookJob) -> int:
    """Claim one attempt in the durable database before calling the destination."""
    job.attempt_count += 1
    attempt = job.attempt_count
    job.status = "processing"
    db.commit()
    return attempt


def _send_safely(job: WebhookJob, attempt: int) -> Outcome:
    """Turn any unexpected send-time exception into a permanent outcome."""
    try:
        return dispatcher.send(job, attempt)
    except Exception as exc:  # noqa: BLE001 - a failed attempt must never stay processing
        return Outcome(
            kind=OutcomeKind.PERMANENT,
            response_code=None,
            error_message=truncate_error(str(exc)),
            latency_ms=0,
        )


def _finish_attempt(
    db: Session,
    job: WebhookJob,
    job_id: str,
    public_id: str,
    origin: str,
    attempt: int,
    outcome: Outcome,
) -> None:
    """Log the outcome, update the job atomically and schedule any next step."""
    if outcome.kind == OutcomeKind.SUCCESS:
        _write_log(db, job, attempt, outcome, "success")
        job.status = "success"
        db.commit()
        _emit(
            "webhook.delivered",
            logging.INFO,
            public_id,
            job.event_id,
            attempt,
            origin,
            outcome,
            "success",
        )
        return

    if outcome.kind == OutcomeKind.RETRYABLE and retry.should_retry(attempt, settings.max_retries):
        delay = retry.backoff_seconds(attempt, settings.backoff_base_seconds)
        _write_log(db, job, attempt, outcome, "retrying")
        job.status = "retrying"
        db.commit()
        _emit(
            "webhook.retry_scheduled",
            logging.WARNING,
            public_id,
            job.event_id,
            attempt,
            origin,
            outcome,
            "retrying",
            delay,
        )
        deliver_webhook.apply_async(args=[job_id], countdown=delay)
        return

    _write_log(db, job, attempt, outcome, "failed")
    job.status = "failed"
    db.commit()
    _emit(
        "webhook.delivery_failed",
        logging.ERROR,
        public_id,
        job.event_id,
        attempt,
        origin,
        outcome,
        "failed",
    )


def _deliver_claimed_job(db: Session, job: WebhookJob, job_id: str) -> None:
    """Run one claimed job through rate limiting, sending and outcome handling."""
    public_id = f"job_{job.id}"
    try:
        security.validate_destination_url(job.destination_url)
        origin = security.normalize_origin(job.destination_url)
    except ValueError as exc:
        _fail_before_attempt(db, job, public_id, str(exc))
        return

    if not _rate_limit_allows(db, job, job_id, public_id, origin):
        return

    if job.attempt_count >= settings.max_retries + 1:
        _fail_before_attempt(db, job, public_id, "retry budget exhausted before send")
        return

    attempt = _begin_attempt(db, job)
    outcome = _send_safely(job, attempt)
    _finish_attempt(db, job, job_id, public_id, origin, attempt, outcome)


@celery_app.task(name="deliver_webhook")
def deliver_webhook(job_id: str) -> None:
    """Deliver one persisted job, log the attempt and schedule the next step."""
    try:
        parsed_id = UUID(job_id)
    except ValueError:
        logger.error("webhook.invalid_task_id", extra={"job_id": job_id})
        return

    with SessionLocal() as db:
        job = db.get(WebhookJob, parsed_id, with_for_update=True)
        if job is None or job.status in TERMINAL_STATUSES:
            logger.info(
                "webhook.skipped",
                extra={"job_id": job_id, "status": None if job is None else job.status},
            )
            return
        _deliver_claimed_job(db, job, job_id)


@celery_app.task(name="sweep_stale_jobs")
def sweep_stale_jobs() -> int:
    """Re-enqueue stale non-terminal jobs recovered from the durable database."""
    cutoff = utcnow() - timedelta(seconds=settings.stale_job_threshold_seconds)
    requeued = 0
    with SessionLocal() as db:
        jobs = db.scalars(
            select(WebhookJob)
            .where(WebhookJob.status.in_(STALE_STATUSES), WebhookJob.updated_at < cutoff)
            .order_by(WebhookJob.updated_at)
            .limit(100)
            .with_for_update(skip_locked=True)
        ).all()
        for job in jobs:
            public_id = f"job_{job.id}"
            if job.attempt_count >= settings.max_retries + 1:
                job.status = "failed"
                db.commit()
                logger.error(
                    "sweeper.job_failed",
                    extra={"job_id": public_id, "event_id": job.event_id, "status": "failed"},
                )
                continue
            job.status = "queued"
            db.commit()
            try:
                deliver_webhook.apply_async(args=[str(job.id)])
            except Exception as exc:  # noqa: BLE001 - broker outage must not stop the sweep
                logger.error(
                    "sweeper.enqueue_failed",
                    extra={"job_id": public_id, "event_id": job.event_id, "error": str(exc)},
                )
                continue
            requeued += 1
            logger.info(
                "sweeper.job_requeued",
                extra={"job_id": public_id, "event_id": job.event_id, "status": "queued"},
            )
    return requeued
