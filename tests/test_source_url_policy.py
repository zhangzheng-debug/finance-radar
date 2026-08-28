from __future__ import annotations

import pytest

from app.source_url_policy import (
    is_public_source_url,
    preferred_public_source_url,
    public_source_url,
)


@pytest.mark.parametrize(
    "value",
    (
        "https://www.sec.gov/Archives/example.htm",
        "http://example.com/story",
        "https://[2606:4700:4700::1111]/story",
    ),
)
def test_public_source_url_accepts_public_http_urls(value: str) -> None:
    assert public_source_url(value) == value
    assert is_public_source_url(value) is True


@pytest.mark.parametrize(
    "value",
    (
        None,
        "",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https://user:secret@example.com/story",
        "http://localhost/private",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://intranet/private",
        "http://service.internal/private",
        "http://printer.local/private",
        "http://127.0.0.1/private",
        "http://127.1/private",
        "http://2130706433/private",
        "http://0x7f000001/private",
        "http://0177.0.0.1/private",
        "http://10.1.2.3/private",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/private",
        "https://example.com:99999/story",
        "https://example.com:0/story",
        "https://example.com/" + "a" * 2049,
    ),
)
def test_public_source_url_rejects_non_public_or_credentialed_urls(value: object) -> None:
    assert public_source_url(value) is None
    assert is_public_source_url(value) is False


def test_preferred_public_source_url_prefers_primary_authority() -> None:
    sources = [
        {
            "authority_tier": "P2",
            "source_url": "https://news.example/story",
        },
        {
            "authority_tier": "P0_official",
            "source_url": "https://regulator.example/notice",
        },
    ]

    assert preferred_public_source_url(sources) == (
        "https://regulator.example/notice"
    )


def test_preferred_public_source_url_ignores_unsafe_primary_authority() -> None:
    sources = [
        {"authority_tier": "P0", "source_url": "http://127.0.0.1/private"},
        {"authority_tier": "P2", "source_url": "https://news.example/story"},
    ]

    assert preferred_public_source_url(sources) == "https://news.example/story"
