from __future__ import annotations

import sqlite3
from pathlib import Path

from app.models.event_playbook import cards_for_family, time_anchor_for_family
from app.models.event_taxonomy import classify_event, load_taxonomy, taxonomy_coverage
from scripts.audit_event_taxonomy import build_report


def test_taxonomy_maps_collector_aliases_to_one_product_category() -> None:
    assert classify_event("earnings", "operating_results").category == "EARNINGS_AND_GUIDANCE"
    assert classify_event("earnings", "operating_results").playbook_family == "earnings_or_guidance"
    assert classify_event("bankruptcy_or_distress", "chapter_11").category == "BANKRUPTCY_INSOLVENCY"
    assert classify_event("listing_compliance", "minimum_bid_price_deficiency").category == "LISTING_COMPLIANCE"
    assert classify_event("price_crash", "one_day_crash").fact_event is False
    assert classify_event("identity_control", "ticker_mapping").fact_event is False


def test_playbook_and_price_anchor_consume_the_same_alias_mapping() -> None:
    assert cards_for_family("earnings")
    assert {card.event_family for card in cards_for_family("earnings")} == {
        "earnings_or_guidance"
    }
    assert time_anchor_for_family("earnings", "operating_results") == "source_published"
    assert time_anchor_for_family("listing_compliance", "minimum_bid_price_deficiency") == "source_published"
    assert cards_for_family("equity_dilution", "atm_offering") == ()
    assert time_anchor_for_family("equity_dilution", "atm_offering") == "source_published"


def test_taxonomy_reports_unknowns_instead_of_silently_guessing() -> None:
    report = taxonomy_coverage(
        [
            {"event_id": "known", "event_family": "governance", "event_type": "management_change"},
            {"event_id": "unknown", "event_family": "mystery", "event_type": "opaque"},
        ]
    )
    assert report["mapped"] == 1
    assert report["unmapped"] == 1
    assert report["coverage_pct"] == 50.0
    assert report["unmapped_examples"][0]["event_id"] == "unknown"


def test_repository_snapshot_taxonomy_coverage_is_at_least_95_percent() -> None:
    db_path = Path("data/finance_radar.sqlite3")
    if not db_path.is_file():
        return
    report = build_report(db_path)
    assert report["coverage_pct"] >= 95.0, report["unmapped_examples"][:20]


def test_taxonomy_config_has_ordered_rules_and_explicit_default() -> None:
    payload = load_taxonomy()
    assert payload["default_category"] == "OTHER_UNMAPPED"
    priorities = [int(rule["priority"]) for rule in payload["rules"]]
    assert priorities == sorted(priorities)
    assert len(priorities) == len(set(priorities))
