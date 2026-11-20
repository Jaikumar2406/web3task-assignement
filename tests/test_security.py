"""URL normalization and SSRF guard tests."""

from __future__ import annotations

from typing import Any

import pytest
from pytest import MonkeyPatch

from app.config import settings
from app.services.security import normalize_origin, validate_destination_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/hook", "https://example.com:443"),
        ("http://Example.COM:8080/hook", "http://example.com:8080"),
        ("http://[::1]:9000/hook", "http://[::1]:9000"),
    ],
)
def test_normalize_origin(url: str, expected: str) -> None:
    assert normalize_origin(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",
        "http://10.0.0.5/hook",
        "http://169.254.169.254/latest/meta-data",
        "http://localhost/hook",
        "http://user:pass@example.com/hook",
        "https://example.com/" + "a" * 3000,
    ],
)
def test_unsafe_urls_are_rejected_without_dns(url: str) -> None:
    with pytest.raises(ValueError):
        validate_destination_url(url, resolve=False)


def test_public_literal_url_is_allowed_without_dns() -> None:
    assert validate_destination_url("https://8.8.8.8/hook", resolve=False) == (
        "https://8.8.8.8/hook"
    )


def test_hostname_resolving_to_private_ip_is_rejected(monkeypatch: MonkeyPatch) -> None:
    def fake_getaddrinfo(*args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        return [(2, 1, 6, "", ("10.0.0.7", 0))]

    monkeypatch.setattr("app.services.security.socket.getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="private destinations"):
        validate_destination_url("https://callback.example/hook")


def test_private_flag_allows_loopback(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "allow_private_destinations", True)

    assert validate_destination_url("http://127.0.0.1:8000/hook") == ("http://127.0.0.1:8000/hook")
