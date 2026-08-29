from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.historical_primary_readmission import (
    apply_readmission_plan,
    build_readmission_authorization_template,
    build_readmission_plan,
    validate_readmission_plan,
)
from app.storage.ledger import LedgerRepository
from scripts.event_ledger import open_ledger, utc_now


def _seed(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    passage: str,
    status: str = "candidate",
    cik: str = "",
    url_cik: str = "",
) -> None:
    now = "2026-08-07T12:00:00+00:00"
    observation_id = f"obs-{event_id}"
    evidence_id = f"evidence-{event_id}"
    document_cik = cik or url_cik
    url = (
        f"https://www.sec.gov/Archives/edgar/data/{document_cik.lstrip('0')}/{event_id}.htm"
        if document_cik
        else f"https://www.sec.gov/Archives/{event_id}.htm"
    )
    content_sha = hashlib.sha256((event_id + passage).encode()).hexdigest()
    connection.execute(
        """INSERT INTO raw_observations(
               observation_id,source_id,external_id,source_published_at,
               local_received_at,title,summary,canonical_url,content_sha256,
               raw_json,observation_status
           ) VALUES(?, 'sec_current_filings', ?, ?, ?, ?, ?, ?, ?, ?, 'captured')""",
        (
            observation_id,
            event_id,
            now,
            now,
            "Example Corp officer update",
            passage,
            url,
            content_sha,
            json.dumps({"company": "Example Corp"}),
        ),
    )
    connection.execute(
        """INSERT INTO canonical_events VALUES(
               ?,1,?,?, 'governance','chief_financial_officer_appointment',
               '2026-08-07',?,?,NULL,'EXM','Example Corp',NULL,'A_P0',
               'sec_current_filings',1)""",
        (event_id, status, status, now, now),
    )
    connection.execute(
        """INSERT INTO event_versions VALUES(
               ?,1,?,?,?,'governance','chief_financial_officer_appointment',
               NULL,?,'legacy_fixture')""",
        (event_id, now, status, status, json.dumps({"cik": cik}) if cik else "{}"),
    )
    connection.execute(
        "INSERT INTO event_observations VALUES(?,?, 'primary',?)",
        (event_id, observation_id, now),
    )
    connection.execute(
        """INSERT INTO event_evidence VALUES(
               ?,?,?,?,'2026-08-07','8-K','5.02',?,NULL,95,
               'machine_extracted_unreviewed',0,?,?)""",
        (evidence_id, event_id, observation_id, url, passage, now, now),
    )


def _ledger(path: Path) -> Path:
    connection = open_ledger(path)
    now = utc_now()
    connection.execute(
        "INSERT INTO sources VALUES(?,?,'fixture',?,1,1,?,?)",
        ("sec_current_filings", "SEC", "P0_official", now, now),
    )
    _seed(
        connection,
        event_id="eligible",
        passage=(
            "On August 7, 2026, the Board of Directors of Example Corp "
            "appointed Jane Doe as the Company's Chief Financial Officer, "
            "effective August 27, 2026."
        ),
    )
    _seed(
        connection,
        event_id="unbound",
        passage="On August 7, 2026, the Board appointed Jane Doe as Chief Financial Officer.",
    )
    _seed(
        connection,
        event_id="formal",
        status="verified",
        passage=(
            "On August 7, 2026, the Board of Directors of Example Corp "
            "appointed Jane Doe as Chief Financial Officer."
        ),
    )
    connection.commit()
    connection.close()
    return path


def _backup(source: Path, target: Path) -> None:
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)


def _authorization(plan: dict, backup: Path) -> dict:
    auth = build_readmission_authorization_template(plan)
    auth.update(
        {
            "approved": True,
            "authorization_id": "test-authorization",
            "actor": "test-owner",
            "purpose": "test exact primary fact replay",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "backup_path": str(backup),
            "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        }
    )
    return auth


def test_plan_is_read_only_and_excludes_unbound_and_formal_rows(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    before = ledger.read_bytes()

    plan = build_readmission_plan(ledger)

    assert ledger.read_bytes() == before
    assert validate_readmission_plan(plan) == plan["plan_sha256"]
    assert plan["candidate_count"] == 1
    assert [row["event_id"] for row in plan["records"]] == ["eligible"]
    assert plan["records"][0]["before"]["status"] == "candidate"
    assert plan["records"][0]["no_human_verification_claim"] is True
    assert plan["blocked_reason_counts"]["FACT_SLOT_HAS_NO_ISSUER_BOUND_FACT"] >= 1


def test_plan_accepts_document_pronoun_only_after_exact_cik_match(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    with open_ledger(ledger) as connection:
        _seed(
            connection,
            event_id="cik-bound",
            passage="The Company appointed Jane Doe as Chief Financial Officer.",
            cik="0000123456",
        )
        connection.commit()

    plan = build_readmission_plan(ledger)
    by_id = {row["event_id"]: row for row in plan["records"]}

    assert "cik-bound" in by_id
    fact = by_id["cik-bound"]["facts"]["claim_fact_slots"]["facts"][0]
    assert fact["subject_binding"] == "DOCUMENT_ISSUER_CIK_MATCH"


def test_plan_recovers_legacy_cik_from_exact_linked_sec_url(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    with open_ledger(ledger) as connection:
        _seed(
            connection,
            event_id="legacy-url-cik-bound",
            passage="The Company appointed Jane Doe as Chief Financial Officer.",
            url_cik="0000123456",
        )
        connection.commit()

    plan = build_readmission_plan(ledger)
    by_id = {row["event_id"]: row for row in plan["records"]}

    assert "legacy-url-cik-bound" in by_id
    fact = by_id["legacy-url-cik-bound"]["facts"]["claim_fact_slots"]["facts"][0]
    assert fact["subject_binding"] == "DOCUMENT_ISSUER_CIK_MATCH"


def test_plan_rejects_legacy_cik_when_claim_and_evidence_urls_disagree(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    with open_ledger(ledger) as connection:
        _seed(
            connection,
            event_id="legacy-url-cik-mismatch",
            passage="The Company appointed Jane Doe as Chief Financial Officer.",
            url_cik="0000123456",
        )
        connection.execute(
            "UPDATE event_evidence SET evidence_url=? WHERE event_id=?",
            (
                "https://www.sec.gov/Archives/edgar/data/9999999/other.htm",
                "legacy-url-cik-mismatch",
            ),
        )
        connection.commit()

    plan = build_readmission_plan(ledger)

    assert "legacy-url-cik-mismatch" not in {
        row["event_id"] for row in plan["records"]
    }
    assert plan["blocked_reason_counts"]["SUBJECT_NOT_BOUND_TO_EVIDENCE"] >= 1


def test_apply_creates_new_reader_ready_version_without_verifying(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    plan = build_readmission_plan(ledger)
    backup = tmp_path / "backup.sqlite3"
    _backup(ledger, backup)

    result = apply_readmission_plan(
        ledger,
        plan,
        _authorization(plan, backup),
        execute=True,
    )

    assert result["applied"] == 1
    assert result["status_or_label_mutation"] is False
    with sqlite3.connect(ledger) as connection:
        connection.row_factory = sqlite3.Row
        event = connection.execute(
            "SELECT current_version,status,label_status,no_trading FROM canonical_events "
            "WHERE event_id='eligible'"
        ).fetchone()
        assert tuple(event) == (2, "candidate", "candidate", 1)
        facts = json.loads(
            connection.execute(
                "SELECT facts_json FROM event_versions WHERE event_id='eligible' AND version=2"
            ).fetchone()[0]
        )
        assert facts["admission_contract_version"] == "event-admission-v3"
        assert facts["formal_verification"] is False
        assert connection.execute(
            "SELECT COUNT(*) FROM event_evidence_relations "
            "WHERE event_id='eligible' AND event_version=2 AND relation_status='SCOPED_MATCH'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM historical_primary_readmission_audit"
        ).fetchone()[0] == 1
    quality = LedgerRepository(ledger).overview(run_integrity_check=False)["reader_quality"]
    assert quality["citation_ready"] == 1


def test_apply_rejects_stale_or_unbacked_authorization(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    plan = build_readmission_plan(ledger)
    backup = tmp_path / "backup.sqlite3"
    _backup(ledger, backup)
    authorization = _authorization(plan, backup)
    authorization["backup_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="backup_sha256"):
        apply_readmission_plan(ledger, plan, authorization, execute=True)

    assert apply_readmission_plan(ledger, plan, execute=False)["ready_to_apply"] == 1
