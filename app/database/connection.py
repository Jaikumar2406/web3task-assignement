"""Database engine, session factory and health probe."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

engine = create_engine(settings.sqlalchemy_database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp for every status change."""
    return datetime.now(UTC)


def init_db() -> None:
    """Create missing tables at startup; this project deliberately has no migrations."""
    from app.models.delivery_log import DeliveryLog
    from app.models.webhook import WebhookJob

    Base.metadata.create_all(bind=engine, tables=[WebhookJob.__table__, DeliveryLog.__table__])


def get_db() -> Generator[Session, None, None]:
    """Yield a session that is closed even when the request fails."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def database_is_available() -> bool:
    """Check PostgreSQL connectivity without relying on application tables."""
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except SQLAlchemyError:
        return False
    return True
