from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_research_quality_report import build_snapshot


class ResearchQualityReportTests(unittest.TestCase):
    def test_snapshot_separates_coverage_review_and_outcomes(self) -> None:
        snapshot = build_snapshot(
            [{"review_score": "90", "event_type": "debt_default", "evidence_readiness": "primary_text_ready"}],
            [
                {"event_candidate_id": "a", "stable_id": "p1", "event_date": "2026-01-01", "event_family": "distress"},
                {"event_candidate_id": "b", "stable_id": "p2", "event_date": "2026-01-01", "event_family": "price"},
            ],
            [
                {"event_candidate_id": "a", "passage_status": "candidate_passage"},
                {"event_candidate_id": "b", "passage_status": "no_keyword_passage"},
            ],
            [{"event_candidate_id": "b"}],
            [
                {"event_candidate_id": "a", "detected_event_type": "bankruptcy_liquidation", "label_status": "verified", "manual_grade": "S"}
            ],
        )
        self.assertEqual(snapshot["live"]["primary_text_ready_pct"], 100.0)
        self.assertEqual(snapshot["historical"]["keyword_passage_coverage_pct"], 50.0)
        self.assertEqual(snapshot["historical"]["adjudicated_pct"], 50.0)
        self.assertEqual(snapshot["historical"]["hard_labels_s_or_a_plus_plus"], 1)
        self.assertEqual(snapshot["historical"]["review_threads"], 2)
        self.assertEqual(snapshot["historical"]["adjudicated_review_thread_pct"], 50.0)
        self.assertFalse(snapshot["safety"]["trading_allowed"])

    def test_sibling_detectors_count_as_one_review_thread(self) -> None:
        queue = [
            {"event_candidate_id": "a", "stable_id": "p1", "event_date": "2026-01-01", "event_family": "price"},
            {"event_candidate_id": "b", "stable_id": "p1", "event_date": "2026-01-01", "event_family": "price"},
        ]
        snapshot = build_snapshot([], queue, [], [], [])
        self.assertEqual(snapshot["historical"]["queue_rows"], 2)
        self.assertEqual(snapshot["historical"]["review_threads"], 1)

    def test_out_of_queue_adjudicated_sibling_resolves_queued_thread(self) -> None:
        queue = [
            {
                "event_candidate_id": "queued",
                "stable_id": "p1",
                "event_date": "2026-01-01",
                "event_family": "equity_dilution",
            }
        ]
        adjudications = [
            {
                "event_candidate_id": "reviewed_sibling",
                "stable_id": "p1",
                "event_date": "2026-01-01",
                "detected_event_type": "reverse_split",
                "label_status": "verified",
                "manual_grade": "A",
            }
        ]
        snapshot = build_snapshot([], queue, [], [], adjudications)
        self.assertEqual(snapshot["historical"]["review_threads"], 1)
        self.assertEqual(snapshot["historical"]["adjudicated_review_threads"], 1)
        self.assertEqual(snapshot["historical"]["adjudicated_review_thread_pct"], 100.0)
        self.assertEqual(snapshot["historical"]["adjudicated_queue_rows"], 1)
        self.assertEqual(snapshot["historical"]["adjudicated_pct"], 100.0)

    def test_prior_batch_rows_do_not_inflate_current_queue_percentages(self) -> None:
        queue = [
            {"event_candidate_id": "current", "stable_id": "p1", "event_date": "2026-01-01", "event_family": "distress"}
        ]
        snapshot = build_snapshot(
            [],
            queue,
            [
                {"event_candidate_id": "current", "passage_status": "candidate_passage"},
                {"event_candidate_id": "prior", "passage_status": "candidate_passage"},
            ],
            [],
            [
                {"event_candidate_id": "current", "detected_event_type": "bankruptcy_liquidation", "label_status": "verified", "manual_grade": "A"},
                {"event_candidate_id": "prior", "detected_event_type": "delisted", "label_status": "rejected", "manual_grade": "rejected"},
            ],
        )
        self.assertEqual(snapshot["historical"]["events_with_keyword_passage"], 1)
        self.assertEqual(snapshot["historical"]["keyword_passage_coverage_pct"], 100.0)
        self.assertEqual(snapshot["historical"]["adjudicated"], 2)
        self.assertEqual(snapshot["historical"]["adjudicated_queue_rows"], 1)
        self.assertEqual(snapshot["historical"]["adjudicated_pct"], 100.0)

    def test_price_crash_episode_counts_as_one_review_thread(self) -> None:
        queue = [
            {"event_candidate_id": "a", "stable_id": "p1", "event_date": "2026-01-01", "event_family": "price_crash"},
            {"event_candidate_id": "b", "stable_id": "p1", "event_date": "2026-01-29", "event_family": "price_crash"},
            {"event_candidate_id": "c", "stable_id": "p1", "event_date": "2026-02-01", "event_family": "price_crash"},
        ]
        snapshot = build_snapshot([], queue, [], [], [])
        self.assertEqual(snapshot["historical"]["review_threads"], 2)

    def test_linked_consequence_does_not_inflate_hard_label_count(self) -> None:
        queue = [
            {"event_candidate_id": "primary", "stable_id": "p1", "event_date": "2026-01-01", "event_family": "distress"},
            {"event_candidate_id": "consequence", "stable_id": "p1", "event_date": "2026-01-01", "event_family": "listing"},
        ]
        adjudications = [
            {"event_candidate_id": "primary", "detected_event_type": "bankruptcy_liquidation", "label_status": "verified", "manual_grade": "A++", "training_role": "positive_boundary_after_import_review"},
            {"event_candidate_id": "consequence", "detected_event_type": "delisted", "label_status": "verified", "manual_grade": "A++", "training_role": "linked_consequence_dedup_excluded"},
        ]
        snapshot = build_snapshot([], queue, [], [], adjudications)
        self.assertEqual(snapshot["historical"]["hard_labels_s_or_a_plus_plus"], 1)

    def test_complete_history_and_closing_only_live_queue_change_the_bottleneck(self) -> None:
        queue = [
            {"event_candidate_id": "a", "stable_id": "p1", "event_date": "2026-01-01", "event_family": "distress"}
        ]
        snapshot = build_snapshot(
            [
                {
                    "review_score": "90",
                    "event_type": "offering_or_dilution",
                    "evidence_readiness": "primary_text_ready",
                    "next_action": "confirm_size_discount_warrants_and_post_money_dilution",
                }
            ],
            queue,
            [{"event_candidate_id": "a", "passage_status": "candidate_passage"}],
            [],
            [
                {
                    "event_candidate_id": "a",
                    "detected_event_type": "bankruptcy_liquidation",
                    "label_status": "verified",
                    "manual_grade": "A",
                }
            ],
        )
        self.assertEqual(snapshot["priority"][0]["work"], "monitor_terminal_primary_evidence")
        self.assertEqual(snapshot["priority"][1]["work"], "expand_auditable_candidate_generation")
        self.assertIn("historical review universe is exhausted", snapshot["interpretation"])


if __name__ == "__main__":
    unittest.main()
