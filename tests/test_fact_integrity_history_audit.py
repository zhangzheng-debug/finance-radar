from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from app.storage.operations import OperationsRepository
from scripts.audit_fact_integrity_history import (
    AGENT_REJECTED,
    LIGHT_REASON_CONTRACT_MISMATCH,
    LIGHT_REASON_GATE_NOT_SUPPORTED,
    LIGHT_REVIEW,
    build_report,
    open_read_only,
    write_markdown,
)
from scripts.event_ledger import open_ledger, stable_json, utc_now


def _seed_ledger(path: Path) -> None:
    connection = open_ledger(path)
    now = utc_now()
    connection.execute(
        "INSERT INTO sources VALUES (?,?,?,?,1,1,?,?)",
        ("sec", "SEC", "official_primary", "P0", now, now),
    )
    connection.execute(
        """INSERT INTO raw_observations VALUES (
           'obs','sec','external',?,?,'SEC filing','ACME quarterly results',
           'https://www.sec.gov/Archives/acme','sha','{}','active')""",
        ("2025-01-03T00:00:00+00:00", now),
    )
    connection.execute(
        """INSERT INTO canonical_events VALUES (
           'evt',2,'verified','verified','delisting_or_suspension','delisted',
           '2025-01-02',?,?,NULL,'ACME','ACME HOLDINGS INC',NULL,NULL,'test',1)""",
        (now, now),
    )
    connection.execute(
        "INSERT INTO event_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "evt",
            1,
            now,
            "candidate",
            "candidate",
            "delisting_or_suspension",
            "delisted",
            None,
            stable_json({"evidence_summary": "ACME entered Chapter 11."}),
            "seed",
        ),
    )
    connection.execute(
        "INSERT INTO event_versions VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "evt",
            2,
            now,
            "verified",
            "verified",
            "delisting_or_suspension",
            "delisted",
            None,
            stable_json(
                {
                    "evidence_summary": "ACME entered Chapter 11.",
                    "light_verification": {
                        "version": "light-evidence-gate-v2",
                        "formal_conclusion": "verified",
                        "evidence_ids": ["evidence"],
                    },
                }
            ),
            "light_evidence_verification_v2",
        ),
    )
    passage = (
        "ACME HOLDINGS INC reported quarterly results and cash balances for the period. "
        "Its customer BETA CORP was determined to delist its common shares from the exchange."
    )
    connection.execute(
        """INSERT INTO event_evidence VALUES (
           'evidence','evt','obs','https://www.sec.gov/Archives/acme','2025-01-03',
           '8-K','8.01',?,'delist',100,'accepted_light_primary_evidence',0,?,?)""",
        (passage, now, now),
    )
    connection.commit()
    connection.close()


def _seed_operations(path: Path) -> None:
    operations = OperationsRepository(path)
    operations.record_agent_decision(
        {
            "event_id": "evt",
            "trace_id": "trace-old",
            "status": "EVIDENCE_READY",
            "prompt_version": "evidence-agent-contract-v3",
            "model_provider": "deterministic_guarded_fallback",
            "model_snapshot": "no-llm-configured-v1",
            "claims": [
                {
                    "claim_id": "claim",
                    "text": "ACME entered Chapter 11.",
                }
            ],
            "evidence_edges": [
                {
                    "claim_id": "claim",
                    "evidence_id": "irrelevant-fed",
                    "exact_excerpt": (
                        "Federal Reserve officials published routine meeting minutes "
                        "about monetary policy and inflation expectations."
                    ),
                },
                {
                    "claim_id": "claim",
                    "evidence_id": "irrelevant-weather",
                    "exact_excerpt": (
                        "The National Weather Service issued a routine seasonal outlook "
                        "for coastal rainfall and temperatures."
                    ),
                },
            ],
            "guardrails": {},
            "tool_calls": [],
            "latency_ms": 1.0,
        }
    )


def _write_legacy_config(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "adjudications": [
                    {
                        "event_id": "evt",
                        "status": "verified",
                        "company_name": "ACME HOLDINGS INC",
                        "event_type": "delisted",
                        "manual_grade": "A",
                        "scores": {"R": 2, "L": 2, "E": 1, "C": 1, "P": 1, "X": 0},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_history_audit_is_read_only_and_exposes_both_old_false_positive_paths(
    tmp_path: Path,
) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    operations_path = tmp_path / "operations.sqlite3"
    config_path = tmp_path / "legacy.json"
    _seed_ledger(ledger_path)
    _seed_operations(operations_path)
    _write_legacy_config(config_path)

    with closing(open_read_only(ledger_path)) as ledger, closing(
        open_read_only(operations_path)
    ) as operations:
        before = tuple(
            ledger.execute(
                "SELECT status,current_version FROM canonical_events WHERE event_id='evt'"
            ).fetchone()
        )
        report = build_report(ledger, operations, config_path)
        after = tuple(
            ledger.execute(
                "SELECT status,current_version FROM canonical_events WHERE event_id='evt'"
            ).fetchone()
        )

    assert before == after == ("verified", 2)
    assert report["canonical_mutation_attempted"] is False
    assert report["agent_decisions"]["counts"] == {AGENT_REJECTED: 1}
    assert report["agent_decisions"]["classification_unit"] == "agent_decision"
    # Classifications count decisions; rejected_edge_total counts the rejected
    # edges inside them. One affected decision can therefore contain two edges.
    assert report["agent_decisions"]["rejected_edge_total"] == 2
    assert report["light_verifications"]["counts"] == {LIGHT_REVIEW: 1}
    assert report["light_verifications"]["review_reason_counts"] == {
        LIGHT_REASON_CONTRACT_MISMATCH: 1,
        LIGHT_REASON_GATE_NOT_SUPPORTED: 1,
    }
    assert report["light_verifications"]["records"][0]["current_dry_run_decision"] == "INSUFFICIENT"
    assert report["legacy_review_config"]["unproven"] == 1
    assert report["legacy_review_config"]["unproven_canonical_verified"] == 1
    assert report["affected_manifests"]["agent_decisions"] == [
        {
            "decision_id": report["agent_decisions"]["decisions"][0]["decision_id"],
            "event_id": "evt",
            "classification": AGENT_REJECTED,
            "rejected_edge_count": 2,
        }
    ]
    assert report["affected_manifests"]["light_formalizations"][0][
        "review_reasons"
    ] == [LIGHT_REASON_GATE_NOT_SUPPORTED, LIGHT_REASON_CONTRACT_MISMATCH]
    assert report["affected_manifests"]["legacy_unproven_canonical_verified"][0][
        "event_id"
    ] == "evt"

    markdown = tmp_path / "audit.md"
    write_markdown(markdown, report)
    text = markdown.read_text(encoding="utf-8")
    assert "Canonical mutation attempted: `false`" in text
    assert "Classification unit: `agent_decision`" in text
    assert "Rejected stored edges across those decisions: `2`" in text
    assert "Federal Reserve officials" not in text
    assert "BETA CORP" not in text

    with closing(sqlite3.connect(ledger_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM event_versions").fetchone()[0] == 2
