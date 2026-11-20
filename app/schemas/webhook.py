"""Pydantic request and response schemas for the dispatch API."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator
from pydantic.alias_generators import to_camel

from app.config import settings
from app.services.security import validate_destination_url


class DispatchRequest(BaseModel):
    """Validated incoming event; public field names are camelCase."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    destination_url: HttpUrl
    event_id: str = Field(min_length=1, max_length=255)
    payload: dict[str, Any]

    @field_validator("destination_url")
    @classmethod
    def check_destination_url(cls, value: HttpUrl) -> HttpUrl:
        """Reject unsafe hosts and malformed URL shapes before any DB work."""
        try:
            validate_destination_url(str(value), resolve=False)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("payload")
    @classmethod
    def limit_payload_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Reject oversized payloads before any database work happens."""
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        if len(encoded) > settings.max_payload_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="payload too large",
            )
        return value


class DispatchResponse(BaseModel):
    """Acknowledgement returned immediately after the job is persisted."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    status: str
    job_id: str


class AttemptResponse(BaseModel):
    """One delivery attempt as exposed by the status endpoint."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    attempt_number: int
    status: str
    response_code: int | None
    error_message: str | None
    attempted_at: datetime


class JobStatusResponse(BaseModel):
    """Job state plus its ordered attempt history."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    job_id: str
    event_id: str
    destination_url: str
    status: str
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    attempts: list[AttemptResponse]
