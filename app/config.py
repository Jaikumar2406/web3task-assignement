"""Central configuration loaded once from the environment."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings sourced from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str
    redis_url: str
    http_timeout_seconds: float = 10.0
    max_retries: int = 3
    backoff_base_seconds: int = 2
    rate_limit_capacity: int = 5
    rate_limit_refill_per_second: float = 1.0
    extra_retryable_status_codes: str = ""
    allow_private_destinations: bool = False
    max_payload_bytes: int = 262_144
    sweeper_interval_seconds: int = 30
    stale_job_threshold_seconds: int = 120
    log_level: str = "INFO"

    @property
    def sqlalchemy_database_url(self) -> str:
        """Rewrite Render's postgres:// scheme into the SQLAlchemy psycopg2 form."""
        if self.database_url.startswith("postgres://"):
            return self.database_url.replace("postgres://", "postgresql+psycopg2://", 1)
        return self.database_url

    @property
    def extra_retryable_codes(self) -> frozenset[int]:
        """Parse the optional comma-separated list of extra retryable status codes."""
        values = (value.strip() for value in self.extra_retryable_status_codes.split(","))
        return frozenset(int(value) for value in values if value)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the single process-wide Settings instance."""
    return Settings()


settings = get_settings()
