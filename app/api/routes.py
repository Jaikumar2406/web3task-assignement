"""Public HTTP routes for health, ingestion and job status."""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.database.connection import database_is_available, get_db
from app.models.webhook import WebhookJob
from app.schemas.webhook import (
    AttemptResponse,
    DispatchRequest,
    DispatchResponse,
    JobStatusResponse,
)
from app.services.redis_client import redis_is_available
from app.workers.tasks import deliver_webhook

logger = logging.getLogger(__name__)
health_router = APIRouter()
webhooks_router = APIRouter(prefix="/api/webhooks")

JOB_ID_PREFIX = "job_"


def parse_job_id(job_id: str) -> UUID:
    """Strip the public prefix and reject anything that is not a UUID."""
    if not job_id.startswith(JOB_ID_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="malformed job id"
        )
    try:
        return UUID(job_id.removeprefix(JOB_ID_PREFIX))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="malformed job id",
        ) from exc


@health_router.get("/health")
def health() -> JSONResponse:
    """Report liveness by probing PostgreSQL and Redis for Render health checks."""
    database_ok = database_is_available()
    redis_ok = redis_is_available()

    if not database_ok:
        logger.warning("health check: database unavailable")
    if not redis_ok:
        logger.warning("health check: redis unavailable")

    healthy = database_ok and redis_ok
    return JSONResponse(
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ok" if healthy else "unavailable"},
    )


@webhooks_router.post(
    "/dispatch",
    response_model=DispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def dispatch(
    request: DispatchRequest,
    db: Annotated[Session, Depends(get_db)],
) -> DispatchResponse:
    """Persist then enqueue a job; never wait for the destination to respond."""
    job = WebhookJob(
        event_id=request.event_id,
        destination_url=str(request.destination_url),
        payload=request.payload,
        status="queued",
    )
    db.add(job)
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.error("failed to persist webhook job", extra={"event_id": request.event_id})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable",
        ) from exc

    public_id = f"{JOB_ID_PREFIX}{job.id}"
    try:
        deliver_webhook.apply_async(args=[str(job.id)])
    except Exception as exc:
        logger.error("failed to enqueue delivery", extra={"job_id": public_id})
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="queue unavailable",
        ) from exc

    logger.info("webhook job queued", extra={"job_id": public_id, "event_id": request.event_id})
    return DispatchResponse(status="queued", job_id=public_id)


@webhooks_router.get("/jobs/{job_id}", response_model=JobStatusResponse)
def get_job(job_id: str, db: Annotated[Session, Depends(get_db)]) -> JobStatusResponse:
    """Return a job and its ordered attempts, or 404 when the id is unknown."""
    parsed_id = parse_job_id(job_id)
    job = db.get(WebhookJob, parsed_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")

    return JobStatusResponse(
        job_id=f"{JOB_ID_PREFIX}{job.id}",
        event_id=job.event_id,
        destination_url=job.destination_url,
        status=job.status,
        attempt_count=job.attempt_count,
        created_at=job.created_at,
        updated_at=job.updated_at,
        attempts=[
            AttemptResponse(
                attempt_number=log.attempt_number,
                status=log.status,
                response_code=log.response_code,
                error_message=log.error_message,
                attempted_at=log.attempted_at,
            )
            for log in job.logs
        ],
    )
