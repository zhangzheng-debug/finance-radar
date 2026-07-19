from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger, utc_now
import build_live_review_triage as triage


class LiveReviewTriageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = open_ledger(Path(self.temp_dir.name) / "db.sqlite3")
        self.now = utc_now()
        self.connection.execute(
            """INSERT INTO sources VALUES (
               'sec_current_filings','SEC','official_primary_feed','P0_official',1,1,?,?)""",
            (self.now, self.now),
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def add_event(self, event_id: str, event_type: str, title: str, summary: str | None = None) -> None:
        observation_id = f"obs_{event_id}"
        self.connection.execute(
            """INSERT INTO raw_observations VALUES (
               ?,'sec_current_filings',?,'2026-07-15',?,?,?,
               'https://www.sec.gov/example','hash','{}','captured')""",
            (observation_id, observation_id, self.now, title, summary or title),
        )
        self.connection.execute(
            """INSERT INTO canonical_events VALUES (
               ?,1,'candidate','candidate','corporate_distress',?,'2026-07-15',
               ?,?,NULL,NULL,'Example Corp',NULL,'A_P0','sec_current_filings',1)""",
            (event_id, event_type, self.now, self.now),
        )
        self.connection.execute(
            "INSERT INTO event_observations VALUES (?,?,?,?)",
            (event_id, observation_id, "official_primary_candidate", self.now),
        )
        self.connection.execute(
            """INSERT INTO pipeline_jobs VALUES (
               ?,?,'live_primary_evidence_review','PENDING_PRIMARY_EVIDENCE',50,0,?,NULL,'{}',?,?)""",
            (f"job_{event_id}", event_id, self.now, self.now, self.now),
        )

    def test_hard_negative_is_reviewed_first_without_auto_s_or_promotion(self) -> None:
        self.add_event("evt_delist", "delisting", "Exchange delisting notice")
        self.add_event("evt_generic", "sec_material_filing", "Generic 8-K")
        rows = triage.build(self.connection)
        events = {
            row["event_id"]: row
            for row in self.connection.execute("SELECT * FROM canonical_events")
        }
        self.assertEqual(rows[0]["event_id"], "evt_delist")
        self.assertGreater(rows[0]["review_score"], rows[1]["review_score"])
        self.assertNotEqual(rows[0]["severity_ceiling"], "S")
        self.assertTrue(all(row["status"] == "candidate" for row in events.values()))
        self.assertTrue(all(row["no_trading"] == 1 for row in events.values()))

    def test_build_is_idempotent_and_updates_only_review_priority(self) -> None:
        self.add_event("evt_default", "debt_default", "Default notice")
        first = triage.build(self.connection)
        second = triage.build(self.connection)
        count = self.connection.execute(
            "SELECT COUNT(*) FROM event_review_triage"
        ).fetchone()[0]
        priority = self.connection.execute(
            "SELECT priority FROM pipeline_jobs WHERE event_id='evt_default'"
        ).fetchone()[0]
        status = self.connection.execute(
            "SELECT status FROM canonical_events WHERE event_id='evt_default'"
        ).fetchone()[0]
        self.assertEqual(first, second)
        self.assertEqual(count, 1)
        self.assertEqual(priority, first[0]["review_score"])
        self.assertEqual(status, "candidate")

    def test_context_overrides_prevent_false_hard_negative_priority(self) -> None:
        self.add_event(
            "evt_relief",
            "delisting",
            "8-K listing update",
            "Nasdaq granted an exception and trading resumed.",
        )
        self.add_event(
            "evt_termination",
            "enforcement_action",
            "Federal Reserve announces termination of enforcement action with Example Bank",
        )
        rows = {row["event_id"]: row for row in triage.build(self.connection)}
        self.assertEqual(rows["evt_relief"]["direction_status"], "compliance_relief_with_remaining_risk")
        self.assertNotEqual(rows["evt_relief"]["review_bucket"], "hard_negative_first")
        self.assertEqual(rows["evt_termination"]["direction_status"], "resolution_likely_not_new_negative")
        self.assertLess(rows["evt_termination"]["review_score"], 60)

    def test_compensation_plan_is_not_routed_as_management_departure(self) -> None:
        self.add_event(
            "evt_plan",
            "management_change",
            "8-K compensation update",
            "The Compensation Committee amended the inducement stock plan.",
        )
        row = triage.build(self.connection)[0]
        self.assertEqual(row["direction_status"], "compensation_plan_not_management_departure")
        self.assertEqual(row["review_bucket"], "low_value_official_noise")

    def test_spac_boilerplate_events_are_not_routed_as_mergers(self) -> None:
        self.add_event(
            "evt_note",
            "spac_sponsor_working_capital_note",
            "8-K sponsor working-capital note",
        )
        self.add_event(
            "evt_ipo",
            "spac_ipo_closing",
            "8-K SPAC IPO closing and trust funding",
        )
        rows = {row["event_id"]: row for row in triage.build(self.connection)}
        self.assertEqual(rows["evt_note"]["review_bucket"], "low_value_official_noise")
        self.assertEqual(rows["evt_ipo"]["direction_status"], "capital_formation_not_merger")
        self.assertLess(rows["evt_note"]["review_score"], 40)
        self.assertLess(rows["evt_ipo"]["review_score"], 40)

    def test_going_concern_financing_dependency_routes_to_material_review(self) -> None:
        self.add_event(
            "evt_going_concern",
            "going_concern_financing_dependency",
            "10-Q liquidity and going-concern disclosure",
            "Cash is below quarterly operating burn and additional financing is required.",
        )
        row = triage.build(self.connection)[0]
        self.assertEqual(row["review_bucket"], "material_negative")
        self.assertEqual(row["direction_status"], "liquidity_distress_likely")
        self.assertGreaterEqual(row["review_score"], 80)

    def test_machine_extracted_official_text_is_ready_but_never_auto_promotes(self) -> None:
        self.add_event(
            "evt_official_page",
            "enforcement_action",
            "Official enforcement release",
        )
        observation_id = "obs_evt_official_page"
        self.connection.execute(
            """INSERT INTO event_evidence(
               evidence_id,event_id,observation_id,evidence_url,filing_date,form,items,
               evidence_passage,matched_keywords,passage_score,evidence_status,
               auto_verification_allowed,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "evidence_evt_official_page",
                "evt_official_page",
                observation_id,
                "https://www.sec.gov/example",
                "2026-07-15",
                "official_page",
                "",
                "The agency filed an enforcement action and described the alleged conduct.",
                "enforcement action",
                10,
                "machine_extracted_unreviewed",
                0,
                self.now,
                self.now,
            ),
        )
        self.connection.commit()

        row = triage.build(self.connection)[0]
        event = self.connection.execute(
            "SELECT status,manual_grade,no_trading FROM canonical_events WHERE event_id=?",
            ("evt_official_page",),
        ).fetchone()
        assessment_count = self.connection.execute(
            "SELECT COUNT(*) FROM event_assessments WHERE event_id=?",
            ("evt_official_page",),
        ).fetchone()[0]

        self.assertEqual(row["evidence_readiness"], "primary_text_ready")
        self.assertEqual(row["evidence_status"], "machine_extracted_unreviewed")
        self.assertIn("alleged conduct", row["evidence_excerpt"])
        self.assertEqual(event["status"], "candidate")
        self.assertIsNone(event["manual_grade"])
        self.assertEqual(event["no_trading"], 1)
        self.assertEqual(assessment_count, 0)


if __name__ == "__main__":
    unittest.main()
