from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger, utc_now
import link_sec_issuer_assets as linker


def _add_event(connection, event_id: str, status: str, cik: int) -> None:
    now = utc_now()
    connection.execute(
        """INSERT INTO canonical_events VALUES (
           ?,1,?,?, 'earnings','earnings_or_guidance','2026-07-21',?,?,NULL,NULL,
           'Example Inc.',NULL,'A_P0_official_candidate','sec_current_filings',1)""",
        (event_id, status, status, now, now),
    )
    facts = {
        "canonical_url": f"https://sec.gov/Archives/edgar/data/{cik}/filing-index.htm",
        "source_title": f"8-K - Example Inc. ({cik:010d}) (Filer)",
    }
    connection.execute(
        """INSERT INTO event_versions VALUES (
           ?,1,?,?,?,'earnings','earnings_or_guidance',NULL,?,'fixture')""",
        (event_id, now, status, status, json.dumps(facts)),
    )
    connection.commit()


def _index_payload() -> bytes:
    return json.dumps(
        {
            "fields": ["cik", "name", "ticker", "exchange"],
            "data": [[1234, "Example Inc.", "EXM", "Nasdaq"]],
        }
    ).encode()


def test_candidate_gets_official_ticker_but_no_market_observation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        connection = open_ledger(root / "ledger.sqlite3")
        _add_event(connection, "FR-LIVE-candidate", "candidate", 1234)
        result = linker.link_sec_issuer_assets(
            connection,
            cache_dir=root / "cache",
            user_agent="FinanceRadar test@example.com",
            fetcher=lambda *_: _index_payload(),
        )
        event = connection.execute(
            "SELECT ticker_at_event FROM canonical_events WHERE event_id='FR-LIVE-candidate'"
        ).fetchone()
        impacts = connection.execute("SELECT COUNT(*) FROM event_asset_impacts").fetchone()[0]
        connection.close()
    assert result["mapped"] == 1
    assert event["ticker_at_event"] == "EXM"
    assert impacts == 0


def test_verified_event_gets_abstain_read_only_market_relation() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        connection = open_ledger(root / "ledger.sqlite3")
        _add_event(connection, "FR-LIVE-verified", "verified", 1234)
        result = linker.link_sec_issuer_assets(
            connection,
            cache_dir=root / "cache",
            user_agent="FinanceRadar test@example.com",
            fetcher=lambda *_: _index_payload(),
        )
        impact = connection.execute("SELECT * FROM event_asset_impacts").fetchone()
        connection.close()
    assert result["market_enabled"] == 1
    assert impact["direction"] == "ABSTAIN"
    assert impact["market_observation_allowed"] == 1
    assert impact["no_trading"] == 1
