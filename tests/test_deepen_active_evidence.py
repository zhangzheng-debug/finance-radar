from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import deepen_active_evidence as deepener


class DeepenActiveEvidenceTests(unittest.TestCase):
    def test_cause_unresolved_outranks_higher_scored_ordinary_action(self) -> None:
        rows = [
            {
                "event_candidate_id": "ACTION",
                "review_bucket": "ordinary_corporate_action",
                "proposed_disposition": "verify_action_only",
                "evidence_readiness": "primary_passage_ready",
                "review_score": "90",
                "review_rank": "1",
            },
            {
                "event_candidate_id": "CAUSE",
                "review_bucket": "delisting_cause_review",
                "proposed_disposition": "cause_unresolved",
                "evidence_readiness": "filing_link_only",
                "review_score": "60",
                "review_rank": "2",
            },
        ]
        selected = deepener.select_targets(rows, 1)
        self.assertEqual(selected[0]["event_candidate_id"], "CAUSE")

    def test_non_gap_bucket_is_not_selected(self) -> None:
        rows = [
            {
                "event_candidate_id": "DONE",
                "review_bucket": "recapitalization_dilution_review",
                "review_score": "100",
                "review_rank": "1",
            }
        ]
        self.assertEqual(deepener.select_targets(rows, 10), [])


if __name__ == "__main__":
    unittest.main()
