from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_detector_precision_report as report


class DetectorPrecisionReportTests(unittest.TestCase):
    def test_separates_acceptance_severity_and_chain_exclusion(self) -> None:
        rows = report.precision_rows(
            [
                {
                    "event_candidate_id": "A",
                    "stable_id": "perm:1",
                    "event_date": "2025-01-01",
                    "detected_event_type": "one_day_crash",
                    "label_status": "verified",
                    "manual_grade": "A++",
                    "training_role": "linked_consequence_dedup_excluded",
                },
                {
                    "event_candidate_id": "B",
                    "stable_id": "perm:2",
                    "event_date": "2025-02-01",
                    "detected_event_type": "one_day_crash",
                    "label_status": "rejected",
                    "manual_grade": "rejected",
                    "training_role": "rejected_price_only_control",
                },
            ]
        )
        detector = next(row for row in rows if row["group_name"] == "one_day_crash")
        self.assertEqual(detector["verified_rows"], 1)
        self.assertEqual(detector["rejected_rows"], 1)
        self.assertEqual(detector["acceptance_rate_pct"], 50.0)
        self.assertEqual(detector["training_eligible_verified_rows"], 0)
        self.assertEqual(detector["training_eligible_a_or_higher_rows"], 0)
        self.assertEqual(detector["training_eligible_s_or_a_plus_plus_rows"], 0)
        self.assertEqual(detector["linked_consequence_excluded"], 1)
        self.assertEqual(detector["false_positive_controls"], 1)


if __name__ == "__main__":
    unittest.main()
