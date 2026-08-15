from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from app.services import evidence_receipt_fingerprint
from app.storage import LedgerRepository

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger, utc_now
import run_live_cycle as cycle


def _open_pending_candidate_job(path: Path):
    connection = open_ledger(path)
    now = utc_now()
    connection.execute(
        "INSERT INTO sources VALUES ('src','Source','official_primary','P0',1,1,?,?)",
        (now, now),
    )
    connection.execute(
        """INSERT INTO raw_observations VALUES (
           'obs-current','src','stable-current','2026-08-01',?,'Example','Example',
           'https://example.com/current','hash','{}','captured')""",
        (now,),
    )
    connection.execute(
        """INSERT INTO canonical_events VALUES (
           'evt-current',2,'candidate','candidate','regulatory','filing','2026-08-01',
           ?,?,'stable',NULL,'Example',NULL,NULL,'src',1)""",
        (now, now),
    )
    connection.execute(
        """INSERT INTO event_versions VALUES (
           'evt-current',2,?,'candidate','candidate','regulatory','filing',NULL,'{}','fixture')""",
        (now,),
    )
    connection.execute(
        """INSERT INTO event_evidence VALUES (
           'ev-current','evt-current','obs-current','https://example.com/current','2026-08-01',NULL,NULL,
           'A current official evidence passage supports the candidate review.',
           'evidence',10,'machine_extracted_unreviewed',0,?,?)""",
        (now, now),
    )
    connection.execute(
        """INSERT INTO pipeline_jobs VALUES (
           'job-current','evt-current','live_primary_evidence_review','PENDING_EVIDENCE_REVIEW',50,0,?,NULL,'{}',?,?)""",
        (now, now, now),
    )
    connection.commit()
    return connection


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

    def test_lease_ttl_covers_twice_the_outer_worker_timeout(self) -> None:
        self.assertEqual(cycle.cycle_lease_ttl_seconds(30), 1200)
        self.assertEqual(cycle.cycle_lease_ttl_seconds(5), 900)

    def test_cycle_lease_can_be_renewed_by_its_current_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            start = cycle.dt.datetime(2026, 8, 15, tzinfo=cycle.dt.timezone.utc)
            token = cycle.acquire_cycle_lease(connection, ttl_seconds=10, now=start)
            self.assertIsNotNone(token)
            self.assertTrue(
                cycle.renew_cycle_lease(
                    connection,
                    token,
                    ttl_seconds=10,
                    now=start + cycle.dt.timedelta(seconds=8),
                )
            )
            self.assertIsNone(
                cycle.acquire_cycle_lease(
                    connection,
                    ttl_seconds=10,
                    now=start + cycle.dt.timedelta(seconds=11),
                )
            )
            cycle.release_cycle_lease(connection, token)
            connection.close()

    def test_lease_heartbeat_keeps_a_long_cycle_single_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "db.sqlite3"
            connection = open_ledger(db_path)
            token = cycle.acquire_cycle_lease(connection, ttl_seconds=1)
            self.assertIsNotNone(token)
            heartbeat = cycle.CycleLeaseHeartbeat(
                db_path,
                token,
                ttl_seconds=1,
                interval_seconds=0.05,
            )
            heartbeat.start()
            try:
                time.sleep(1.2)
                self.assertIsNone(cycle.acquire_cycle_lease(connection, ttl_seconds=1))
                self.assertFalse(heartbeat.lost)
                self.assertIsNone(heartbeat.last_error)
            finally:
                heartbeat.stop()
                cycle.release_cycle_lease(connection, token)
                connection.close()

    def test_lease_heartbeat_survives_a_busy_main_cycle_connection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "db.sqlite3"
            connection = open_ledger(db_path)
            token = cycle.acquire_cycle_lease(connection, ttl_seconds=2)
            self.assertIsNotNone(token)
            heartbeat = cycle.CycleLeaseHeartbeat(
                db_path,
                token,
                ttl_seconds=2,
                interval_seconds=0.05,
            )
            connection.execute("BEGIN IMMEDIATE")
            heartbeat.start()
            try:
                time.sleep(0.25)
                self.assertTrue(heartbeat._thread.is_alive())
                self.assertIsNotNone(heartbeat.last_error)
                connection.rollback()
                deadline = time.monotonic() + 2.0
                while heartbeat.last_error is not None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertIsNone(heartbeat.last_error)
                self.assertFalse(heartbeat.lost)
            finally:
                connection.rollback()
                heartbeat.stop()
                cycle.release_cycle_lease(connection, token)
                connection.close()

    def test_light_followup_with_evidence_advances_to_human_review_without_touching_other_jobs(self) -> None:
        class ExistingDecisionOperations:
            def __init__(self, decision):
                self.decision = decision

            def agent_decisions(self, event_id: str, *, limit: int = 1):
                self.assertEqual(event_id, "evt-light")
                self.assertEqual(limit, 1)
                return [self.decision]

        class NeverRunEvidenceAgent:
            def __init__(self, ledger):
                self.ledger = ledger

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

            ledger_repository = LedgerRepository(Path(directory) / "db.sqlite3")
            detail = ledger_repository.event_detail("evt-light")
            self.assertIsNotNone(detail)
            evidence = ledger_repository.event_evidence("evt-light")
            event_version = int(detail["event"]["current_version"])
            # Bind the local unittest assertion to the simple adapter method.
            operations = ExistingDecisionOperations(
                {
                    "decision_id": "existing",
                    "output": {
                        "event_version": event_version,
                        "evidence_receipt_fingerprint": evidence_receipt_fingerprint(
                            event_version,
                            evidence,
                        ),
                    },
                }
            )
            operations.assertEqual = self.assertEqual
            result = cycle.run_pending_evidence_agents(
                connection,
                NeverRunEvidenceAgent(ledger_repository),
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
            self.assertEqual(result["stale_or_legacy_rerun"], 0)
            self.assertEqual(result["by_job_type"], {"light_verification_followup": 1})
            self.assertEqual(statuses["light-followup"], "PENDING_HUMAN_REVIEW")
            self.assertEqual(statuses["unrelated-evidence-job"], "PENDING_EVIDENCE_REVIEW")
            canonical = connection.execute(
                "SELECT status,label_status FROM canonical_events WHERE event_id='evt-light'"
            ).fetchone()
            self.assertEqual(tuple(canonical), ("verified", "verified"))
            connection.close()

    def test_legacy_or_stale_evidence_or_version_decision_reruns_the_agent(self) -> None:
        class ExistingDecisionOperations:
            def __init__(self, decision):
                self.decision = decision

            def agent_decisions(self, event_id: str, *, limit: int = 1):
                self.assertEqual(event_id, "evt-current")
                self.assertEqual(limit, 1)
                return [self.decision]

        class RecordingEvidenceAgent:
            def __init__(self, ledger):
                self.ledger = ledger
                self.calls: list[str] = []

            def run(self, event_id: str):
                self.calls.append(event_id)
                return {"status": "INSUFFICIENT"}

        for stale_kind in ("legacy", "evidence", "version"):
            with self.subTest(stale_kind=stale_kind), tempfile.TemporaryDirectory() as directory:
                db_path = Path(directory) / "db.sqlite3"
                connection = _open_pending_candidate_job(db_path)
                ledger_repository = LedgerRepository(db_path)
                detail = ledger_repository.event_detail("evt-current")
                self.assertIsNotNone(detail)
                event_version = int(detail["event"]["current_version"])
                evidence = ledger_repository.event_evidence("evt-current")
                recorded_version = event_version
                recorded_evidence = evidence
                if stale_kind == "evidence":
                    recorded_evidence = [dict(item) for item in evidence]
                    recorded_evidence[0]["evidence_passage"] = "An older superseded passage."
                elif stale_kind == "version":
                    recorded_version = event_version - 1
                output = (
                    {}
                    if stale_kind == "legacy"
                    else {
                        "event_version": recorded_version,
                        "evidence_receipt_fingerprint": evidence_receipt_fingerprint(
                            recorded_version,
                            recorded_evidence,
                        ),
                    }
                )
                operations = ExistingDecisionOperations(
                    {
                        "decision_id": f"stale-{stale_kind}",
                        "output": output,
                    }
                )
                operations.assertEqual = self.assertEqual
                evidence_agent = RecordingEvidenceAgent(ledger_repository)

                result = cycle.run_pending_evidence_agents(
                    connection,
                    evidence_agent,
                    operations,
                    limit=4,
                )

                self.assertEqual(evidence_agent.calls, ["evt-current"])
                self.assertEqual(result["run"], 1)
                self.assertEqual(result["already_run"], 0)
                self.assertEqual(result["stale_or_legacy_rerun"], 1)
                status = connection.execute(
                    "SELECT status FROM pipeline_jobs WHERE job_id='job-current'"
                ).fetchone()[0]
                self.assertEqual(status, "PENDING_HUMAN_REVIEW")
                connection.close()


if __name__ == "__main__":
    unittest.main()
