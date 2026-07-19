from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger, stable_json, utc_now
import telegram_alert_outbox as alerts


class TelegramAlertOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = open_ledger(Path(self.temp_dir.name) / "ledger.sqlite3")
        now = utc_now()
        self.connection.execute(
            """INSERT INTO sources VALUES ('sec','SEC','official','P0',1,1,?,?)""",
            (now, now),
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def add_event(self, event_id: str, event_date: str, status: str = "verified") -> None:
        now = utc_now()
        self.connection.execute(
            """INSERT INTO raw_observations VALUES (
               ?, 'sec', ?, ?, ?, 'filing', 'evidence', 'https://sec.example/filing',
               'hash', '{}', 'captured')""",
            (f"obs-{event_id}", event_id, event_date, now),
        )
        self.connection.execute(
            """INSERT INTO canonical_events VALUES (
               ?,1,?,?, 'distress','bankruptcy',?,?,?, 'stable','TST','Test Co','S','S','test',1)""",
            (event_id, status, status, event_date, now, now),
        )
        self.connection.execute(
            """INSERT INTO event_evidence VALUES (
               ?,?,?,'https://sec.example/filing',?,'8-K','1.03','passage','chapter 11',10,
               'confirmed',0,?,?)""",
            (f"ev-{event_id}", event_id, f"obs-{event_id}", event_date, now, now),
        )
        self.connection.commit()

    def test_historical_event_does_not_enqueue(self) -> None:
        today = dt.date(2026, 7, 16)
        self.add_event("old", "2025-01-01")
        self.assertEqual(
            alerts.enqueue_verified_alerts(self.connection, freshness_days=3, today=today), 0
        )
        self.assertEqual(len(alerts.pending_rows(self.connection)), 0)

    def test_fresh_verified_event_is_idempotent_and_delivered(self) -> None:
        today = dt.date(2026, 7, 16)
        self.add_event("fresh", "2026-07-15")
        self.assertEqual(
            alerts.enqueue_verified_alerts(self.connection, freshness_days=3, today=today), 1
        )
        self.assertEqual(
            alerts.enqueue_verified_alerts(self.connection, freshness_days=3, today=today), 0
        )
        calls: list[tuple[str, dict[str, str]]] = []

        def requester(url: str, data: dict[str, str], timeout: float) -> dict[str, object]:
            calls.append((url.rsplit("/", 1)[-1], data))
            return {"ok": True, "result": {"message_id": 99}}

        client = alerts.TelegramBotClient("secret", requester=requester)
        self.assertEqual(alerts.deliver_pending(self.connection, client, "123"), (1, 0))
        self.assertEqual(calls[0][0], "sendMessage")
        self.assertIn("不构成投资建议", calls[0][1]["text"])
        self.assertIn("/Event_Intelligence?event_id=fresh", calls[0][1]["text"])
        row = self.connection.execute("SELECT status FROM alert_outbox").fetchone()
        self.assertEqual(row["status"], "SENT")
        attempts = self.connection.execute(
            "SELECT COUNT(*) FROM alert_delivery_attempts"
        ).fetchone()[0]
        self.assertEqual(attempts, 1)

    def test_delivery_lease_blocks_concurrent_worker(self) -> None:
        today = dt.date(2026, 7, 16)
        self.add_event("leased", "2026-07-15")
        alerts.enqueue_verified_alerts(self.connection, freshness_days=3, today=today)
        outbox_id = self.connection.execute("SELECT outbox_id FROM alert_outbox").fetchone()[0]
        first = alerts.acquire_delivery_lease(self.connection, outbox_id)
        self.assertIsNotNone(first)
        self.assertIsNone(alerts.acquire_delivery_lease(self.connection, outbox_id))
        alerts.release_delivery_lease(self.connection, outbox_id, first)
        second = alerts.acquire_delivery_lease(self.connection, outbox_id)
        self.assertIsNotNone(second)

    def test_pending_payload_link_refresh_does_not_touch_sent_rows(self) -> None:
        today = dt.date(2026, 7, 16)
        self.add_event("pending", "2026-07-15")
        alerts.enqueue_verified_alerts(self.connection, freshness_days=3, today=today)
        with patch.dict("os.environ", {"FINANCE_RADAR_WEB_URL": "https://radar.example/radar"}):
            self.assertEqual(alerts.refresh_pending_payloads(self.connection), 1)
            self.assertEqual(alerts.refresh_pending_payloads(self.connection), 0)
        payload = json.loads(self.connection.execute("SELECT payload_json FROM alert_outbox").fetchone()[0])
        self.assertIn("https://radar.example/radar/Event_Intelligence?event_id=pending", payload["text"])
        self.connection.execute("UPDATE alert_outbox SET status='SENT'")
        self.connection.commit()
        with patch.dict("os.environ", {"FINANCE_RADAR_WEB_URL": "https://other.example/radar"}):
            self.assertEqual(alerts.refresh_pending_payloads(self.connection), 0)


if __name__ == "__main__":
    unittest.main()
