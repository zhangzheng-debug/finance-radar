from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.services.event_quality_recovery import BUCKETS, build_recovery_plan
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
    url: str | None = None,
) -> None:
    now = "2026-08-20T01:02:03+00:00"
    observation_id = f"obs-{event_id}"
    evidence_id = f"ev-{event_id}"
    content = hashlib.sha256(event_id.encode()).hexdigest()
    source_url = url or f"https://www.sec.gov/Archives/{event_id}.htm"
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
        (event_id, now, status, status, event_type, json.dumps(facts or {})),
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
        connection.execute(
            """INSERT INTO event_evidence_relations VALUES (
               ?,?,1,'SCOPED_MATCH',1,1,1,'DISCLOSED',?,'event-admission-v1','fixture',?)""",
            (event_id, evidence_id, "f" * 64, now),
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
        facts=structured,
        relation=True,
    )
    _seed_event(
        connection,
        event_id="legacy-verified",
        status="verified",
        evidence_status="confirmed_primary",
        passage=primary_passage,
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
    assert plan["source_event_count"] == 7
    assert plan["partition_total"] == 7
    assert plan["partition_complete"] is True
    assert set(plan["bucket_counts"]) == set(BUCKETS)
    by_event = {record["event_id"]: record for record in plan["records"]}
    assert by_event["ready"]["bucket"] == "READER_READY_CURRENT"
    assert by_event["legacy-verified"]["bucket"] == "LEGACY_FORMAL_REVIEW_REQUIRED"
    assert by_event["generic"]["bucket"] == "GENERIC_SEC_DISCOVERY"
    assert by_event["nondecision"]["bucket"] == "NON_DECISION_EVIDENCE_ONLY"
    assert by_event["missing-primary"]["bucket"] == "MISSING_PRIMARY_EVIDENCE"
    assert by_event["enrichable"]["bucket"] == "ENRICHABLE_PRIMARY"
    assert by_event["z-duplicate"]["bucket"] == "STRICT_DUPLICATE_CANDIDATE"
    assert all(len(record["before"]["evidence_fingerprint"]) == 64 for record in plan["records"])
    assert all(len(record["rollback_identity_sha256"]) == 64 for record in plan["records"])
    assert all(record["canonical_mutation_attempted"] is False for record in plan["records"])


def test_cli_export_contains_hashes_and_no_apply_surface(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "ledger.sqlite3")
    output = tmp_path / "plan"

    manifest = build(ledger, output)

    assert manifest["read_only"] is True
    assert (output / "manifest.json").is_file()
    assert (output / "recovery_plan.jsonl").read_text(encoding="utf-8").count("\n") == 7
    sums = (output / "SHA256SUMS.txt").read_text(encoding="utf-8")
    assert "recovery_plan.jsonl" in sums
    assert "apply" not in {path.stem for path in output.iterdir()}
