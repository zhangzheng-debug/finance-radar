from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.audit_reader_visibility import (
    audit,
    connect_readonly,
    render_markdown,
)
from scripts.event_ledger import open_ledger


NOW = "2026-08-23T10:00:00+00:00"
PASSAGE = (
    "The company filed a voluntary petition for reorganization under Chapter 11 "
    "of the United States Bankruptcy Code."
)
CLAIMS = {
    "claim_subject": "Acme Corp",
    "claim_action": "filed for Chapter 11",
    "claim_stage": "FILED",
    "known_at": NOW,
}
SUMMARY = {"evidence_summary": "Acme Corp filed for Chapter 11 bankruptcy protection."}


def _seed(path: Path, rows: list[tuple[str, str, dict, bool, bool]]) -> None:
    connection = open_ledger(path)
    for source_id, tier in (("sec_current_filings", "P0"), ("aggregated_news", "P2")):
        connection.execute(
            "INSERT INTO sources(source_id,name,source_type,authority_tier,read_only,"
            "enabled,created_at,updated_at) VALUES(?,?,?,?,1,1,?,?)",
            (source_id, source_id, "official", tier, NOW, NOW),
        )
    for event_id, source_id, facts, has_subject, has_evidence in rows:
        connection.execute(
            "INSERT INTO canonical_events(event_id,current_version,status,label_status,"
            "event_family,event_type,event_date,first_seen_at,last_updated_at,"
            "ticker_at_event,company_name,discovery_source,no_trading)"
            " VALUES(?,1,'verified','verified','regulatory','bankruptcy','2026-08-20',?,?,?,?,?,1)",
            (
                event_id,
                NOW,
                NOW,
                "ACME" if has_subject else None,
                "Acme Corp" if has_subject else None,
                source_id,
            ),
        )
        connection.execute(
            "INSERT INTO event_versions(event_id,version,changed_at,status,label_status,"
            "event_family,event_type,facts_json,change_reason)"
            " VALUES(?,1,?,'verified','verified','regulatory','bankruptcy',?,'seed')",
            (event_id, NOW, json.dumps(facts)),
        )
        if has_evidence:
            observation_id = f"{event_id}-obs"
            connection.execute(
                "INSERT INTO raw_observations(observation_id,source_id,external_id,"
                "local_received_at,title,summary,content_sha256,raw_json,observation_status)"
                " VALUES(?,?,?,?,?,?,?,?,'active')",
                (observation_id, source_id, observation_id, NOW, "t", "s", "0" * 64, "{}"),
            )
            connection.execute(
                "INSERT INTO event_evidence(evidence_id,event_id,observation_id,evidence_url,"
                "evidence_passage,evidence_status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    f"{event_id}-e1",
                    event_id,
                    observation_id,
                    "https://www.sec.gov/example",
                    PASSAGE,
                    "confirmed_primary",
                    NOW,
                    NOW,
                ),
            )
    connection.commit()
    connection.close()


@pytest.fixture()
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.sqlite3"
    rows: list[tuple[str, str, dict, bool, bool]] = []
    for index in range(3):
        rows.append((f"ok-{index}", "sec_current_filings", {**SUMMARY, **CLAIMS}, True, True))
    for index in range(12):
        rows.append((f"sec-legacy-{index}", "sec_current_filings", dict(SUMMARY), True, True))
    for index in range(7):
        rows.append((f"news-legacy-{index}", "aggregated_news", dict(SUMMARY), True, True))
    for index in range(5):
        rows.append((f"noev-{index}", "aggregated_news", dict(SUMMARY), True, False))
    for index in range(2):
        rows.append((f"nosub-{index}", "aggregated_news", dict(SUMMARY), False, True))
    _seed(path, rows)
    return path


def _audit(path: Path) -> dict:
    connection = connect_readonly(path)
    try:
        return audit(connection)
    finally:
        connection.close()


def test_connect_readonly_refuses_writes(ledger: Path) -> None:
    connection = connect_readonly(ledger)
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM canonical_events")
            connection.commit()
    finally:
        connection.close()


def test_connect_readonly_requires_an_existing_database(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        connect_readonly(tmp_path / "missing.sqlite3")


def test_only_fully_qualified_events_are_reader_visible(ledger: Path) -> None:
    result = _audit(ledger)
    assert result["total_events"] == 29
    assert result["reader_visible"] == 3
    assert result["reader_hidden"] == 26


def test_claim_contract_is_reported_as_the_dominant_blocker(ledger: Path) -> None:
    counts = _audit(ledger)["condition_pass_counts"]
    assert counts["summary"]["failed"] == 0
    assert counts["subject"]["failed"] == 2
    assert counts["evidence"]["failed"] == 5
    for field in ("claim_subject", "claim_action", "claim_stage", "known_at"):
        assert counts[field]["failed"] == 26


def test_backlog_splits_machine_recoverable_from_human_work(ledger: Path) -> None:
    backlog = _audit(ledger)["claim_contract_backlog"]
    # Only events already carrying subject, evidence and summary count here, so
    # the five without evidence and the two without a subject are excluded.
    assert backlog["total"] == 19
    assert backlog["machine_recoverable"] == 12
    assert backlog["needs_human"] == 7


def test_events_missing_evidence_are_not_counted_as_recoverable(ledger: Path) -> None:
    result = _audit(ledger)
    combinations = {entry["missing"]: entry["events"] for entry in result["top_blocking_combinations"]}
    assert combinations["claim_subject + claim_action + claim_stage + known_at"] == 19
    assert (
        combinations["evidence + claim_subject + claim_action + claim_stage + known_at"] == 5
    )
    assert combinations["subject + claim_subject + claim_action + claim_stage + known_at"] == 2


def test_audit_never_reports_more_visible_than_total(ledger: Path) -> None:
    result = _audit(ledger)
    assert result["reader_visible"] + result["reader_hidden"] == result["total_events"]
    assert result["read_only"] is True


def test_empty_ledger_is_handled(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite3"
    _seed(path, [])
    result = _audit(path)
    assert result["total_events"] == 0
    assert result["reader_visible"] == 0
    assert result["top_blocking_combinations"] == []


def test_render_markdown_includes_the_backlog_split(ledger: Path) -> None:
    text = render_markdown(_audit(ledger))
    assert "公开可见（估计上界）：**3**" in text
    assert "确定性 enricher 可自动恢复：**12**" in text
    assert "需要人工：**7**" in text
