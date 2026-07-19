from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import (
    open_ledger,
    record_source_observation,
    stable_id,
    upsert_source,
    utc_now,
)
import apply_live_primary_adjudications as adjudicator


class LivePrimaryAdjudicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = open_ledger(Path(self.temp_dir.name) / "db.sqlite3")
        now = utc_now()
        self.connection.execute(
            """INSERT INTO canonical_events VALUES (
               'evt',1,'candidate','candidate','security','incident','2026-07-15',?,?,NULL,NULL,
               NULL,NULL,'B','opennews',1)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO event_versions VALUES (
               'evt',1,?,'candidate','candidate','security','incident',NULL,'{}','test')""",
            (now,),
        )
        self.connection.execute(
            """INSERT INTO pipeline_jobs VALUES (
               'job','evt','live_primary_evidence_review','PENDING_PRIMARY_EVIDENCE',90,0,?,NULL,'{}',?,?)""",
            (now, now, now),
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def row(self) -> dict[str, object]:
        return {
            "event_id": "evt",
            "status": "verified",
            "event_family": "security_incident",
            "event_type": "trading_paused",
            "event_date": "2026-07-15",
            "manual_grade": "A",
            "credibility_tier": "P1",
            "scores": {"R": 2, "L": 0, "E": 2, "C": 2, "P": 0, "X": -1},
            "score_rationale": "reviewed",
            "company_name": "Example",
            "ticker_at_event": None,
            "source_id": "official",
            "source_name": "Official",
            "source_type": "project_primary",
            "authority_tier": "P1",
            "external_id": "post-1",
            "evidence_url": "https://example.test/post-1",
            "evidence_title": "Official incident notice",
            "evidence_passage": "The project paused trading while investigating.",
            "confirmed_facts": ["Trading paused."],
            "unconfirmed_facts": ["Loss amount."],
            "event_chain": {
                "chain_id": "CHAIN-example",
                "chain_type": "incident_episode",
                "canonical_key": "example-2026-07-15",
                "chain_role": "primary_event",
                "counts_as_primary_event": True,
                "rationale": "The first confirmed incident notice is the primary event.",
            },
        }

    def test_reviewed_evidence_scores_and_version_are_idempotent(self) -> None:
        first = adjudicator.apply_rows(self.connection, [self.row()])
        second = adjudicator.apply_rows(self.connection, [self.row()])
        self.assertEqual(first["applied"], 1)
        self.assertEqual(second["already_applied"], 1)
        event = self.connection.execute("SELECT * FROM canonical_events").fetchone()
        self.assertEqual(event["status"], "verified")
        self.assertEqual(event["current_version"], 2)
        assessment = self.connection.execute("SELECT * FROM event_assessments").fetchone()
        self.assertEqual(assessment["score_total"], 5)
        self.assertEqual(assessment["severity_grade"], "A")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM event_versions").fetchone()[0], 2
        )
        chain = self.connection.execute("SELECT * FROM event_chains").fetchone()
        member = self.connection.execute("SELECT * FROM event_chain_members").fetchone()
        self.assertEqual(chain["primary_event_id"], "evt")
        self.assertEqual(member["chain_role"], "primary_event")
        self.assertEqual(member["counts_as_primary_event"], 1)

    def test_manual_grade_may_conflict_with_rule_priority(self) -> None:
        row = self.row()
        row["manual_grade"] = "S"
        result = adjudicator.apply_rows(self.connection, [row])
        self.assertEqual(result["applied"], 1)
        assessment = self.connection.execute("SELECT * FROM event_assessments").fetchone()
        self.assertEqual(assessment["score_total"], 5)
        self.assertEqual(assessment["severity_grade"], "S")

    def test_preexisting_evidence_row_does_not_block_manual_adjudication(self) -> None:
        upsert_source(
            self.connection,
            source_id="official",
            name="Official",
            source_type="project_primary",
            authority_tier="P1",
        )
        observation_id, _ = record_source_observation(
            self.connection,
            source_id="official",
            external_id="post-1",
            source_published_at="2026-07-15",
            local_received_at=utc_now(),
            title="Official incident notice",
            summary="Machine extracted passage",
            canonical_url="https://example.test/post-1",
            content_sha256="machine",
            raw_json="{}",
            revision_kind="new",
        )
        evidence_id = stable_id("EVID", "evt", observation_id)
        now = utc_now()
        self.connection.execute(
            """INSERT INTO event_evidence(
               evidence_id,event_id,observation_id,evidence_url,filing_date,form,items,
               evidence_passage,matched_keywords,passage_score,evidence_status,
               auto_verification_allowed,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                evidence_id,
                "evt",
                observation_id,
                "https://example.test/post-1",
                "2026-07-15",
                "official",
                "",
                "Machine extracted passage",
                "incident",
                4,
                "confirmed_primary",
                0,
                now,
                now,
            ),
        )
        self.connection.commit()
        result = adjudicator.apply_rows(self.connection, [self.row()])
        self.assertEqual(result["applied"], 1)
        event = self.connection.execute("SELECT * FROM canonical_events").fetchone()
        evidence = self.connection.execute(
            "SELECT * FROM event_evidence WHERE evidence_id=?", (evidence_id,)
        ).fetchone()
        self.assertEqual(event["status"], "verified")
        self.assertEqual(evidence["evidence_status"], "confirmed_primary")
        self.assertEqual(evidence["evidence_passage"], self.row()["evidence_passage"])


if __name__ == "__main__":
    unittest.main()
