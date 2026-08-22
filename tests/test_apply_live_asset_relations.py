from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger, utc_now
import apply_live_asset_relations as relations


class LiveAssetRelationTests(unittest.TestCase):
    def add_event(self, connection, event_id: str, status: str) -> None:
        now = utc_now()
        connection.execute(
            """INSERT INTO canonical_events VALUES (
               ?,1,?,?, 'security','incident','2026-07-15',?,?,NULL,'WRONG',NULL,
               NULL,'B','opennews',1)""",
            (event_id, status, status, now, now),
        )
        connection.commit()

    def definition(self, event_id: str, allowed: bool) -> list[dict[str, object]]:
        return [
            {
                "event_id": event_id,
                "entities": [
                    {"type": "protocol", "name": "Example", "role": "SUBJECT", "confidence": 1.0}
                ],
                "assets": [
                    {
                        "asset_type": "crypto",
                        "symbol": "ETH",
                        "provider_symbol": "ETH/USD",
                        "venue": "TwelveData",
                        "currency": "USD",
                        "relation_type": "ECOSYSTEM_PROXY",
                        "direction": "ABSTAIN",
                        "impact_score": 20,
                        "confidence": 0.3,
                        "reason_codes": ["PROXY"],
                        "market_observation_allowed": allowed,
                    }
                ],
            }
        ]

    def test_verified_event_relations_are_explicit_and_no_trading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            self.add_event(connection, "FR-LIVE-test", "verified")
            result = relations.apply_relations(connection, self.definition("FR-LIVE-test", True))
            event = connection.execute("SELECT ticker_at_event FROM canonical_events").fetchone()
            impact = connection.execute("SELECT * FROM event_asset_impacts").fetchone()
            connection.close()
        self.assertEqual(event["ticker_at_event"], "WRONG")
        self.assertEqual(impact["direction"], "ABSTAIN")
        self.assertEqual(impact["no_trading"], 1)
        self.assertEqual(result["market_enabled"], 1)

    def test_candidate_cannot_enable_market_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            self.add_event(connection, "FR-LIVE-test", "candidate")
            with self.assertRaisesRegex(ValueError, "Candidate event"):
                relations.apply_relations(connection, self.definition("FR-LIVE-test", True))
            connection.close()

    def test_deleted_event_definition_is_reported_without_failing_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = open_ledger(Path(directory) / "db.sqlite3")
            result = relations.apply_relations(
                connection,
                self.definition("FR-LIVE-deleted", False),
            )
            entities = connection.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            impacts = connection.execute(
                "SELECT COUNT(*) FROM event_asset_impacts"
            ).fetchone()[0]
            connection.close()
        self.assertEqual(result["events"], 0)
        self.assertEqual(result["stale_event_definitions"], 1)
        self.assertEqual(result["stale_event_ids"], ["FR-LIVE-deleted"])
        self.assertEqual(entities, 0)
        self.assertEqual(impacts, 0)


if __name__ == "__main__":
    unittest.main()
