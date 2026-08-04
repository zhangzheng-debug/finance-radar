from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger, utc_now
import run_live_cycle as cycle


class LiveCycleLeaseTests(unittest.TestCase):
    def test_cycle_lease_is_single_owner_and_releasable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            first = cycle.acquire_cycle_lease(connection)
            self.assertIsNotNone(first)
            self.assertIsNone(cycle.acquire_cycle_lease(connection))
            cycle.release_cycle_lease(connection, first)
            second = cycle.acquire_cycle_lease(connection)
            self.assertIsNotNone(second)
            cycle.release_cycle_lease(connection, second)
            connection.close()

    def test_light_followup_with_evidence_advances_to_human_review_without_touching_other_jobs(self) -> None:
        class ExistingDecisionOperations:
            def agent_decisions(self, event_id: str, *, limit: int = 1):
                self.assertEqual(event_id, "evt-light")
                self.assertEqual(limit, 1)
                return [{"decision_id": "existing"}]

        class NeverRunEvidenceAgent:
            def run(self, event_id: str):
                raise AssertionError(f"existing decision should avoid re-running evidence agent for {event_id}")

        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            now = utc_now()
            connection.execute(
                "INSERT INTO sources VALUES ('src','Source','official_primary','P0',1,1,?,?)",
                (now, now),
            )
            connection.execute(
                """INSERT INTO raw_observations VALUES (
                   'obs-light','src','stable-light','2026-08-01',?,'Example','Example',
                   'https://example.com/light','hash','{}','captured')""",
                (now,),
            )
            connection.execute(
                """INSERT INTO canonical_events VALUES (
                   'evt-light',1,'candidate','candidate','regulatory','filing','2026-08-01',
                   ?,?,'stable',NULL,'Example',NULL,NULL,'src',1)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO event_versions VALUES (
                   'evt-light',1,?,'candidate','candidate','regulatory','filing',NULL,'{}','fixture')""",
                (now,),
            )
            connection.execute(
                """INSERT INTO event_evidence VALUES (
                   'ev-light','evt-light','obs-light','https://example.com/light','2026-08-01',NULL,NULL,
                   'A machine-extracted official evidence passage is present for the follow-up review.',
                   'evidence',10,'machine_extracted_unreviewed',0,?,?)""",
                (now, now),
            )
            for job_id, job_type in (
                ("light-followup", "light_verification_followup"),
                ("unrelated-evidence-job", "historical_evidence_review"),
            ):
                connection.execute(
                    """INSERT INTO pipeline_jobs VALUES (
                       ?,?,?,'PENDING_EVIDENCE_REVIEW',50,0,?,NULL,'{}',?,?)""",
                    (job_id, "evt-light", job_type, now, now, now),
                )
            # This mirrors a legacy reconciliation: preserve the historical
            # verified conclusion while opening an explicit evidence/human
            # follow-up.  It must not be treated as an ordinary candidate.
            connection.execute(
                "UPDATE canonical_events SET status='verified',label_status='verified' WHERE event_id='evt-light'"
            )
            connection.commit()

            # Bind the local unittest assertion to the simple adapter method.
            operations = ExistingDecisionOperations()
            operations.assertEqual = self.assertEqual
            result = cycle.run_pending_evidence_agents(
                connection,
                NeverRunEvidenceAgent(),
                operations,
                limit=4,
            )
            statuses = {
                row["job_id"]: row["status"]
                for row in connection.execute(
                    "SELECT job_id,status FROM pipeline_jobs ORDER BY job_id"
                )
            }
            self.assertEqual(result["selected"], 1)
            self.assertEqual(result["already_run"], 1)
            self.assertEqual(result["by_job_type"], {"light_verification_followup": 1})
            self.assertEqual(statuses["light-followup"], "PENDING_HUMAN_REVIEW")
            self.assertEqual(statuses["unrelated-evidence-job"], "PENDING_EVIDENCE_REVIEW")
            canonical = connection.execute(
                "SELECT status,label_status FROM canonical_events WHERE event_id='evt-light'"
            ).fetchone()
            self.assertEqual(tuple(canonical), ("verified", "verified"))
            connection.close()


if __name__ == "__main__":
    unittest.main()
