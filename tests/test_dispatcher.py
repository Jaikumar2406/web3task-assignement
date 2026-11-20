"""Pure classification tests for the outbound dispatcher."""

from __future__ import annotations

import httpx
import pytest

from app.services.dispatcher import (
    OutcomeKind,
    classify_exception,
    classify_status,
    truncate_error,
)


@pytest.mark.parametrize("status_code", [200, 201, 202, 204, 299])
def test_success_statuses(status_code: int) -> None:
    assert classify_status(status_code) == OutcomeKind.SUCCESS


@pytest.mark.parametrize("status_code", [429, 500, 502, 503, 504, 599])
def test_retryable_statuses(status_code: int) -> None:
    assert classify_status(status_code) == OutcomeKind.RETRYABLE


@pytest.mark.parametrize("status_code", [301, 302, 400, 401, 403, 404, 409, 422, 499])
def test_permanent_statuses(status_code: int) -> None:
    assert classify_status(status_code) == OutcomeKind.PERMANENT


def test_configured_extra_retryable_code_is_honored() -> None:
    assert classify_status(408, {408}) == OutcomeKind.RETRYABLE
    assert classify_status(404, {408}) == OutcomeKind.PERMANENT


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectTimeout("connect timed out"),
        httpx.ConnectError("connection refused"),
        httpx.RemoteProtocolError("broken response"),
    ],
)
def test_transport_failures_are_retryable(exc: httpx.HTTPError) -> None:
    assert classify_exception(exc) == OutcomeKind.RETRYABLE


def test_unexpected_exception_is_permanent() -> None:
    assert classify_exception(ValueError("bad input")) == OutcomeKind.PERMANENT


def test_error_messages_are_bounded() -> None:
    assert truncate_error("x" * 2000) == "x" * 1000
