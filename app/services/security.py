"""URL normalization and SSRF checks for outbound webhooks."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from app.config import settings

ALLOWED_SCHEMES = {"http", "https"}
MAX_URL_LENGTH = 2048


def normalize_origin(url: str) -> str:
    """Return the canonical ``scheme://host:port`` key used for rate limiting."""
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise ValueError("destination URL must use http or https")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("destination URL has no host")
    host = hostname.lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return f"{scheme}://{host}:{port}"


def _address_is_blocked(address: str) -> bool:
    """Report whether an IP literal belongs to a forbidden SSRF range."""
    ip = ipaddress.ip_address(address)
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_reserved,
            ip.is_multicast,
            ip.is_unspecified,
        )
    )


def _resolve_addresses(hostname: str) -> set[str]:
    """Resolve a hostname to every IP address, raising on DNS failure."""
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("could not resolve destination host") from exc
    return {info[4][0] for info in infos}


def validate_destination_url(url: str, *, resolve: bool = True) -> str:
    """Validate a destination, rejecting SSRF-prone targets unless explicitly allowed."""
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("destination URL is too long")
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise ValueError("destination URL must use http or https")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("destination URL has no host")
    if parsed.username or parsed.password:
        raise ValueError("destination URL must not contain credentials")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("destination URL has an invalid port") from exc

    if settings.allow_private_destinations:
        return url

    host = hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("private destinations are not allowed")
    try:
        addresses = {str(ipaddress.ip_address(host))}
    except ValueError:
        addresses = _resolve_addresses(host) if resolve else set()
    if any(_address_is_blocked(address) for address in addresses):
        raise ValueError("private destinations are not allowed")
    return url
