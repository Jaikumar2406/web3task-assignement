"""Pure backoff and retry-window tests."""

from __future__ import annotations

import pytest

from app.services.retry import backoff_seconds, should_retry


@pytest.mark.parametrize(
    ("failed_attempt", "expected"),
    [(1, 2), (2, 4), (3, 8)],
)
def test_backoff_is_2_4_8_seconds(failed_attempt: int, expected: int) -> None:
    assert backoff_seconds(failed_attempt, 2) == expected


def test_backoff_uses_configured_base() -> None:
    assert backoff_seconds(2, 5) == 10


@pytest.mark.parametrize(
    ("failed_attempt", "expected"),
    [(1, True), (2, True), (3, True), (4, False)],
)
def test_three_retries_are_allowed(failed_attempt: int, expected: bool) -> None:
    assert should_retry(failed_attempt, 3) is expected
