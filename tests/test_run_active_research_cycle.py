from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_active_research_cycle as cycle


class ActiveResearchCycleTests(unittest.TestCase):
    def test_partial_batch_does_not_advance_durable_cursor(self) -> None:
        self.assertEqual(
            cycle.committed_next_offset(
                offset=25,
                batch_rows=25,
                queue_rows=150,
                evidence_errors=(),
                passage_errors=("temporary TLS failure",),
            ),
            25,
        )
        self.assertEqual(
            cycle.committed_next_offset(
                offset=25,
                batch_rows=25,
                queue_rows=150,
                evidence_errors=(),
                passage_errors=(),
            ),
            50,
        )

    def test_merge_rows_is_idempotent_and_incoming_wins(self) -> None:
        existing = [{"queue_rank": "1", "event_candidate_id": "E", "accession_number": "A", "value": "old"}]
        incoming = [{"queue_rank": "1", "event_candidate_id": "E", "accession_number": "A", "value": "new"}]
        rows = cycle.merge_rows(existing, incoming, ("event_candidate_id", "accession_number"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], "new")

    def test_initial_offset_preserves_existing_first_batch(self) -> None:
        self.assertEqual(
            cycle.infer_initial_offset(None, current_hash="x", initial_batch_size=25, existing_evidence=True),
            25,
        )
        self.assertEqual(
            cycle.infer_initial_offset(
                {"queue_sha256": "x", "next_offset": 50},
                current_hash="x",
                initial_batch_size=25,
                existing_evidence=True,
            ),
            50,
        )

    def test_changed_queue_hash_restarts_at_first_row_despite_old_evidence(self) -> None:
        self.assertEqual(
            cycle.infer_initial_offset(
                {"queue_sha256": "old", "next_offset": 150},
                current_hash="new",
                initial_batch_size=25,
                existing_evidence=True,
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
