"""Pure backoff and retry-decision functions."""

from __future__ import annotations


def backoff_seconds(failed_attempt: int, base_seconds: int) -> int:
    """Return the exponential delay after a given 1-based attempt failed."""
    return base_seconds * 2 ** (failed_attempt - 1)


def should_retry(failed_attempt: int, max_retries: int) -> bool:
    """Report whether another delivery attempt is allowed after this failure."""
    return failed_attempt <= max_retries
