"""Token-bucket limiter tests; Lua behavior is checked against real Redis."""

from __future__ import annotations

from collections.abc import Generator

import pytest
import redis

from app.config import settings
from app.services.rate_limiter import acquire
from app.services.redis_client import redis_client, redis_is_available

requires_redis = pytest.mark.skipif(
    not redis_is_available(), reason="requires the compose Redis service"
)


@pytest.fixture
def clean_buckets() -> Generator[None, None, None]:
    """Give each integration test an isolated set of rate-limit keys."""

    def flush() -> None:
        keys = redis_client.keys("rl:*test-*")
        if keys:
            redis_client.delete(*keys)

    flush()
    yield
    flush()


@requires_redis
def test_token_bucket_blocks_excess_and_returns_wait(
    clean_buckets: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rate_limit_capacity", 2)
    monkeypatch.setattr(settings, "rate_limit_refill_per_second", 0.1)
    origin = "http://test-limiter:80"

    assert acquire(origin) == (True, 0.0)
    assert acquire(origin)[0] is True
    allowed, wait = acquire(origin)

    assert allowed is False
    assert 0 < wait <= 10.5


@requires_redis
def test_different_origins_have_independent_buckets(
    clean_buckets: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "rate_limit_capacity", 1)
    monkeypatch.setattr(settings, "rate_limit_refill_per_second", 0.01)

    assert acquire("http://test-first:80")[0] is True
    assert acquire("http://test-second:80")[0] is True


def test_redis_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_connection_error(*args: object, **kwargs: object) -> None:
        raise redis.ConnectionError("redis unavailable")

    monkeypatch.setattr("app.services.rate_limiter.redis_client.eval", raise_connection_error)

    assert acquire("http://first.test:80") == (False, float(settings.backoff_base_seconds))
