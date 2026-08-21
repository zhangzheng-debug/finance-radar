from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

import scripts.apply_event_quality_recovery as recovery_cli
from app.services.event_admission import (
    ADMISSION_CONTRACT_VERSION,
    FACT_SLOT_CONTRACT_VERSION,
    LEGACY_ADMISSION_CONTRACT_VERSION,
    evidence_relation_fingerprint,
    extract_evidence_fact_slots,
    fact_slot_receipt_sha256,
    public_fact_summary,
)
from app.services.event_quality_recovery import (
    BUCKETS,
    apply_machine_relation_backfill,
    build_recovery_authorization_template,
    build_recovery_plan,
    sha256_file,
    validate_recovery_plan,
)
from app.storage.ledger import LedgerRepository
from scripts.apply_event_quality_recovery import load_plan, run
from scripts.build_event_quality_recovery_plan import build
from scripts.event_ledger import open_ledger, utc_now


def _seed_event(
    connection,
    *,
    event_id: str,
    status: str = "candidate",
    event_type: str = "management_change",
    source_id: str = "sec_current_filings",
    evidence_status: str | None = None,
    passage: str = "",
    facts: dict | None = None,
    relation: bool = False,
    recoverable: bool = False,
    url: str | None = None,
) -> None:
    now = "2026-08-20T01:02:03+00:00"
    observation_id = f"obs-{event_id}"
    evidence_id = f"ev-{event_id}"
    content = hashlib.sha256(event_id.encode()).hexdigest()
    source_url = url or f"https://www.sec.gov/Archives/{event_id}.htm"
    effective_facts = dict(facts or {})
    if recoverable:
        extraction = extract_evidence_fact_slots(
            evidence_passage=passage,
            event_type=event_type,
            expected_subject="Example Corp",
        )
        summary = public_fact_summary(
            subject="Example Corp",
            action_label=event_type,
            stage_label="DISCLOSED",
            extraction=extraction,
        )
        receipt_sha256 = fact_slot_receipt_sha256(
            extraction=extraction,
            public_fact_summary_text=summary,
        )
        effective_facts.update(
            {
                "candidate_only": True,
                "public_fact_summary": summary,
                "claim_subject": "Example Corp",
                "claim_action": event_type,
                "claim_stage": "DISCLOSED",
                "claim_fact_slots": extraction.as_dict(),
                "fact_slot_contract_version": FACT_SLOT_CONTRACT_VERSION,
                "fact_slot_receipt_sha256": receipt_sha256,
                "known_at": now,
                "source_observation_id": observation_id,
                "source_content_sha256": content,
                "evidence_id": evidence_id,
                "admission_contract_version": ADMISSION_CONTRACT_VERSION,
                "formal_verification": False,
                "no_trading": True,
            }
        )
        effective_facts["evidence_fingerprint"] = evidence_relation_fingerprint(
            event_id=event_id,
            event_version=1,
            evidence_id=evidence_id,
            content_sha256=content,
            subject="Example Corp",
            action=event_type,
            stage="DISCLOSED",
            known_at=now,
            contract_version=ADMISSION_CONTRACT_VERSION,
            evidence_passage_sha256=extraction.passage_sha256,
            fact_slot_receipt_sha256=receipt_sha256,
            public_fact_summary_sha256=hashlib.sha256(summary.encode("utf-8")).hexdigest(),
        )
    connection.execute(
        """INSERT INTO raw_observations VALUES (
           ?,?,?,?,?,'Example Corp disclosure','Example Corp disclosure',?,?,?,'captured')""",
        (
            observation_id,
            source_id,
            event_id,
            now,
            now,
            source_url,
            content,
            json.dumps({"item": {"company": "Example Corp"}}),
        ),
    )
    connection.execute(
        """INSERT INTO canonical_events VALUES (
           ?,1,?,?, 'governance',?,?,?, ?,NULL,'EXM','Example Corp',NULL,'A_P0',?,1)""",
        (event_id, status, status, event_type, now[:10], now, now, source_id),
    )
    connection.execute(
        """INSERT INTO event_versions VALUES (
           ?,1,?,?,?,'governance',?,NULL,?,'fixture')""",
        (event_id, now, status, status, event_type, json.dumps(effective_facts)),
    )
    connection.execute(
        "INSERT INTO event_observations VALUES (?,?, 'primary',?)",
        (event_id, observation_id, now),
    )
    if evidence_status:
        connection.execute(
            """INSERT INTO event_evidence VALUES (
               ?,?,?,?,NULL,NULL,NULL,?,NULL,90,?,0,?,?)""",
            (evidence_id, event_id, observation_id, source_url, passage, evidence_status, now, now),
        )
    if relation:
        relation_fingerprint = str(effective_facts.get("evidence_fingerprint") or "f" * 64)
        relation_contract = str(
            effective_facts.get("admission_contract_version")
            or LEGACY_ADMISSION_CONTRACT_VERSION
        )
        connection.execute(
            """INSERT INTO event_evidence_relations VALUES (
               ?,?,1,'SCOPED_MATCH',1,1,1,'DISCLOSED',?,?,'fixture',?)""",
            (event_id, evidence_id, relation_fingerprint, relation_contract, now),
        )


def _ledger(path: Path) -> Path:
    connection = open_ledger(path)
    now = utc_now()
    for source_id, tier in (
        ("sec_current_filings", "P0_official"),
        ("opennews_free", "P2_discovery"),
    ):
        connection.execute(
            "INSERT INTO sources VALUES (?,?, 'fixture',?,1,1,?,?)",
            (source_id, source_id, tier, now, now),
        )
    structured = {
        "public_fact_summary": "Example Corp 的官方文件确认管理层发生变更，当前为已披露阶段。",
        "claim_subject": "Example Corp",
        "claim_action": "management_change",
        "claim_stage": "DISCLOSED",
        "known_at": "2026-08-20T01:02:03+00:00",
    }
    primary_passage = (
        "Example Corp disclosed that its chief financial officer resigned effective immediately."
    )
    _seed_event(
        connection,
        event_id="ready",
        evidence_status="machine_extracted_unreviewed",
        passage=primary_passage,
        recoverable=True,
        relation=True,
    )
    _seed_event(
        connection,
        event_id="legacy-verified",
        status="verified",
        evidence_status="confirmed_primary",
        passage=primary_passage,
        recoverable=True,
    )
    _seed_event(connection, event_id="generic", event_type="sec_material_filing")
    _seed_event(
        connection,
        event_id="nondecision",
        evidence_status="machine_extracted_non_decision",
        passage=primary_passage,
    )
    _seed_event(connection, event_id="missing-primary", source_id="opennews_free")
    _seed_event(
        connection,
        event_id="enrichable",
        evidence_status="machine_extracted_unreviewed",
        passage=primary_passage,
    )
    for event_id in ("recoverable-a", "recoverable-b"):
        _seed_event(
            connection,
            event_id=event_id,
            evidence_status="machine_extracted_unreviewed",
            passage=primary_passage,
            recoverable=True,
        )
    _seed_event(
        connection,
        event_id="z-duplicate",
        evidence_status="machine_extracted_unreviewed",
        passage=primary_passage,
        url="https://www.sec.gov/Archives/enrichable.htm",
    )
    connection.commit()
    connection.close()
    return path


def test_recovery_plan_is_read_only_exhaustive_and_version_bound(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    before = ledger.read_bytes()

    plan = build_recovery_plan(ledger)

    assert ledger.read_bytes() == before
    assert plan["source_event_count"] == 9
    assert plan["partition_total"] == 9
    assert plan["partition_complete"] is True
    assert set(plan["bucket_counts"]) == set(BUCKETS)
    by_event = {record["event_id"]: record for record in plan["records"]}
    assert by_event["ready"]["bucket"] == "READER_READY_CURRENT"
    assert by_event["legacy-verified"]["bucket"] == "LEGACY_FORMAL_REVIEW_REQUIRED"
    assert by_event["generic"]["bucket"] == "GENERIC_SEC_DISCOVERY"
    assert by_event["nondecision"]["bucket"] == "NON_DECISION_EVIDENCE_ONLY"
    assert by_event["missing-primary"]["bucket"] == "MISSING_PRIMARY_EVIDENCE"
    assert by_event["enrichable"]["bucket"] == "ENRICHABLE_PRIMARY"
    assert by_event["recoverable-a"]["bucket"] == "ENRICHABLE_PRIMARY"
    assert by_event["recoverable-a"]["machine_relation_backfill_eligible"] is True
    assert by_event["recoverable-b"]["machine_relation_backfill_eligible"] is True
    assert by_event["legacy-verified"]["machine_relation_backfill_eligible"] is False
    assert "FORMAL_OR_TERMINAL_STATUS_REQUIRES_HUMAN" in by_event[
        "legacy-verified"
    ]["machine_relation_backfill_reason_codes"]
    assert by_event["z-duplicate"]["bucket"] == "STRICT_DUPLICATE_CANDIDATE"
    assert all(len(record["before"]["evidence_fingerprint"]) == 64 for record in plan["records"])
    assert all(len(record["rollback_identity_sha256"]) == 64 for record in plan["records"])
    assert all(record["canonical_mutation_attempted"] is False for record in plan["records"])
    assert plan["machine_relation_backfill_eligible"] == 2
    assert validate_recovery_plan(plan) == plan["plan_sha256"]


def test_legacy_v1_receipts_are_read_only_and_never_machine_recovered(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    with open_ledger(ledger) as connection:
        for event_id in ("ready", "recoverable-a"):
            row = connection.execute(
                "SELECT facts_json FROM event_versions WHERE event_id=? AND version=1",
                (event_id,),
            ).fetchone()
            facts = json.loads(row[0])
            facts["admission_contract_version"] = LEGACY_ADMISSION_CONTRACT_VERSION
            connection.execute(
                "UPDATE event_versions SET facts_json=? WHERE event_id=? AND version=1",
                (json.dumps(facts), event_id),
            )
        connection.commit()

    reader_ids = {
        row["event_id"]
        for row in LedgerRepository(ledger).list_events(reader_ready=True, limit=100)["items"]
    }
    assert "ready" not in reader_ids
    assert LedgerRepository(ledger).event_evidence("ready")[0]["reader_eligible"] == 0

    plan = build_recovery_plan(ledger)
    record = next(row for row in plan["records"] if row["event_id"] == "recoverable-a")
    assert record["machine_relation_backfill_eligible"] is False
    assert "LEGACY_ADMISSION_V1_READ_ONLY_NOT_RECOVERABLE" in record[
        "machine_relation_backfill_reason_codes"
    ]


def test_recovery_and_reader_reject_tampered_slots_summary_and_receipt(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    with open_ledger(ledger) as connection:
        for event_id in ("recoverable-a", "recoverable-b"):
            row = connection.execute(
                "SELECT facts_json FROM event_versions WHERE event_id=? AND version=1",
                (event_id,),
            ).fetchone()
            facts = json.loads(row[0])
            if event_id == "recoverable-a":
                facts["claim_fact_slots"]["facts"][0]["action_text"] = "reported"
            else:
                facts["public_fact_summary"] += " altered"
                facts["fact_slot_receipt_sha256"] = "0" * 64
            connection.execute(
                "UPDATE event_versions SET facts_json=? WHERE event_id=? AND version=1",
                (json.dumps(facts), event_id),
            )
        connection.commit()

    reader_ids = {
        row["event_id"]
        for row in LedgerRepository(ledger).list_events(reader_ready=True, limit=100)["items"]
    }
    assert "ready" in reader_ids

    plan = build_recovery_plan(ledger)
    by_event = {record["event_id"]: record for record in plan["records"]}
    assert "STORED_FACT_SLOTS_DO_NOT_REPLAY" in by_event["recoverable-a"][
        "machine_relation_backfill_reason_codes"
    ]
    assert "PUBLIC_FACT_SUMMARY_DOES_NOT_REPLAY" in by_event["recoverable-b"][
        "machine_relation_backfill_reason_codes"
    ]
    assert "FACT_SLOT_RECEIPT_HASH_MISMATCH" in by_event["recoverable-b"][
        "machine_relation_backfill_reason_codes"
    ]


def test_sec_reader_gate_invalidates_when_the_bound_source_revision_changes(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    assert "ready" in {
        row["event_id"]
        for row in LedgerRepository(ledger).list_events(reader_ready=True, limit=100)["items"]
    }
    with open_ledger(ledger) as connection:
        connection.execute(
            """INSERT INTO source_revisions(
                   revision_id,observation_id,source_id,external_id,revision_no,
                   revision_kind,revision_at,content_sha256,title,summary,raw_json
               ) VALUES (?,?,?,?,1,'edit',?,?,?,?,?)""",
            (
                "rev-ready-edit",
                "obs-ready",
                "sec_current_filings",
                "ready",
                "2026-08-20T02:02:03+00:00",
                "b" * 64,
                "Example Corp amended disclosure",
                "The source content changed after admission.",
                "{}",
            ),
        )
        connection.commit()

    reader_ids = {
        row["event_id"]
        for row in LedgerRepository(ledger).list_events(reader_ready=True, limit=100)["items"]
    }
    assert "ready" not in reader_ids
    assert LedgerRepository(ledger).event_evidence("ready")[0]["reader_eligible"] == 0


def test_recovery_planner_rejects_deleted_and_changed_source_revisions(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    with open_ledger(ledger) as connection:
        connection.execute(
            "UPDATE raw_observations SET observation_status='deleted' "
            "WHERE observation_id='obs-recoverable-a'"
        )
        connection.execute(
            """INSERT INTO source_revisions(
                   revision_id,observation_id,source_id,external_id,revision_no,
                   revision_kind,revision_at,content_sha256,title,summary,raw_json
               ) VALUES (?,?,?,?,1,'edit',?,?,?,?,?)""",
            (
                "rev-recoverable-b-edit",
                "obs-recoverable-b",
                "sec_current_filings",
                "recoverable-b",
                "2026-08-20T02:02:03+00:00",
                "b" * 64,
                "Example Corp amended disclosure",
                "The current official source no longer matches the admission receipt.",
                "{}",
            ),
        )
        connection.commit()

    plan = build_recovery_plan(ledger)
    by_event = {record["event_id"]: record for record in plan["records"]}
    assert by_event["recoverable-a"]["machine_relation_backfill_eligible"] is False
    assert "SOURCE_REVISION_DELETED" in by_event["recoverable-a"][
        "machine_relation_backfill_reason_codes"
    ]
    assert by_event["recoverable-b"]["machine_relation_backfill_eligible"] is False
    assert "SOURCE_REVISION_CHANGED" in by_event["recoverable-b"][
        "machine_relation_backfill_reason_codes"
    ]


def test_recovery_reproof_reports_source_revision_reason_after_plan(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    plan = build_recovery_plan(ledger)
    with open_ledger(ledger) as connection:
        connection.execute(
            """INSERT INTO source_revisions(
                   revision_id,observation_id,source_id,external_id,revision_no,
                   revision_kind,revision_at,content_sha256,title,summary,raw_json
               ) VALUES (?,?,?,?,1,'edit',?,?,?,?,?)""",
            (
                "rev-recoverable-a-edit",
                "obs-recoverable-a",
                "sec_current_filings",
                "recoverable-a",
                "2026-08-20T02:02:03+00:00",
                "c" * 64,
                "Example Corp amended disclosure",
                "A changed source revision must invalidate the frozen recovery plan.",
                "{}",
            ),
        )
        connection.commit()

    preview = apply_machine_relation_backfill(ledger, plan)
    assert preview["ready_to_apply"] == 0
    assert any(
        issue == "recoverable-a: SOURCE_REVISION_CHANGED"
        for issue in preview["issues"]
    )


def test_empty_post_migration_relation_tables_fail_closed_but_expose_safe_subset(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    # Reproduce a Schema 12 ledger that predates the two semantic-gate tables,
    # then let the real migration path create empty Schema 14 tables.
    with sqlite3.connect(ledger) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TABLE event_evidence_relations")
        connection.execute("DROP TABLE event_fact_workflow")
        connection.execute("DELETE FROM event_ledger_schema")
        connection.execute(
            "INSERT INTO event_ledger_schema(version,applied_at) VALUES (12,?)",
            (utc_now(),),
        )
        connection.commit()
    with open_ledger(ledger) as migrated:
        assert migrated.execute(
            "SELECT MAX(version) FROM event_ledger_schema"
        ).fetchone()[0] == 14
        assert migrated.execute(
            "SELECT COUNT(*) FROM event_evidence_relations"
        ).fetchone()[0] == 0
    public = LedgerRepository(ledger).list_events(reader_ready=True, limit=100)
    assert public["total"] == 0

    plan = build_recovery_plan(ledger)
    eligible = {
        record["event_id"]
        for record in plan["records"]
        if record["machine_relation_backfill_eligible"]
    }
    assert eligible == {"ready", "recoverable-a", "recoverable-b"}
    assert "legacy-verified" not in eligible


def test_cli_export_contains_hashes_and_inert_authorization_template(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    output = tmp_path / "plan"

    manifest = build(ledger, output)

    assert manifest["read_only"] is True
    assert (output / "manifest.json").is_file()
    assert (output / "recovery_plan.jsonl").read_text(encoding="utf-8").count("\n") == 9
    sums = (output / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert "recovery_plan.jsonl" in sums
    assert "authorization_template.json" in sums
    authorization = json.loads(
        (output / "authorization_template.json").read_text(encoding="utf-8")
    )
    assert authorization["approved"] is False
    assert authorization["no_status_or_label_mutation"] is True
    assert len(authorization["scope"]) == 2
    assert load_plan(output)["plan_sha256"] == manifest["plan_sha256"]


def _authorization(plan: dict, backup: Path) -> dict:
    authorization = build_recovery_authorization_template(plan)
    authorization.update(
        {
            "approved": True,
            "authorization_id": "test-recovery-authorization",
            "actor": "test-owner",
            "purpose": "restore rows already proven by frozen admission facts",
            "expires_at": "2099-08-20T01:02:03+00:00",
            "backup_path": str(backup),
            "backup_sha256": sha256_file(backup),
        }
    )
    return authorization


def test_default_dry_run_then_authorized_apply_restores_reader_gate_only(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    plan = build_recovery_plan(ledger)
    before_ready = {
        row["event_id"]
        for row in LedgerRepository(ledger).list_events(reader_ready=True, limit=100)["items"]
    }
    assert "recoverable-a" not in before_ready
    before_state = {
        row["event_id"]: (row["current_version"], row["status"], row["label_status"])
        for row in LedgerRepository(ledger).list_events(limit=100)["items"]
    }

    preview = apply_machine_relation_backfill(ledger, plan)
    assert preview["mode"] == "DRY_RUN"
    assert preview["ready_to_apply"] == 2
    assert preview["applied"] == 0
    with open_ledger(ledger) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM event_evidence_relations WHERE event_id LIKE 'recoverable-%'"
        ).fetchone()[0] == 0

    backup = tmp_path / "ledger.pre-recovery.sqlite3"
    shutil.copy2(ledger, backup)
    result = apply_machine_relation_backfill(
        ledger,
        plan,
        _authorization(plan, backup),
        execute=True,
    )
    assert result["mode"] == "APPLIED"
    assert result["applied"] == 2
    assert result["canonical_status_or_version_mutation"] is False
    after_ready = {
        row["event_id"]
        for row in LedgerRepository(ledger).list_events(reader_ready=True, limit=100)["items"]
    }
    assert {"recoverable-a", "recoverable-b"} <= after_ready
    after_state = {
        row["event_id"]: (row["current_version"], row["status"], row["label_status"])
        for row in LedgerRepository(ledger).list_events(limit=100)["items"]
    }
    assert after_state == before_state
    with open_ledger(ledger) as connection:
        rows = connection.execute(
            """SELECT rel.event_id,rel.relation_status,w.workflow_state
               FROM event_evidence_relations rel
               JOIN event_fact_workflow w
                 ON w.event_id=rel.event_id AND w.event_version=rel.event_version
               WHERE rel.event_id LIKE 'recoverable-%' ORDER BY rel.event_id"""
        ).fetchall()
        assert [(row[0], row[1], row[2]) for row in rows] == [
            ("recoverable-a", "SCOPED_MATCH", "EVIDENCE_READY"),
            ("recoverable-b", "SCOPED_MATCH", "EVIDENCE_READY"),
        ]
        assert connection.execute(
            "SELECT COUNT(*) FROM event_evidence_relations WHERE event_id='legacy-verified'"
        ).fetchone()[0] == 0
        audit = connection.execute(
            """SELECT state,result_sha256,result_json,no_status_or_version_mutation,
                      no_trading
               FROM event_quality_recovery_audit"""
        ).fetchone()
        assert audit[0] == "DB_COMMITTED"
        assert json.loads(audit[2])["result_sha256"] == audit[1]
        assert (audit[3], audit[4]) == (1, 1)


def test_authorization_is_bound_to_one_target_and_rejected_on_clone(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path / "ledger-a.sqlite3")
    plan = build_recovery_plan(ledger)
    backup = tmp_path / "ledger-a.pre-recovery.sqlite3"
    clone = tmp_path / "ledger-b-clone.sqlite3"
    shutil.copy2(ledger, backup)
    shutil.copy2(ledger, clone)
    authorization = _authorization(plan, backup)

    first = apply_machine_relation_backfill(
        ledger,
        plan,
        authorization,
        execute=True,
    )
    assert first["applied"] == 2
    with pytest.raises(ValueError, match="target ledger identity"):
        apply_machine_relation_backfill(
            clone,
            plan,
            authorization,
            execute=True,
        )
    with open_ledger(clone) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM event_evidence_relations WHERE event_id LIKE 'recoverable-%'"
        ).fetchone()[0] == 0


def test_stale_evidence_aborts_entire_authorized_batch(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    plan = build_recovery_plan(ledger)
    backup = tmp_path / "ledger.pre-recovery.sqlite3"
    shutil.copy2(ledger, backup)
    with open_ledger(ledger) as connection:
        connection.execute(
            """UPDATE event_evidence SET evidence_passage=evidence_passage || ' changed',
                      updated_at=? WHERE event_id='recoverable-b'""",
            (utc_now(),),
        )
        connection.commit()

    preview = apply_machine_relation_backfill(ledger, plan)
    assert preview["ready_to_apply"] == 0
    assert preview["individually_revalidated"] == 1
    assert preview["stale_or_blocked"] >= 1
    with pytest.raises(ValueError, match="STALE_RECOVERY_SCOPE"):
        apply_machine_relation_backfill(
            ledger,
            plan,
            _authorization(plan, backup),
            execute=True,
        )
    with open_ledger(ledger) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM event_evidence_relations WHERE event_id LIKE 'recoverable-%'"
        ).fetchone()[0] == 0


def test_backup_must_match_the_full_authorized_logical_snapshot(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    plan = build_recovery_plan(ledger)
    backup = tmp_path / "ledger.incomplete-backup.sqlite3"
    shutil.copy2(ledger, backup)
    with open_ledger(backup) as connection:
        connection.execute(
            "UPDATE canonical_events SET company_name='Drifted Corp' WHERE event_id='generic'"
        )
        connection.commit()
    authorization = _authorization(plan, backup)
    with pytest.raises(ValueError, match="backup .*logical snapshot"):
        apply_machine_relation_backfill(
            ledger,
            plan,
            authorization,
            execute=True,
        )
    with open_ledger(ledger) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM event_evidence_relations WHERE event_id LIKE 'recoverable-%'"
        ).fetchone()[0] == 0


def test_backup_hard_link_to_target_is_not_an_independent_recovery_copy(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    plan = build_recovery_plan(ledger)
    hard_link = tmp_path / "ledger-hard-link.sqlite3"
    try:
        os.link(ledger, hard_link)
    except OSError as exc:
        pytest.skip(f"filesystem does not support creating hard links: {exc}")
    assert hard_link.resolve() != ledger.resolve()
    assert hard_link.samefile(ledger)

    with pytest.raises(ValueError, match="independent file.*hard link"):
        apply_machine_relation_backfill(
            ledger,
            plan,
            _authorization(plan, hard_link),
            execute=True,
        )
    with open_ledger(ledger) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM event_evidence_relations WHERE event_id LIKE 'recoverable-%'"
        ).fetchone()[0] == 0


def test_raw_main_file_copy_that_omits_committed_wal_is_rejected(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    writer = sqlite3.connect(ledger)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute(
            "UPDATE sources SET updated_at=? WHERE source_id='opennews_free'",
            ("2026-08-20T09:09:09+00:00",),
        )
        writer.commit()
        plan = build_recovery_plan(ledger)
        # Deliberately copy only the main file while the committed update still
        # lives in -wal.  A raw copy may pass SQLite integrity checks but is not
        # the authorized logical snapshot.
        backup = tmp_path / "raw-main-file-only.sqlite3"
        shutil.copy2(ledger, backup)
    finally:
        writer.close()
    authorization = _authorization(plan, backup)
    with pytest.raises(ValueError, match="incomplete WAL copy"):
        apply_machine_relation_backfill(
            ledger,
            plan,
            authorization,
            execute=True,
        )
    with open_ledger(ledger) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM event_evidence_relations WHERE event_id LIKE 'recoverable-%'"
        ).fetchone()[0] == 0


def test_apply_cli_requires_explicit_flag_and_writes_append_only_audit(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    plan_dir = tmp_path / "plan"
    build(ledger, plan_dir)
    preview = run(ledger, plan_dir)
    assert preview["mode"] == "DRY_RUN"
    backup = tmp_path / "backup.sqlite3"
    shutil.copy2(ledger, backup)
    plan = load_plan(plan_dir)
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_text(
        json.dumps(_authorization(plan, backup)), encoding="utf-8"
    )
    audit = tmp_path / "audit"
    result = run(
        ledger,
        plan_dir,
        execute=True,
        authorization_path=authorization_path,
        audit_output=audit,
    )
    assert result["applied"] == 2
    assert (audit / "apply_intent.json").is_file()
    assert (audit / "apply_result.json").is_file()
    assert "apply_intent.json" in (audit / "SHA256SUMS.txt").read_text(encoding="utf-8")


def _assert_sealed_audit_directory(audit: Path) -> None:
    entries = {}
    for line in (audit / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        assert separator
        entries[name] = digest
    expected_files = {
        path.name for path in audit.iterdir() if path.is_file() and path.name != "SHA256SUMS.txt"
    }
    assert set(entries) == expected_files
    assert all(sha256_file(audit / name) == digest for name, digest in entries.items())


def test_failed_apply_still_writes_a_sealed_failure_receipt(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    plan_dir = tmp_path / "plan"
    build(ledger, plan_dir)
    plan = load_plan(plan_dir)
    backup = tmp_path / "backup.sqlite3"
    shutil.copy2(ledger, backup)
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_text(
        json.dumps(_authorization(plan, backup)), encoding="utf-8"
    )
    with open_ledger(ledger) as connection:
        connection.execute(
            "UPDATE event_evidence SET evidence_passage=evidence_passage || ' changed' "
            "WHERE event_id='recoverable-b'"
        )
        connection.commit()

    audit = tmp_path / "failed-audit"
    with pytest.raises(ValueError, match="STALE_RECOVERY_SCOPE"):
        run(
            ledger,
            plan_dir,
            execute=True,
            authorization_path=authorization_path,
            audit_output=audit,
        )
    error = json.loads((audit / "apply_error.json").read_text(encoding="utf-8"))
    assert error["state"] == "ABORTED_OR_FAILED"
    assert not (audit / "apply_result.json").exists()
    _assert_sealed_audit_directory(audit)


def test_database_receipt_survives_postcommit_file_receipt_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    plan_dir = tmp_path / "plan"
    build(ledger, plan_dir)
    plan = load_plan(plan_dir)
    backup = tmp_path / "backup.sqlite3"
    shutil.copy2(ledger, backup)
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_text(
        json.dumps(_authorization(plan, backup)), encoding="utf-8"
    )
    real_write_exclusive = recovery_cli._write_exclusive

    def fail_result_receipt(path: Path, payload: dict) -> None:
        if path.name == "apply_result.json":
            raise OSError("simulated post-commit file receipt failure")
        real_write_exclusive(path, payload)

    monkeypatch.setattr(recovery_cli, "_write_exclusive", fail_result_receipt)
    audit = tmp_path / "postcommit-audit"
    with pytest.raises(OSError, match="post-commit file receipt failure"):
        recovery_cli.run(
            ledger,
            plan_dir,
            execute=True,
            authorization_path=authorization_path,
            audit_output=audit,
        )

    with sqlite3.connect(ledger) as connection:
        row = connection.execute(
            "SELECT state,result_sha256,result_json FROM event_quality_recovery_audit"
        ).fetchone()
        assert row is not None
        assert row[0] == "DB_COMMITTED"
        durable_result = json.loads(row[2])
        assert durable_result["applied"] == 2
        assert durable_result["result_sha256"] == row[1]
        assert connection.execute(
            "SELECT COUNT(*) FROM event_evidence_relations WHERE event_id LIKE 'recoverable-%'"
        ).fetchone()[0] == 2
    error = json.loads((audit / "apply_error.json").read_text(encoding="utf-8"))
    assert error["state"] == "DATABASE_COMMITTED_FILE_RECEIPT_FAILED"
    assert error["durable_audit_id"] == durable_result["durable_audit_id"]
    assert not (audit / "apply_result.json").exists()
    _assert_sealed_audit_directory(audit)


def test_tampered_plan_and_missing_backup_fail_closed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    plan = build_recovery_plan(ledger)
    tampered = json.loads(json.dumps(plan))
    tampered["records"][0]["bucket"] = "NEEDS_HUMAN"
    with pytest.raises(ValueError, match="plan_sha256"):
        apply_machine_relation_backfill(ledger, tampered)

    authorization = build_recovery_authorization_template(plan)
    authorization.update(
        {
            "approved": True,
            "authorization_id": "test-missing-backup",
            "actor": "test-owner",
            "purpose": "negative test",
            "expires_at": "2099-08-20T01:02:03+00:00",
            "backup_path": str(tmp_path / "missing.sqlite3"),
            "backup_sha256": "0" * 64,
        }
    )
    with pytest.raises(ValueError, match="backup_path"):
        apply_machine_relation_backfill(
            ledger,
            plan,
            authorization,
            execute=True,
        )


def test_empty_machine_scope_cannot_be_presented_as_successful_apply(
    tmp_path: Path,
) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    with open_ledger(ledger) as connection:
        connection.execute(
            """UPDATE canonical_events SET status='verified',label_status='verified'
               WHERE event_id LIKE 'recoverable-%'"""
        )
        connection.execute(
            """UPDATE event_versions SET status='verified',label_status='verified'
               WHERE event_id LIKE 'recoverable-%'"""
        )
        connection.commit()
    plan = build_recovery_plan(ledger)
    assert plan["machine_relation_backfill_eligible"] == 0
    preview = apply_machine_relation_backfill(ledger, plan)
    assert preview["ready_to_apply"] == 0
    with pytest.raises(ValueError, match="no machine-safe rows"):
        apply_machine_relation_backfill(ledger, plan, {}, execute=True)
