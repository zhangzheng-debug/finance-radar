from __future__ import annotations

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
        revisions = self.connection.execute(
            "SELECT revision_kind,summary FROM source_revisions ORDER BY revision_no"
        ).fetchall()
        self.assertEqual([row["revision_kind"] for row in revisions], ["new", "edit"])
        self.assertEqual(raw["summary"], "Company files for bankruptcy")


if __name__ == "__main__":
    unittest.main()
