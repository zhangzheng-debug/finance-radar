from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger, upsert_source
import opennews_free_collector as collector


class OpenNewsFreeCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = open_ledger(Path(self.temp_dir.name) / "ledger.sqlite3")
        upsert_source(
            self.connection,
            source_id="opennews_free",
            name="OpenNews",
            source_type="aggregated_discovery",
            authority_tier="P2_experimental",
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def payload(self, title: str = "Company files for bankruptcy") -> dict[str, object]:
        return {
            "success": True,
            "news": {
                "success": True,
                "updated_at": "2026-07-16T00:00:00Z",
                "items": [
                    {
                        "id": 123,
                        "title": title,
                        "source": "Reuters",
                        "link": "https://example.test/a",
                        "score": 90,
                        "published_at": "2026-07-15T23:00:00Z",
                    }
                ],
            },
            "tweets": {"success": False, "items": []},
        }

    def test_capture_is_immutable_idempotent_and_jobbed(self) -> None:
        payload = self.payload()

        def requester(url: str, timeout: float) -> dict[str, object]:
            return payload

        first = collector.collect_category(
            self.connection, category="macro", requester=requester
        )
        second = collector.collect_category(
            self.connection, category="macro", requester=requester
        )
        self.assertEqual(first, {"items": 1, "new_revisions": 1, "jobs": 1})
        self.assertEqual(second, {"items": 1, "new_revisions": 0, "jobs": 0})
        raw = self.connection.execute("SELECT summary FROM raw_observations").fetchone()
        self.assertEqual(raw["summary"], "Company files for bankruptcy")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0], 1
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM observation_jobs").fetchone()[0], 1
        )

        payload["news"]["updated_at"] = "2026-07-16T00:05:00Z"
        payload["news"]["items"][0]["score"] = 99
        metadata_only = collector.collect_category(
            self.connection, category="macro", requester=requester
        )
        self.assertEqual(metadata_only["new_revisions"], 0)

        payload["news"]["items"][0]["title"] = "Company files Chapter 11 petition"
        third = collector.collect_category(
            self.connection, category="macro", requester=requester
        )
        self.assertEqual(third["new_revisions"], 1)
        self.assertEqual(third["jobs"], 1)
        revisions = self.connection.execute(
            "SELECT revision_kind,summary FROM source_revisions ORDER BY revision_no"
        ).fetchall()
        self.assertEqual([row["revision_kind"] for row in revisions], ["new", "edit"])
        self.assertEqual(raw["summary"], "Company files for bankruptcy")
        job = self.connection.execute(
            "SELECT status,attempts,payload_json FROM observation_jobs"
        ).fetchone()
        self.assertEqual(job["status"], "PENDING")
        self.assertEqual(job["attempts"], 0)
        self.assertEqual(
            json.loads(job["payload_json"])["source_content_sha256"],
            collector.semantic_content_hash(
                "macro", "news", payload["news"]["items"][0]
            ),
        )

    def test_latest_opennews_revision_updates_url_and_published_time(self) -> None:
        payload = self.payload()

        def requester(url: str, timeout: float) -> dict[str, object]:
            return payload

        collector.collect_category(self.connection, category="macro", requester=requester)
        payload["news"]["items"][0]["title"] = "Company files Chapter 11 petition"
        payload["news"]["items"][0]["link"] = "https://example.test/revised"
        payload["news"]["items"][0]["published_at"] = "2026-07-15T23:05:00Z"
        collector.collect_category(self.connection, category="macro", requester=requester)

        current = self.connection.execute(
            "SELECT canonical_url,source_published_at,latest_revision_no "
            "FROM latest_source_content"
        ).fetchone()
        self.assertEqual(current["canonical_url"], "https://example.test/revised")
        self.assertEqual(current["source_published_at"], "2026-07-15T23:05:00Z")
        self.assertEqual(current["latest_revision_no"], 2)

    def test_news_content_change_creates_revision_and_reopens_extraction(self) -> None:
        payload = self.payload()
        payload["news"]["items"][0]["content"] = "Markets await central-bank minutes."

        def requester(url: str, timeout: float) -> dict[str, object]:
            return payload

        first = collector.collect_category(
            self.connection, category="macro", requester=requester
        )
        self.connection.execute(
            "UPDATE observation_jobs SET status='COMPLETED',attempts=1"
        )
        self.connection.commit()
        payload["news"]["items"][0]["content"] = (
            "Federal Reserve released the meeting minutes."
        )

        second = collector.collect_category(
            self.connection, category="macro", requester=requester
        )

        self.assertEqual(first["new_revisions"], 1)
        self.assertEqual(second["new_revisions"], 1)
        self.assertEqual(second["jobs"], 1)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0],
            2,
        )
        job = self.connection.execute(
            "SELECT status,attempts,payload_json FROM observation_jobs"
        ).fetchone()
        self.assertEqual(job["status"], "PENDING")
        self.assertEqual(job["attempts"], 0)
        self.assertEqual(
            json.loads(job["payload_json"])["source_content_sha256"],
            collector.semantic_content_hash(
                "macro", "news", payload["news"]["items"][0]
            ),
        )

    def test_existing_legacy_view_is_upgraded_once_not_on_every_open(self) -> None:
        ledger_path = Path(self.temp_dir.name) / "legacy-view.sqlite3"
        initial = open_ledger(ledger_path)
        initial.execute("DROP VIEW latest_source_content")
        initial.execute(
            """CREATE VIEW latest_source_content AS
               SELECT observation_id,source_id,external_id,source_published_at,
                      local_received_at,title,summary,canonical_url,content_sha256,
                      raw_json,observation_status,0 AS latest_revision_no,
                      'new' AS latest_revision_kind,local_received_at AS latest_revision_at
               FROM raw_observations"""
        )
        initial.commit()
        initial.close()

        migrated = open_ledger(ledger_path)
        view_sql = migrated.execute(
            "SELECT sql FROM sqlite_master WHERE type='view' AND name='latest_source_content'"
        ).fetchone()[0]
        migrated_schema_version = migrated.execute("PRAGMA schema_version").fetchone()[0]
        migrated.close()
        reopened = open_ledger(ledger_path)
        reopened_schema_version = reopened.execute("PRAGMA schema_version").fetchone()[0]
        reopened.close()

        self.assertIn("$.item.published_at", view_sql)
        self.assertEqual(reopened_schema_version, migrated_schema_version)


if __name__ == "__main__":
    unittest.main()
