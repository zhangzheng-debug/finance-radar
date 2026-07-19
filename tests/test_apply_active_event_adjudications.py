from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import apply_active_event_adjudications as adjudicator
from manual_historical_findings import load_manual_findings


class ActiveEventAdjudicationTests(unittest.TestCase):
    def queue(self) -> list[dict[str, str]]:
        return [{
            "event_candidate_id": "CAND-1",
            "stable_id": "permaticker:1",
            "ticker_at_event": "AAA",
            "event_date": "2025-01-02",
            "event_type": "bankruptcy_liquidation",
        }]

    def passages(self) -> list[dict[str, str]]:
        return [{
            "event_candidate_id": "CAND-1",
            "filing_document_url": "https://www.sec.gov/example",
        }]

    def decision(self) -> dict[str, object]:
        return {
            "event_candidate_id": "CAND-1",
            "evidence_date": "2025-01-03",
            "evidence_form": "8-K",
            "evidence_item": "8.01",
            "evidence_url": "https://www.sec.gov/example",
            "evidence_summary": "Board approved a Chapter 7 petition.",
            "label_status": "verified",
            "canonical_event_family": "distress_equity_death",
            "canonical_event_type": "chapter_7_liquidation",
            "scores": {"R": 3, "L": 3, "E": 2, "C": 3, "P": 0, "X": -1},
            "manual_grade": "A++",
            "training_role": "positive_boundary",
            "adjudication_note": "No S label without explicit old-common outcome.",
        }

    def test_builds_queue_identity_and_validated_score(self) -> None:
        row = adjudicator.build_rows([self.decision()], self.queue(), self.passages())[0]
        self.assertEqual(row["stable_id"], "permaticker:1")
        self.assertEqual(row["score_total"], "10")
        self.assertEqual(row["manual_grade"], "A++")

    def test_manual_grade_may_conflict_with_rule_priority(self) -> None:
        decision = self.decision()
        decision["manual_grade"] = "A"
        row = adjudicator.build_rows([decision], self.queue(), self.passages())[0]
        self.assertEqual(row["score_total"], "10")
        self.assertEqual(row["manual_grade"], "A")

    def test_requires_evidence_from_extracted_passages(self) -> None:
        decision = self.decision()
        decision["evidence_url"] = "https://example.test/unseen"
        with self.assertRaisesRegex(ValueError, "not an extracted candidate passage"):
            adjudicator.build_rows([decision], self.queue(), self.passages())

    def test_accepts_registered_external_official_evidence(self) -> None:
        decision = self.decision()
        decision["evidence_url"] = "https://official.example/order.pdf"
        external = [{
            "event_candidate_id": "CAND-1",
            "evidence_date": "2025-01-03",
            "evidence_form": "Regulator order",
            "evidence_url": "https://official.example/order.pdf",
            "evidence_summary": "The regulator issued an official order.",
            "source_name": "Official regulator",
        }]
        row = adjudicator.build_rows(
            [decision], self.queue(), self.passages(), external
        )[0]
        self.assertEqual(row["evidence_url"], "https://official.example/order.pdf")

    def test_rejected_control_must_have_zero_total(self) -> None:
        decision = self.decision()
        decision["label_status"] = "rejected"
        decision["manual_grade"] = "rejected"
        with self.assertRaisesRegex(ValueError, "score total 0"):
            adjudicator.build_rows([decision], self.queue(), self.passages())

    def test_upsert_is_idempotent(self) -> None:
        row = adjudicator.build_rows([self.decision()], self.queue(), self.passages())[0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "adjudications.csv"
            first = adjudicator.upsert_rows(path, [row])
            second = adjudicator.upsert_rows(path, [row])
            self.assertEqual(first["inserted"], 1)
            self.assertEqual(second["unchanged"], 1)
            with path.open("r", encoding="utf-8", newline="") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 1)

    def test_prior_adjudication_context_survives_queue_rotation(self) -> None:
        existing = {
            "event_candidate_id": "CAND-OLD",
            "stable_id": "permaticker:9",
            "ticker_at_event": "OLD",
            "event_date": "2024-01-02",
            "detected_event_type": "delisted",
            "evidence_url": "https://www.sec.gov/old-evidence",
        }
        queue_rows, passage_rows = adjudicator.prior_adjudication_context([existing])
        decision = self.decision()
        decision.update(
            {
                "event_candidate_id": "CAND-OLD",
                "evidence_url": "https://www.sec.gov/old-evidence",
            }
        )
        row = adjudicator.build_rows([decision], queue_rows, passage_rows)[0]
        self.assertEqual(row["stable_id"], "permaticker:9")
        self.assertEqual(row["detected_event_type"], "delisted")

    def test_prior_context_does_not_allow_unregistered_new_evidence(self) -> None:
        existing = {
            "event_candidate_id": "CAND-OLD",
            "stable_id": "permaticker:9",
            "ticker_at_event": "OLD",
            "event_date": "2024-01-02",
            "detected_event_type": "delisted",
            "evidence_url": "https://www.sec.gov/old-evidence",
        }
        queue_rows, passage_rows = adjudicator.prior_adjudication_context([existing])
        decision = self.decision()
        decision.update(
            {
                "event_candidate_id": "CAND-OLD",
                "evidence_url": "https://www.sec.gov/new-unseen-evidence",
            }
        )
        with self.assertRaisesRegex(ValueError, "not an extracted candidate passage"):
            adjudicator.build_rows([decision], queue_rows, passage_rows)

    def test_manual_finding_is_queue_compatible_and_time_separate(self) -> None:
        payload = {
            "schema_version": "manual-historical-findings-v1",
            "findings": [{
                "event_candidate_id": "MANUAL-SEC-AAA-20250110-TERMINAL",
                "stable_id": "cik:1",
                "ticker_at_event": "AAA",
                "company_name": "AAA Inc.",
                "event_date": "2025-01-10",
                "event_family": "distress_equity_death",
                "event_type": "old_common_cancelled_without_consideration",
                "detection_rule": "official terminal filing",
                "detection_value": "old common cancelled",
                "priority_score": "150",
                "provisional_grade_cap": "S_deep_review_only",
                "required_evidence": "confirmed plan",
                "evidence_search_query": "AAA confirmed plan",
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual.json"
            path.write_text(__import__("json").dumps(payload), encoding="utf-8")
            row = load_manual_findings(path)[0]
        self.assertEqual(row["event_date"], "2025-01-10")
        self.assertEqual(row["source_table"], "MANUAL_OFFICIAL_PRIMARY_DISCOVERY")
        self.assertEqual(row["allowed_use"], "manual_research_priority_only_no_trading")

    def test_manual_finding_requires_manual_prefix(self) -> None:
        payload = {
            "schema_version": "manual-historical-findings-v1",
            "findings": [{field: "x" for field in (
                "event_candidate_id", "stable_id", "ticker_at_event", "company_name",
                "event_date", "event_family", "event_type", "detection_rule",
                "detection_value", "priority_score", "provisional_grade_cap",
                "required_evidence", "evidence_search_query",
            )}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual.json"
            path.write_text(__import__("json").dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must start with MANUAL-"):
                load_manual_findings(path)


if __name__ == "__main__":
    unittest.main()
