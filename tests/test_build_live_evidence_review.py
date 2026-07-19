from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger, stable_json, utc_now
import build_live_evidence_review as review


class LiveEvidenceReviewTests(unittest.TestCase):
    def test_only_pending_live_evidence_jobs_are_routed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            now = utc_now()
            connection.execute(
                """INSERT INTO canonical_events VALUES (
                   'evt',1,'candidate','candidate','macro_policy','monetary_policy','2026-07-15',
                   ?,?,NULL,NULL,NULL,NULL,'B','opennews',1)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO pipeline_jobs VALUES (
                   'job','evt','live_primary_evidence_review','PENDING_PRIMARY_EVIDENCE',90,0,?,NULL,'{}',?,?)""",
                (now, now, now),
            )
            config = {
                "routes": {
                    "monetary_policy": {
                        "goal": "confirm",
                        "official_sources": [["ECB", "https://ecb.example"]],
                        "query_terms": ["rates"],
                    },
                    "default": {
                        "goal": "default",
                        "official_sources": [["SEC", "https://sec.example"]],
                        "query_terms": [],
                    },
                }
            }
            rows = review.build_rows(connection, config)
            connection.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source_name"], "ECB")
        self.assertEqual(rows[0]["review_decision"], "pending_manual_review")

    def test_p0_discovery_url_is_routed_as_unreviewed_primary_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            now = utc_now()
            connection.execute(
                """INSERT INTO sources VALUES (
                   'sec_current_filings','SEC','official_primary_feed','P0_official',1,1,?,?)""",
                (now, now),
            )
            connection.execute(
                """INSERT INTO raw_observations VALUES (
                   'obs','sec_current_filings','acc','2026-07-15',?,'8-K Example','Item 2.02',
                   'https://www.sec.gov/Archives/example.htm','hash','{}','captured')""",
                (now,),
            )
            connection.execute(
                """INSERT INTO canonical_events VALUES (
                   'evt',1,'candidate','candidate','earnings','earnings_or_guidance','2026-07-15',
                   ?,?,NULL,NULL,'Example Corp',NULL,'A_P0','sec_current_filings',1)""",
                (now, now),
            )
            connection.execute(
                "INSERT INTO event_observations VALUES ('evt','obs','official_primary_candidate',?)",
                (now,),
            )
            connection.execute(
                """INSERT INTO pipeline_jobs VALUES (
                   'job','evt','live_primary_evidence_review','PENDING_PRIMARY_EVIDENCE',91,0,?,NULL,'{}',?,?)""",
                (now, now, now),
            )
            config = {
                "routes": {
                    "default": {
                        "goal": "confirm",
                        "official_sources": [["SEC", "https://sec.example"]],
                        "query_terms": [],
                    }
                }
            }
            rows = review.build_rows(connection, config)
            connection.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["support_level"], "official_discovery_unreviewed")
        self.assertEqual(rows[0]["known_evidence_url"], "https://www.sec.gov/Archives/example.htm")


if __name__ == "__main__":
    unittest.main()
