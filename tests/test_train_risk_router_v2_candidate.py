from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.event_ledger import open_ledger
from scripts.train_risk_router_v2_candidate import (
    load_content_dataset,
    parse_official_feed,
    sanitize_publish_time_text,
)


def test_atom_and_rss_hard_negative_feeds_are_supported() -> None:
    atom = b"""<feed xmlns='http://www.w3.org/2005/Atom'><entry>
    <title>Routine product update</title><summary>New features are available.</summary>
    <link rel='alternate' href='https://example.test/atom'/><updated>2026-07-18T00:00:00Z</updated>
    </entry></feed>"""
    rss = b"""<rss><channel><item><title>Routine partnership</title>
    <description>Two companies announced a collaboration.</description>
    <link>https://example.test/rss</link><pubDate>Sat, 18 Jul 2026 00:00:00 GMT</pubDate>
    </item></channel></rss>"""
    assert parse_official_feed(atom)[0]["title"] == "Routine product update"
    assert parse_official_feed(rss)[0]["url"] == "https://example.test/rss"


def test_content_dataset_does_not_inject_taxonomy_or_discovery_source(tmp_path: Path) -> None:
    db = tmp_path / "ledger.sqlite3"
    connection = open_ledger(db)
    now = "2026-07-18T00:00:00+00:00"
    connection.execute(
        """INSERT INTO canonical_events(
           event_id,current_version,status,label_status,event_family,event_type,event_date,
           first_seen_at,last_updated_at,stable_id,ticker_at_event,company_name,manual_grade,
           provisional_grade_cap,discovery_source,no_trading
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        (
            "event-1", 1, "verified", "verified", "SECRET_FAMILY_MARKER",
            "SECRET_TYPE_MARKER", "2026-07-18", now, now, "issuer-1", "EXM",
            "Example Corp", "A", None, "sharadar_active_research",
        ),
    )
    connection.execute(
        """INSERT INTO event_versions(
           event_id,version,changed_at,status,label_status,event_family,event_type,
           manual_grade,facts_json,change_reason
        ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            "event-1", 1, now, "verified", "verified", "SECRET_FAMILY_MARKER",
            "SECRET_TYPE_MARKER", "A",
            json.dumps({"confirmed_facts": ["The company filed a complaint. SECRET_FAMILY_MARKER"]}),
            "test fixture",
        ),
    )
    connection.commit()
    connection.close()
    rows, metadata = load_content_dataset(db)
    assert len(rows) == 1
    assert "example corp" in rows[0]["text"]
    assert "the company filed a complaint" in rows[0]["text"]
    assert "SECRET_FAMILY_MARKER" not in rows[0]["text"]
    assert "SECRET_TYPE_MARKER" not in rows[0]["text"]
    assert "sharadar_active_research" not in rows[0]["text"]
    assert "event_family" in metadata["prohibited_model_features"]


def test_sanitizer_removes_internal_control_phrases() -> None:
    text = sanitize_publish_time_text(
        "Candidate official SEC notice for DISTRESS_EQUITY_DEATH",
        {"distress equity death"},
    )
    assert "candidate official" not in text
    assert "distress equity death" not in text
