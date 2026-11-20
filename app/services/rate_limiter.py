"""Atomic per-destination token bucket backed by Redis Lua."""

from __future__ import annotations

import logging

import redis

from app.config import settings
from app.services.redis_client import redis_client
from app.services.security import normalize_origin

logger = logging.getLogger(__name__)

LUA_TOKEN_BUCKET = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1e6
local cap, rate = tonumber(ARGV[1]), tonumber(ARGV[2])

local d = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens, ts = tonumber(d[1]), tonumber(d[2])
if tokens == nil then tokens, ts = cap, now end

tokens = math.min(cap, tokens + (now - ts) * rate)

local allowed, wait = 0, 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1
else wait = (1 - tokens) / rate end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], math.ceil(cap / rate) * 2)
return {allowed, tostring(wait)}
"""


def bucket_key(origin: str) -> str:
    """Return the Redis key for a normalized destination origin."""
    return f"rl:{normalize_origin(origin)}"


def acquire(origin: str) -> tuple[bool, float]:
    """Atomically consume one token or return the wait; fail closed on Redis errors."""
    key = bucket_key(origin)
    try:
        result = redis_client.eval(
            LUA_TOKEN_BUCKET,
            1,
            key,
            settings.rate_limit_capacity,
            settings.rate_limit_refill_per_second,
        )
        allowed = int(result[0]) == 1
        wait = float(result[1])
    except (redis.RedisError, TypeError, ValueError, IndexError):
        logger.warning("rate limiter unavailable; deferring", extra={"origin": origin})
        return False, float(settings.backoff_base_seconds)
    return allowed, wait
