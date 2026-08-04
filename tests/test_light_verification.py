from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.models.risk_router import derive_evidence_context
from app.services.light_verification import (
    LIGHT_FOLLOWUP_JOB_TYPE,
    LIGHT_VERIFICATION_VERSION,
    apply_event,
    evaluate_event,
    model_delta,
    reconcile_legacy_event,
)
from app.storage.operations import OperationsRepository
from app.workers.continuous import ROOT as WORKER_ROOT
from app.workers.continuous import execute_cycle
from scripts.event_ledger import open_ledger, stable_json, utc_now


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
import light_verify


def _event(event_type: str = "delisted") -> dict:
    return {
        "event_id": "evt-light-1",
        "current_version": 1,
        "status": "candidate",
        "label_status": "candidate",
        "event_family": "delisting_or_suspension" if event_type == "delisted" else "price_crash",
        "event_type": event_type,
        "event_date": "2025-01-02",
        "company_name": "ACME HOLDINGS INC",
        "ticker_at_event": "ACME",
        "manual_grade": None,
        "facts": {"evidence_summary": "ACME reported a delisting event."},
    }


def _evidence(passage: str, *, keywords: str = "delist") -> list[dict]:
    return [
        {
            "evidence_id": "ev-light-1",
            "evidence_status": "candidate_passage",
            "authority_tier": "P0",
            "evidence_url": "https://www.sec.gov/Archives/acme.htm",
            "filing_date": "2025-01-03",
            "source_published_at": "2025-01-03",
            "observation_title": "SEC ACME",
            "observation_summary": "ACME HOLDINGS INC",
            "evidence_passage": passage,
            "matched_keywords": keywords,
            "passage_score": 30,
        }
    ]


def _seed_ledger(tmp_path: Path, *, include_evidence: bool = True) -> tuple[sqlite3.Connection, str]:
    db = tmp_path / "ledger.sqlite3"
    connection = open_ledger(db)
    now = utc_now()
    connection.execute(
        """INSERT INTO canonical_events(
           event_id,current_version,status,label_status,event_family,event_type,event_date,
           first_seen_at,last_updated_at,stable_id,ticker_at_event,company_name,manual_grade,
           provisional_grade_cap,discovery_source,no_trading
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
        ("evt-light-1", 1, "candidate", "candidate", "delisting_or_suspension", "delisted", "2025-01-02", now, now, "stable", "ACME", "ACME HOLDINGS INC", None, None, "test"),
    )
    connection.execute(
        "INSERT INTO event_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("evt-light-1", 1, now, "candidate", "candidate", "delisting_or_suspension", "delisted", None, stable_json({"evidence_summary": "ACME event"}), "seed"),
    )
    connection.execute(
        "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?)",
        ("sec", "SEC", "official_primary", "P0", 1, 1, now, now),
    )
    connection.execute(
        "INSERT INTO raw_observations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("obs", "sec", "acme-1", "2025-01-03", now, "SEC ACME", "ACME", "https://www.sec.gov/Archives/acme.htm", "hash", "{}", "captured"),
    )
    if include_evidence:
        connection.execute(
            "INSERT INTO event_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ev-light-1", "evt-light-1", "obs", "https://www.sec.gov/Archives/acme.htm", "2025-01-03", None, None, "ACME HOLDINGS INC was determined to delist under the applicable listing rule.", "delist", 30, "candidate_passage", 0, now, now),
        )
    connection.execute(
        "INSERT INTO pipeline_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("job", "evt-light-1", "historical_evidence_review", "PENDING_EVIDENCE_REVIEW", 1, 0, now, None, "{}", now, now),
    )
    connection.commit()
    return connection, str(db)


def test_supported_requires_event_taxonomy_signal() -> None:
    result = evaluate_event(
        _event(),
        _evidence("ACME HOLDINGS INC was determined to delist under the applicable listing rule, and trading will be suspended after the final exchange notice."),
    )
    assert result["decision"] == "SUPPORTED"
    assert result["score"] >= 80

    wrong_taxonomy = evaluate_event(
        _event("volume_crash"),
        _evidence("ACME HOLDINGS INC was determined to delist under the applicable listing rule, and trading will be suspended after the final exchange notice."),
    )
    assert wrong_taxonomy["decision"] == "INSUFFICIENT"


def test_forward_looking_boilerplate_does_not_verify_liquidity_stress() -> None:
    event = _event("cash_short_debt_stress")
    event["event_family"] = "fundamental_shock"
    evidence = _evidence(
        'Forward-looking statements include plans and expectations regarding liquidity or results of operations.',
        keywords="liquidity",
    )
    assert evaluate_event(event, evidence)["decision"] == "INSUFFICIENT"


def test_supported_rejects_not_delisted_and_old_year_primary_passage() -> None:
    not_delisted = evaluate_event(
        _event(),
        _evidence(
            "ACME HOLDINGS INC announced that it will not be delisted and will remain listed under the applicable listing rule after review."
        ),
    )
    assert not_delisted["decision"] == "INSUFFICIENT"
    assert "negated_withdrawn_or_counterclaimed" in not_delisted["checks"][0]["modality_reason"]

    old_evidence = _evidence(
        "ACME HOLDINGS INC was determined to delist under the applicable listing rule, and trading will be suspended after the final exchange notice."
    )
    old_evidence[0]["filing_date"] = "2014-01-03"
    old_evidence[0]["source_published_at"] = "2014-01-03"
    old = evaluate_event(_event(), old_evidence)
    assert old["decision"] == "INSUFFICIENT"
    assert old["checks"][0]["date_coherent"] is False

    other_issuer = _evidence(
        "OTHER HOLDINGS INC was determined to delist under the applicable listing rule, and trading will be suspended after the final exchange notice."
    )
    other_issuer[0]["observation_title"] = "SEC OTHER HOLDINGS INC"
    other_issuer[0]["observation_summary"] = "OTHER HOLDINGS INC"
    identity_mismatch = evaluate_event(_event(), other_issuer)
    assert identity_mismatch["decision"] == "INSUFFICIENT"
    assert identity_mismatch["checks"][0]["identity_match"] is False


def test_negative_equity_pattern_is_valid_and_decision_grade() -> None:
    event = _event("negative_equity")
    event["event_family"] = "fundamental_shock"
    result = evaluate_event(
        event,
        _evidence(
            "ACME HOLDINGS INC reported an accumulated deficit and total equity ($1.2 million) in its annual report, resulting in negative equity at year end.",
            keywords="negative equity",
        ),
    )
    assert result["decision"] == "SUPPORTED"


def test_apply_is_atomic_and_marks_light_evidence(tmp_path: Path) -> None:
    connection, _ = _seed_ledger(tmp_path)

    result = evaluate_event(_event(), _evidence("ACME HOLDINGS INC was determined to delist under the applicable listing rule, and trading will be suspended after the final exchange notice."))
    result["batch_id"] = "batch-test"
    connection.execute("BEGIN")
    applied = apply_event(
        connection,
        result,
        batch_id="batch-test",
        before_model={"label": "ABSTAIN"},
        after_model={"label": "NON_TARGET"},
    )
    connection.commit()
    row = connection.execute("SELECT status,current_version FROM canonical_events WHERE event_id='evt-light-1'").fetchone()
    evidence = connection.execute("SELECT evidence_status,auto_verification_allowed FROM event_evidence WHERE event_id='evt-light-1'").fetchone()
    version = connection.execute("SELECT version,change_reason FROM event_versions WHERE event_id='evt-light-1' ORDER BY version DESC LIMIT 1").fetchone()
    assert applied["applied"] is True
    assert tuple(row) == ("verified", 2)
    assert tuple(evidence) == ("accepted_light_primary_evidence", 0)
    assert tuple(version) == (2, "light_evidence_verification_v2")
    connection.close()


def test_formal_support_is_bracketed_by_durable_operations_outbox(tmp_path: Path) -> None:
    connection, _ = _seed_ledger(tmp_path)
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    result = evaluate_event(
        _event(),
        _evidence(
            "ACME HOLDINGS INC was determined to delist under the applicable listing rule, and trading will be suspended after the final exchange notice."
        ),
    )
    before_model = {"input_sha256": "before", "label": "ABSTAIN"}
    after_model = {"input_sha256": "after", "label": "RISK_REVIEW"}
    result.update(
        {
            "batch_id": "batch-outbox",
            "before_model": before_model,
            "after_model": after_model,
            "after_version": None,
            "applied": False,
            "no_trading": True,
        }
    )
    mutation_id = operations.prepare_light_verification_mutation(result)
    connection.execute("BEGIN IMMEDIATE")
    applied = apply_event(
        connection,
        result,
        batch_id="batch-outbox",
        before_model=before_model,
        after_model=after_model,
    )
    connection.commit()
    operations.confirm_light_verification_mutation(mutation_id, applied)
    audit = operations.formal_mutation_audits("evt-light-1")[0]
    assert applied["formal_applied"] is True
    assert audit["state"] == "LEDGER_COMMITTED"
    assert audit["after_version"] == 2
    connection.close()


def test_insufficient_is_nonterminal_and_persists_gap_followup(tmp_path: Path) -> None:
    connection, _ = _seed_ledger(tmp_path)
    evidence = _evidence(
        "ACME HOLDINGS INC was determined to delist under the applicable listing rule, and trading will be suspended after the final exchange notice."
    )
    evidence[0]["filing_date"] = "2014-01-03"
    evidence[0]["source_published_at"] = "2014-01-03"
    result = evaluate_event(_event(), evidence)
    assert result["decision"] == "INSUFFICIENT"
    connection.execute("BEGIN IMMEDIATE")
    applied = apply_event(
        connection,
        result,
        batch_id="batch-insufficient",
        before_model={"input_sha256": "same"},
        after_model={"input_sha256": "same"},
    )
    connection.commit()
    event = connection.execute("SELECT status,current_version FROM canonical_events WHERE event_id='evt-light-1'").fetchone()
    original_job = connection.execute("SELECT status FROM pipeline_jobs WHERE job_id='job'").fetchone()
    followup = connection.execute(
        "SELECT status,payload_json FROM pipeline_jobs WHERE event_id='evt-light-1' AND job_type=?",
        (LIGHT_FOLLOWUP_JOB_TYPE,),
    ).fetchone()
    payload = json.loads(followup["payload_json"])["light_verification_followup"]
    assert tuple(event) == ("candidate", 1)
    assert original_job["status"] == "PENDING_EVIDENCE_REVIEW"
    assert followup["status"] == "PENDING_EVIDENCE_REVIEW"
    assert payload["original_event_version"] == 1
    assert any("366-day" in reason for reason in payload["gap_reasons"])
    assert applied["applied"] is False
    assert applied["attempt_persisted"] is True
    assert applied["model_delta"]["status"] == "UNCHANGED"
    assert applied["model_delta"]["confidence"] == "NOT_APPLICABLE"
    connection.close()


def test_rough_conflict_can_never_be_automatically_applied(tmp_path: Path) -> None:
    connection, db = _seed_ledger(tmp_path)
    now = utc_now()
    connection.execute(
        "INSERT INTO pipeline_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "rough-conflict",
            "evt-light-1",
            "rough_review",
            "COMPLETED_AUTHORIZED_ROUGH_REVIEW",
            99,
            0,
            now,
            None,
            stable_json({"rough_review": {"outcome": "ROUGH_CONFLICT"}}),
            now,
            now,
        ),
    )
    connection.commit()
    result = evaluate_event(
        _event(),
        _evidence(
            "ACME HOLDINGS INC was determined to delist under the applicable listing rule, and trading will be suspended after the final exchange notice."
        ),
    )
    assert result["decision"] == "SUPPORTED"
    connection.execute("BEGIN IMMEDIATE")
    applied = apply_event(
        connection,
        result,
        batch_id="batch-conflict",
        before_model={"input_sha256": "before"},
        after_model={"input_sha256": "after"},
    )
    connection.commit()
    event = connection.execute("SELECT status,current_version FROM canonical_events WHERE event_id='evt-light-1'").fetchone()
    followup = connection.execute(
        "SELECT status FROM pipeline_jobs WHERE event_id='evt-light-1' AND job_type=?",
        (LIGHT_FOLLOWUP_JOB_TYPE,),
    ).fetchone()
    assert tuple(event) == ("candidate", 1)
    assert followup["status"] == "PENDING_HUMAN_REVIEW"
    assert applied["formal_applied"] is False
    assert applied["application_blocked_reason"] == "rough-review outcome ROUGH_CONFLICT"
    assert light_verify.candidate_ids(Path(db), limit=10, event_id=None, require_rough=False) == []
    connection.execute(
        "UPDATE pipeline_jobs SET payload_json=? WHERE job_id='rough-conflict'",
        (stable_json({"rough_review": {"outcome": "ROUGH_UNRESOLVED"}}),),
    )
    connection.commit()
    assert light_verify.candidate_ids(Path(db), limit=10, event_id=None, require_rough=False) == []
    connection.close()


def test_skipped_attempt_fingerprint_prevents_queue_starvation_until_evidence_changes(tmp_path: Path) -> None:
    connection, db = _seed_ledger(tmp_path, include_evidence=False)
    assert light_verify.candidate_ids(Path(db), limit=10, event_id=None, require_rough=False) == ["evt-light-1"]
    result = evaluate_event(_event(), [])
    assert result["decision"] == "SKIPPED"
    connection.execute("BEGIN IMMEDIATE")
    applied = apply_event(
        connection,
        result,
        batch_id="batch-skipped",
        before_model={"input_sha256": "same"},
        after_model={"input_sha256": "same"},
    )
    connection.commit()
    assert applied["attempt_persisted"] is True
    assert light_verify.candidate_ids(Path(db), limit=10, event_id=None, require_rough=False) == []
    now = utc_now()
    connection.execute(
        "INSERT INTO event_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ev-new", "evt-light-1", "obs", "https://www.sec.gov/Archives/new.htm", "2025-01-03", None, None, "ACME HOLDINGS INC was determined to delist under the applicable listing rule, and trading will be suspended after the final exchange notice.", "delist", 30, "candidate_passage", 0, now, now),
    )
    connection.commit()
    assert light_verify.candidate_ids(Path(db), limit=10, event_id=None, require_rough=False) == ["evt-light-1"]
    connection.close()


def test_same_shadow_input_is_explicitly_not_applicable_delta() -> None:
    delta = model_delta(
        {"input_sha256": "same", "label": "ABSTAIN", "confidence": 1.0},
        {"input_sha256": "same", "label": "ABSTAIN", "confidence": 1.0},
    )
    assert delta["status"] == "UNCHANGED"
    assert delta["confidence"] == "NOT_APPLICABLE"


def test_scoped_authorization_binds_expiry_batch_and_exact_event_scope(tmp_path: Path) -> None:
    contract_path = tmp_path / "authorization.json"
    contract_path.write_text(
        json.dumps(
            {
                "authorization": light_verify.AUTHORIZATION_PHRASE,
                "authorization_id": "lv-test-1",
                "actor": "test-user",
                "purpose": "bounded regression",
                "batch_id": "batch-scoped",
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                "max_applies": 1,
                "event_ids": ["evt-light-1"],
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        authorization_file=contract_path,
        authorization=light_verify.AUTHORIZATION_PHRASE,
        batch_id="batch-scoped",
    )
    contract, context, event_ids = light_verify.load_scoped_authorization(args)
    assert contract["authorization_id"] == "lv-test-1"
    assert context["batch_id"] == "batch-scoped"
    assert event_ids == {"evt-light-1"}


def test_legacy_reconciliation_reopens_task_without_rolling_back_history(tmp_path: Path) -> None:
    connection, _ = _seed_ledger(tmp_path)
    now = utc_now()
    legacy_facts = {
        "light_verification": {
            "version": "light-evidence-gate-v1",
            "evidence_ids": ["ev-light-1"],
            "formal_conclusion": "weak",
        }
    }
    connection.execute(
        "UPDATE canonical_events SET current_version=2,status='weak',label_status='weak',last_updated_at=? WHERE event_id='evt-light-1'",
        (now,),
    )
    connection.execute(
        "INSERT INTO event_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("evt-light-1", 2, now, "weak", "weak", "delisting_or_suspension", "delisted", None, stable_json(legacy_facts), "light_evidence_verification_v1"),
    )
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    result = reconcile_legacy_event(connection, event_id="evt-light-1", batch_id="legacy-reconcile-test")
    connection.commit()
    current = connection.execute("SELECT status,current_version FROM canonical_events WHERE event_id='evt-light-1'").fetchone()
    followup = connection.execute(
        "SELECT status,payload_json FROM pipeline_jobs WHERE event_id='evt-light-1' AND job_type=?",
        (LIGHT_FOLLOWUP_JOB_TYPE,),
    ).fetchone()
    payload = json.loads(followup["payload_json"])["light_verification_followup"]
    assert result["reopened"] is True
    assert tuple(current) == ("weak", 2)
    assert followup["status"] == "PENDING_EVIDENCE_REVIEW"
    assert payload["legacy_reconciliation"] is True
    assert payload["original_event_version"] == 2
    connection.close()


def test_worker_defaults_to_no_light_write_and_optional_mode_is_dry_run(tmp_path: Path) -> None:
    live_report = WORKER_ROOT / "reports" / "live_cycle_latest.json"
    light_report = WORKER_ROOT / "reports" / "light_verification_latest.json"
    originals = {
        path: path.read_bytes() if path.exists() else None
        for path in (live_report, light_report)
    }
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append([str(item) for item in command])
        if "run_live_cycle.py" in str(command[1]):
            live_report.parent.mkdir(parents=True, exist_ok=True)
            live_report.write_text(json.dumps({"finished_at": "2026-08-04T00:00:00+00:00"}), encoding="utf-8")
        else:
            light_report.parent.mkdir(parents=True, exist_ok=True)
            light_report.write_text(json.dumps({"mode": "dry_run"}), encoding="utf-8")
        return Mock(returncode=0, stdout="", stderr="")

    settings = SimpleNamespace(ledger_db=tmp_path / "ledger.sqlite3", operations_db=tmp_path / "operations.sqlite3")
    operations = OperationsRepository(settings.operations_db)
    try:
        with patch("app.workers.continuous.subprocess.run", side_effect=fake_run):
            status, _ = execute_cycle(settings, operations, send=False, timeout=1, health_only=False)
            assert status == "SUCCESS"
            assert len(calls) == 1
            calls.clear()
            status, _ = execute_cycle(
                settings,
                operations,
                send=False,
                timeout=1,
                health_only=False,
                light_enabled=True,
            )
        assert status == "SUCCESS"
        assert len(calls) == 2
        light_command = calls[1]
        assert "--apply" not in light_command
        assert light_verify.AUTHORIZATION_PHRASE not in light_command
    finally:
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)


def test_light_state_is_decision_grade_for_shadow_router() -> None:
    context = derive_evidence_context(
        [{"evidence_status": "accepted_light_primary_evidence", "authority_tier": "P0"}]
    )
    assert context["state"] == "PRIMARY_SUPPORTED_LIGHT_VERIFIED"
    assert context["reason_codes"] == ["bounded_light_primary_exact_passage"]


def test_operations_persist_light_audit(tmp_path: Path) -> None:
    operations = OperationsRepository(tmp_path / "operations.sqlite3")
    run_id = operations.record_light_verification(
        {
            "batch_id": "batch-test",
            "event_id": "evt-light-1",
            "decision": "SUPPORTED",
            "before_version": 1,
            "after_version": 2,
            "evidence_ids": ["ev-light-1"],
            "budget": {"model_calls": 0},
            "rationale": "test",
            "before_model": {"label": "ABSTAIN"},
            "after_model": {"label": "RISK_REVIEW"},
            "applied": True,
        }
    )
    rows = operations.light_verification_runs("evt-light-1")
    assert rows[0]["run_id"] == run_id
    assert rows[0]["evidence_ids"] == ["ev-light-1"]
    assert rows[0]["applied"] == 1
