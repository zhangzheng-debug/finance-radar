from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_short_label_import_review as packet


class BuildShortLabelImportReviewTests(unittest.TestCase):
    def test_import_rows_disable_training_and_match_header(self) -> None:
        header = [
            "event_source",
            "event_id",
            "company_name",
            "listing",
            "event_type_raw",
            "event_date",
            "evidence_codes",
            "label_status",
            "stable_id",
            "ticker_at_event",
            "canonical_event_family",
            "canonical_event_type",
            "R",
            "L",
            "E",
            "C",
            "P",
            "X",
            "score_total_manual",
            "hard_training_label",
            "hard_training_dedup_excluded",
            "training_bucket",
            "recommended_use",
        ]
        adjudication = {
            "event_candidate_id": "C1",
            "stable_id": "permaticker:1",
            "ticker_at_event": "AAA",
            "event_date": "2026-01-01",
            "detected_event_type": "bankruptcy_liquidation",
            "evidence_date": "2026-01-01",
            "evidence_url": "https://www.sec.gov/example",
            "evidence_summary": "Primary evidence.",
            "label_status": "verified",
            "canonical_event_family": "distress_equity_death",
            "canonical_event_type": "chapter_11",
            "R": "3",
            "L": "3",
            "E": "2",
            "C": "3",
            "P": "0",
            "X": "-1",
            "score_total": "10",
            "manual_grade": "A++",
            "training_role": "positive_boundary_after_import_review",
            "adjudication_note": "Review first.",
        }
        queue = {
            "C1": {
                "company_name": "AAA Corp",
                "exchange": "NASDAQ",
                "ticker_at_event": "AAA",
                "industry": "Software",
            }
        }
        rows = packet.build_import_rows([adjudication], queue, header)
        self.assertEqual(list(rows[0]), header)
        self.assertEqual(rows[0]["hard_training_label"], "false")
        self.assertEqual(rows[0]["hard_training_dedup_excluded"], "true")
        self.assertEqual(rows[0]["training_bucket"], "pending_verified_severe_import_review")

    def test_linked_consequence_uses_shared_chain_and_stays_dedup_excluded(self) -> None:
        header = ["event_id", "event_chain_id", "event_chain_role", "hard_training_dedup_reason", "recommended_use"]
        adjudication = {
            "event_candidate_id": "C2", "stable_id": "permaticker:2", "ticker_at_event": "BBB",
            "event_date": "2026-02-02", "detected_event_type": "delisted", "evidence_date": "2026-02-02",
            "evidence_url": "https://www.sec.gov/example", "evidence_summary": "Bankruptcy-driven delisting.",
            "label_status": "verified", "canonical_event_family": "listing_status",
            "canonical_event_type": "bankruptcy_driven_delisting", "R": "3", "L": "3", "E": "2",
            "C": "3", "P": "0", "X": "-1", "score_total": "10", "manual_grade": "A++",
            "training_role": "linked_consequence_dedup_excluded", "adjudication_note": "Same chain.",
        }
        rows = packet.build_import_rows([adjudication], {"C2": {"company_name": "BBB", "exchange": "NASDAQ", "ticker_at_event": "BBB"}}, header)
        self.assertEqual(rows[0]["event_chain_id"], "FR-permaticker:2-2026-02-02")
        self.assertEqual(rows[0]["event_chain_role"], "consequence")
        self.assertEqual(rows[0]["hard_training_dedup_reason"], "linked_consequence_same_event_chain")

    def test_rotated_queue_falls_back_to_durable_adjudication_identity(self) -> None:
        header = ["event_id", "listing", "stable_id", "ticker_at_event", "event_date"]
        adjudication = {
            "event_candidate_id": "OLD-C1",
            "stable_id": "permaticker:99",
            "ticker_at_event": "OLD",
            "event_date": "2025-03-01",
            "detected_event_type": "chapter_11",
            "evidence_date": "2025-03-02",
            "evidence_url": "https://www.sec.gov/example",
            "evidence_summary": "Primary evidence.",
            "label_status": "verified",
            "canonical_event_family": "distress_equity_death",
            "canonical_event_type": "chapter_11",
            "R": "3", "L": "3", "E": "2", "C": "3", "P": "0", "X": "-1",
            "score_total": "10", "manual_grade": "A++",
            "training_role": "positive_boundary_after_import_review",
            "adjudication_note": "Review first.",
        }
        row = packet.build_import_rows([adjudication], {}, header)[0]
        self.assertEqual(row["stable_id"], "permaticker:99")
        self.assertEqual(row["ticker_at_event"], "OLD")
        self.assertEqual(row["listing"], "OLD")


if __name__ == "__main__":
    unittest.main()
