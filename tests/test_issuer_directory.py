from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.models.issuer_directory import IssuerDirectory, load_issuer_directory


def _document() -> dict[str, object]:
    return {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [1045810, "NVIDIA CORP", "NVDA", "Nasdaq"],
            [320193, "Apple Inc.", "AAPL", "Nasdaq"],
        ],
    }


def test_load_directory_is_content_addressed(tmp_path: Path) -> None:
    path = tmp_path / "company_tickers_exchange.json"
    payload = json.dumps(_document()).encode("utf-8")
    path.write_bytes(payload)

    directory = load_issuer_directory(path)

    assert directory is not None
    assert directory.record_count == 2
    assert directory.source_sha256 == hashlib.sha256(payload).hexdigest()


def test_missing_directory_disables_resolution_without_fabricating_data(
    tmp_path: Path,
) -> None:
    assert load_issuer_directory(tmp_path / "missing.json") is None


def test_malformed_directory_fails_closed() -> None:
    with pytest.raises(ValueError, match="missing required fields"):
        IssuerDirectory.from_document({"fields": ["name"], "data": []})


def test_company_name_must_lead_the_captured_text() -> None:
    directory = IssuerDirectory.from_document(_document())

    assert (
        directory.resolve(
            {
                "event_family": "earnings",
                "event_type": "earnings_or_guidance",
                "source_title": "US stocks rise while NVIDIA prepares to report",
            }
        )
        is None
    )


def test_incidental_cashtag_does_not_become_the_event_subject() -> None:
    directory = IssuerDirectory.from_document(_document())

    assert (
        directory.resolve(
            {
                "event_family": "earnings",
                "event_type": "earnings_or_guidance",
                "source_title": "OpenAI investment discussed in a $NVDA earnings preview",
            }
        )
        is None
    )
