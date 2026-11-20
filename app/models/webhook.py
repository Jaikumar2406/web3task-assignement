"""Persistent webhook job table definition."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.connection import Base, utcnow

if TYPE_CHECKING:
    from app.models.delivery_log import DeliveryLog


class WebhookJob(Base):
    """A single accepted event plus the state of its delivery attempts."""

    __tablename__ = "webhook_jobs"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    event_id: Mapped[str] = mapped_column(String(255), index=True)
    destination_url: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    status: Mapped[str] = mapped_column(String(20), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
        index=True,
    )

    logs: Mapped[list[DeliveryLog]] = relationship(
        back_populates="job",
        order_by="DeliveryLog.attempt_number",
    )

    __table_args__ = (Index("ix_webhook_jobs_status_updated_at", "status", "updated_at"),)
