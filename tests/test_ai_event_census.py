from __future__ import annotations

import hashlib
import json
import sys
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import ai_event_census_kit
import event_ledger
from app.services.ai_event_census import (
    BOUNDARY_VALUES,
    CONTRACT_VERSION,
    OVERLAP_RATE,
    PROMPT_SHA256,
    PROMPT_VERSION,
    allocate_packets,
    build_assignment_shards,
    extract_all_event_packets,
    merge_census_submissions,
    parse_assignment_records,
    validate_submission_records,
)


def _queue(candidate_id: str) -> dict[str, str]:
    ticker = f"T{candidate_id}"
    return {
        "queue_rank": candidate_id,
        "event_candidate_id": candidate_id,
        "stable_id": f"permaticker:{candidate_id}",
        "ticker_at_event": ticker,
        "company_name": f"{ticker} Corporation",
        "event_date": "2026-01-01",
        "event_family": "bankruptcy_or_distress",
        "event_type": "bankruptcy_liquidation",
        "detection_rule": "fixture candidate",
        "detection_value": "fixture",
        "priority_score": "100",
        "provisional_grade_cap": "A++_candidate",
        "sec_filings_url": f"https://www.sec.gov/{candidate_id}",
    }


def _passage(candidate_id: str) -> dict[str, str]:
    ticker = f"T{candidate_id}"
    return {
        "event_candidate_id": candidate_id,
        "accession_number": f"0001-26-{candidate_id}",
        "filing_date": "2026-01-01",
        "form": "8-K",
        "items": "1.03",
        "filing_document_url": f"https://www.sec.gov/{candidate_id}/document",
        "text_sha256": hashlib.sha256(candidate_id.encode("utf-8")).hexdigest(),
        "evidence_passage": (
            f"{ticker} Corporation filed a voluntary petition under Chapter 11 "
            "in the United States Bankruptcy Court on January 1, 2026."
        ),
        "matched_keywords": "chapter 11",
        "passage_score": "10",
        "passage_status": "candidate_passage",
    }


@pytest.fixture()
def census_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "census.sqlite3"
    connection = event_ledger.open_ledger(path)
    try:
        identifiers = [f"C{index:02d}" for index in range(1, 22)]
        event_ledger.import_active_research(
            connection,
            queue_rows=[_queue(identifier) for identifier in identifiers],
            passage_rows=[_passage(identifier) for identifier in identifiers],
            adjudication_rows=[],
            market_rows=[],
        )
        event_ids = [
            str(row[0])
            for row in connection.execute(
                "SELECT event_id FROM canonical_events ORDER BY event_id"
            ).fetchall()
        ]
        connection.execute(
            "UPDATE canonical_events SET status='verified',label_status='verified' WHERE event_id=?",
            (event_ids[0],),
        )
        connection.execute(
            "UPDATE canonical_events SET status='weak',label_status='weak' WHERE event_id=?",
            (event_ids[1],),
        )
        connection.execute(
            "UPDATE canonical_events SET status='rejected',label_status='rejected' WHERE event_id=?",
            (event_ids[2],),
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _result_for(packet: dict, header: dict, *, disagreement: bool = False) -> dict:
    evidence_ids = [
        str(row["evidence_id"])
        for row in packet.get("evidence") or []
        if row.get("evidence_id")
    ]
    if disagreement:
        checks = {
            "source_accessible": "YES",
            "subject_match": "YES",
            "event_claim_supported": "UNCLEAR",
            "date_stage_coherent": "YES",
            "evidence_sufficient": "UNCLEAR",
            "conflict_found": "NO",
        }
        evidence_state = "INSUFFICIENT"
        disposition = "AI_NEEDS_EVIDENCE"
        reason_codes = ["NO_EXACT_PASSAGE"]
        selected_evidence_ids: list[str] = []
        materiality = "UNCLEAR"
        polarity = "UNCLEAR"
    else:
        checks = {
            "source_accessible": "YES",
            "subject_match": "YES",
            "event_claim_supported": "YES",
            "date_stage_coherent": "YES",
            "evidence_sufficient": "YES",
            "conflict_found": "NO",
        }
        evidence_state = "PRIMARY_SUPPORTED"
        disposition = "AI_CONFIRM_CANDIDATE"
        reason_codes = ["SUPPORTED_BY_PRIMARY"]
        selected_evidence_ids = evidence_ids[:1]
        materiality = "MATERIAL_ADVERSE"
        polarity = "ADVERSE"
    return {
        "record_type": "ai_census_result",
        "schema_version": header["schema_version"],
        "contract_version": header["contract_version"],
        "batch_id": header["batch_id"],
        "reviewer_slot": header["reviewer_slot"],
        "shard_id": header["shard_id"],
        "assignment_sha256": header["assignment_sha256"],
        "event_id": packet["event_id"],
        "event_version": packet["event_version"],
        "event_fingerprint": packet["event_fingerprint"],
        "packet_sha256": packet["packet_sha256"],
        "checks": checks,
        "event_stage": "REALIZED",
        "materiality": materiality,
        "polarity": polarity,
        "evidence_state": evidence_state,
        "disposition": disposition,
        "reason_codes": reason_codes,
        "selected_evidence_ids": selected_evidence_ids,
        "possible_duplicate_event_ids": [],
        "summary": "The frozen filing identifies the event and its subject.",
        "rationale": "The exact primary passage and the event date were independently compared.",
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        **BOUNDARY_VALUES,
    }


def _submission(
    assignment: list[dict],
    *,
    disagree_event_id: str | None = None,
) -> list[dict]:
    header, packets = parse_assignment_records(assignment)
    submission_header = {
        "record_type": "submission_header",
        "schema_version": header["schema_version"],
        "contract_version": header["contract_version"],
        "batch_id": header["batch_id"],
        "reviewer_slot": header["reviewer_slot"],
        "shard_id": header["shard_id"],
        "assignment_sha256": header["assignment_sha256"],
        "ai_system": {
            "provider": "Fixture AI",
            "model": "fixture-model",
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": PROMPT_SHA256,
            "tool_mode": "MANUAL_UPLOAD",
        },
        "complete": True,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        **BOUNDARY_VALUES,
    }
    return [
        submission_header,
        *[
            _result_for(
                packet,
                header,
                disagreement=str(packet["event_id"]) == disagree_event_id,
            )
            for packet in packets
        ],
    ]


def test_extracts_every_status_read_only_and_hides_old_conclusions(census_ledger: Path) -> None:
    before = _file_hash(census_ledger)
    snapshot = extract_all_event_packets(census_ledger)
    after = _file_hash(census_ledger)

    assert before == after
    assert len(snapshot["packets"]) == 21
    assert snapshot["status_counts"] == {
        "candidate": 18,
        "rejected": 1,
        "verified": 1,
        "weak": 1,
    }
    assert all("status" not in packet for packet in snapshot["packets"])
    assert all("manual_grade" not in packet for packet in snapshot["packets"])
    assert all(packet["packet_sha256"] for packet in snapshot["packets"])
    assert len(snapshot["logical_snapshot_sha256"]) == 64
    assert all(packet["canonical_mutation_allowed"] is False for packet in snapshot["packets"])


def test_allocation_is_full_balanced_deterministic_and_five_percent(census_ledger: Path) -> None:
    packets = extract_all_event_packets(census_ledger)["packets"]
    first = allocate_packets(packets, batch_id="AIC-TEST")
    second = allocate_packets(list(reversed(packets)), batch_id="AIC-TEST")
    ids_a = {str(row["event_id"]) for row in first["slots"]["A"]}
    ids_b = {str(row["event_id"]) for row in first["slots"]["B"]}

    assert first["overlap_rate"] == OVERLAP_RATE
    assert first["overlap_count"] == 2
    assert ids_a | ids_b == {str(row["event_id"]) for row in packets}
    assert ids_a & ids_b == set(first["overlap_event_ids"])
    assert abs(len(ids_a) - len(ids_b)) <= 1
    assert first["overlap_event_ids"] == second["overlap_event_ids"]
    assert [row["event_id"] for row in first["slots"]["A"]] == [
        row["event_id"] for row in second["slots"]["A"]
    ]


def test_jsonl_submission_validation_is_strict_and_advisory(census_ledger: Path) -> None:
    packets = extract_all_event_packets(census_ledger)["packets"]
    allocation = allocate_packets(packets, batch_id="AIC-VALIDATE")
    assignment = build_assignment_shards(
        allocation,
        generated_at=datetime.now(timezone.utc).isoformat(),
        shard_size=20,
    )[0]
    valid = _submission(assignment)
    report = validate_submission_records(
        assignment,
        valid,
        batch_event_ids=[row["event_id"] for row in packets],
    )
    assert report["valid"] is True
    assert report["canonical_state_changed"] is False
    assert report["formal_verification"] is False

    invalid = json.loads(json.dumps(valid))
    invalid[1]["market_return"] = -0.45
    invalid[1]["human_reviewed"] = True
    rejected = validate_submission_records(assignment, invalid)
    assert rejected["valid"] is False
    assert any("unsupported fields" in issue for issue in rejected["issues"])
    assert any("human_reviewed must be false" in issue for issue in rejected["issues"])


def test_merge_requires_complete_shards_and_reports_overlap_disagreement(
    census_ledger: Path,
) -> None:
    packets = extract_all_event_packets(census_ledger)["packets"]
    allocation = allocate_packets(packets, batch_id="AIC-MERGE")
    assignments = build_assignment_shards(
        allocation,
        generated_at=datetime.now(timezone.utc).isoformat(),
        shard_size=5,
    )
    overlap_event_id = str(allocation["overlap_event_ids"][0])
    submissions = []
    for assignment in assignments:
        header = assignment[0]
        disagree = overlap_event_id if header["reviewer_slot"] == "B" else None
        submissions.append(_submission(assignment, disagree_event_id=disagree))

    merged = merge_census_submissions(
        assignments,
        submissions,
        expected_event_ids=[row["event_id"] for row in packets],
        overlap_event_ids=allocation["overlap_event_ids"],
    )
    assert merged["summary"]["merged_event_count"] == 21
    assert merged["summary"]["overlap_event_count"] == 2
    assert merged["summary"]["coverage_counts"]["OVERLAP_DISAGREEMENT"] == 1
    assert merged["summary"]["canonical_state_changed"] is False
    disagreement = next(
        row
        for row in merged["records"]
        if row.get("event_id") == overlap_event_id
    )
    assert disagreement["advisory_consensus"] is None
    assert disagreement["requires_human_followup"] is True

    with pytest.raises(ValueError, match="missing .* shard submissions"):
        merge_census_submissions(
            assignments,
            submissions[:-1],
            expected_event_ids=[row["event_id"] for row in packets],
            overlap_event_ids=allocation["overlap_event_ids"],
        )


def test_build_cli_artifact_contains_frozen_jsonl_manifest_and_zip(
    census_ledger: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / "delivery"
    archive = tmp_path / "delivery.zip"
    args = Namespace(
        ledger=census_ledger,
        output=output,
        zip=archive,
        batch_id="AIC-PACKAGE",
        shard_size=7,
    )
    assert ai_event_census_kit.build_command(args) == 0
    manifest = json.loads((output / "batch_manifest.json").read_text(encoding="utf-8"))
    owner_index = json.loads(
        (output / "负责人材料" / "owner_index.json").read_text(encoding="utf-8")
    )
    assert manifest["source_ledger_event_count"] == 21
    assert manifest["logical_snapshot_sha256"] == owner_index["logical_snapshot_sha256"]
    assert len(manifest["owner_index_sha256"]) == 64
    assert manifest["collective_full_coverage"] is True
    assert manifest["overlap_event_count"] == 2
    assert owner_index["event_count"] == 21
    assert len(list(output.glob("成员A/任务分片/*.input.jsonl"))) > 1
    assert len(list(output.glob("成员B/任务分片/*.input.jsonl"))) > 1
    assert (output / "成员A" / "AI审核工作台.html").is_file()
    assert (output / "成员B" / "AI审核工作台.html").is_file()
    assert archive.is_file()
    assert (output / "SHA256SUMS.csv").is_file()
    assert not any(path.name.endswith(".sqlite3") for path in output.rglob("*"))
    assert _file_hash(census_ledger) == manifest["source_ledger_sha256"]


def test_contract_file_matches_implementation() -> None:
    contract = json.loads((ROOT / "config" / "ai_census_v1.json").read_text(encoding="utf-8"))
    assert contract["contract_version"] == CONTRACT_VERSION
    assert contract["prompt_version"] == PROMPT_VERSION
    assert contract["prompt_sha256"] == PROMPT_SHA256
    assert contract["allocation"]["deterministic_overlap_rate"] == OVERLAP_RATE
    assert contract["mandatory_boundaries"] == BOUNDARY_VALUES


def test_offline_workbench_locks_boundaries_and_has_no_api_transport() -> None:
    html = (ROOT / "ai_census_kit" / "AI审核工作台.html").read_text(
        encoding="utf-8"
    )
    assert html.count("data-boundary-locked disabled") == 6
    assert "localStorage" in html
    assert "submission_header" in html
    assert "prompt_sha256:state.promptHash" in html
    assert PROMPT_SHA256 in html
    for forbidden in (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "EventSource",
        "sendBeacon",
        "api_key",
        "API key",
    ):
        assert forbidden not in html
