"""Shared Redis client for health checks and the token bucket."""

from __future__ import annotations

import redis

from app.config import settings

redis_client = redis.Redis.from_url(
    settings.redis_url,
    decode_responses=True,
    socket_connect_timeout=1,
    socket_timeout=1,
)


def redis_is_available() -> bool:
    """Return True when Redis answers PING, False on any connection error."""
    try:
        return bool(redis_client.ping())
    except redis.RedisError:
        return False
