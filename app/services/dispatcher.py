"""Outbound HTTP delivery and pure outcome classification."""

from __future__ import annotations

import time
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum

import httpx

from app.config import settings
from app.models.webhook import WebhookJob
from app.services import security

MAX_ERROR_CHARS = 1000


class OutcomeKind(StrEnum):
    """The three mutually exclusive result classes for a delivery attempt."""

    SUCCESS = "success"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"


@dataclass(frozen=True)
class Outcome:
    """Classified result of one outbound attempt, safe to persist or log."""

    kind: OutcomeKind
    response_code: int | None
    error_message: str | None
    latency_ms: int


def classify_status(status_code: int, extra_retryable: Collection[int] = ()) -> OutcomeKind:
    """Map an HTTP status code to an outcome class without performing I/O."""
    if 200 <= status_code < 300:
        return OutcomeKind.SUCCESS
    if status_code == 429 or 500 <= status_code < 600 or status_code in extra_retryable:
        return OutcomeKind.RETRYABLE
    return OutcomeKind.PERMANENT


def classify_exception(exc: Exception) -> OutcomeKind:
    """Map an HTTP client failure to a retryable or permanent outcome."""
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return OutcomeKind.RETRYABLE
    return OutcomeKind.PERMANENT


def truncate_error(message: str) -> str:
    """Bound stored error messages so no large response body is persisted."""
    return message[:MAX_ERROR_CHARS]


def send(job: WebhookJob, attempt: int) -> Outcome:
    """POST the stored payload once with explicit timeouts and no redirects."""
    security.validate_destination_url(job.destination_url)
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Job-Id": f"job_{job.id}",
        "X-Webhook-Event-Id": job.event_id,
        "X-Webhook-Attempt": str(attempt),
    }
    timeout = httpx.Timeout(
        connect=5.0,
        read=settings.http_timeout_seconds,
        write=5.0,
        pool=5.0,
    )
    started = time.monotonic()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            response = client.post(job.destination_url, json=job.payload, headers=headers)
    except httpx.HTTPError as exc:
        return Outcome(
            kind=classify_exception(exc),
            response_code=None,
            error_message=truncate_error(str(exc)),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
    return Outcome(
        kind=classify_status(response.status_code, settings.extra_retryable_codes),
        response_code=response.status_code,
        error_message=None,
        latency_ms=int((time.monotonic() - started) * 1000),
    )
