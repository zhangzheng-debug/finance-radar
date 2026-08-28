from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit


_BLOCKED_PUBLIC_SOURCE_HOSTS = frozenset(
    {
        "localhost",
        "metadata.google.internal",
    }
)
_BLOCKED_PUBLIC_SOURCE_SUFFIXES = (
    ".home.arpa",
    ".internal",
    ".lan",
    ".local",
    ".localdomain",
    ".localhost",
)


def public_source_url(value: Any) -> str | None:
    """Return a source URL only when it is safe to expose to public readers."""

    source_url = str(value or "").strip()
    if not source_url or len(source_url) > 2048:
        return None
    try:
        parsed = urlsplit(source_url)
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if port == 0:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    if (
        not host
        or "%" in host
        or host in _BLOCKED_PUBLIC_SOURCE_HOSTS
        or host.endswith(_BLOCKED_PUBLIC_SOURCE_SUFFIXES)
    ):
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.inet_aton(host))
        except (OSError, ValueError):
            address = None
    if address is not None and not address.is_global:
        return None
    if address is None and "." not in host:
        return None
    return source_url


def is_public_source_url(value: Any) -> bool:
    """Return whether ``value`` is a public, credential-free HTTP(S) URL."""

    return public_source_url(value) is not None


def preferred_public_source_url(
    sources: Iterable[Mapping[str, Any]],
) -> str | None:
    """Prefer a public P0/P1 source URL while preserving input order otherwise."""

    fallback: str | None = None
    for source in sources:
        safe_url = public_source_url(
            source.get("source_url") or source.get("canonical_url")
        )
        if not safe_url:
            continue
        authority = str(source.get("authority_tier") or "").strip().upper()
        if authority in {"P0", "P1"} or authority.startswith(("P0_", "P1_")):
            return safe_url
        if fallback is None:
            fallback = safe_url
    return fallback
