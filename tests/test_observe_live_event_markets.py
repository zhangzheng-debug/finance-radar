from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger, utc_now
import observe_live_event_markets as observer


class LiveMarketObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = open_ledger(Path(self.temp_dir.name) / "db.sqlite3")
        now = utc_now()
        self.connection.execute(
            """INSERT INTO canonical_events VALUES (
               'evt',1,'verified','verified','security','incident','2026-07-15',?,?,NULL,NULL,
               'Example','A','A','test',1)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO assets VALUES (
               'asset','crypto','ETH','ETH/USD','TwelveData','USD','{}',?,?)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO event_asset_impacts VALUES (
               'impact','evt','asset','ECOSYSTEM_PROXY','ABSTAIN',20,0.3,'[]','test',1,1,?,?)""",
            (now, now),
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_crypto_schedules_binance_public_read_only_snapshot(self) -> None:
        inserted = observer.schedule_jobs(
            self.connection, freshness_days=3, today=dt.date(2026, 7, 16)
        )
        self.assertEqual(inserted, 1)

        job = self.connection.execute("SELECT * FROM market_jobs").fetchone()
        self.assertEqual(job["provider"], "binance_public")

        def binance_requester(symbols, timeout):
            self.assertEqual(symbols, ["ETHUSDT"])
            return {"ETHUSDT": {"symbol": "ETHUSDT", "price": "2010.25"}}

        result = observer.run_pending(
            self.connection, binance_requester=binance_requester
        )
        self.assertEqual(result["requested"], 1)
        self.assertEqual(result["completed"], 1)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(result["providers"]["binance_public"]["status"], "COMPLETED")
        snapshot = self.connection.execute("SELECT * FROM market_snapshots").fetchone()
        self.assertEqual(snapshot["price"], "2010.25")
        self.assertEqual(snapshot["provider"], "binance_public")
        self.assertEqual(snapshot["provider_symbol"], "ETHUSDT")
        self.assertEqual(snapshot["currency"], "USDT")
        self.assertEqual(snapshot["read_only"], 1)
        self.assertEqual(snapshot["no_trading"], 1)
        self.assertEqual(snapshot["freshness_status"], "provider_timestamp_unavailable")
        self.assertNotIn("credential", snapshot["raw_json"].lower())
        self.assertIn('"order_endpoint_called":false', snapshot["raw_json"])
        self.assertEqual(
            observer.schedule_jobs(
                self.connection, freshness_days=3, today=dt.date(2026, 7, 16)
            ),
            0,
        )

    def test_missing_twelve_key_does_not_block_public_binance_jobs(self) -> None:
        now = utc_now()
        self.connection.execute(
            """INSERT INTO assets VALUES (
               'asset-etf','etf','USO','USO','TwelveData','USD','{}',?,?)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO event_asset_impacts VALUES (
               'impact-etf','evt','asset-etf','MACRO_PROXY','ABSTAIN',25,0.3,'[]','test',1,1,?,?)""",
            (now, now),
        )
        self.connection.commit()
        self.assertEqual(
            observer.schedule_jobs(
                self.connection, freshness_days=3, today=dt.date(2026, 7, 16)
            ),
            2,
        )

        first = observer.run_pending(
            self.connection,
            api_key="",
            binance_requester=lambda symbols, timeout: {
                "ETHUSDT": {"symbol": "ETHUSDT", "price": "2000"}
            },
        )
        self.assertEqual(first["completed"], 1)
        self.assertEqual(first["skipped_missing_key"], 1)
        statuses = {
            row["provider"]: row["status"]
            for row in self.connection.execute("SELECT provider,status FROM market_jobs")
        }
        self.assertEqual(statuses["binance_public"], "COMPLETED")
        self.assertEqual(statuses["twelve_data"], "PENDING")

        second = observer.run_pending(
            self.connection,
            api_key="test-key",
            requester=lambda symbols, api_key, timeout: {"USO": {"price": "80.5"}},
        )
        self.assertEqual(second["completed"], 1)
        self.assertEqual(second["errors"], 0)
        providers = {
            row["provider"]
            for row in self.connection.execute("SELECT provider FROM market_snapshots")
        }
        self.assertEqual(providers, {"binance_public", "twelve_data"})

    def test_real_followup_capture_creates_observer_relative_return(self) -> None:
        baseline_at = dt.datetime(2026, 7, 18, 12, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(
            observer.schedule_jobs(
                self.connection,
                freshness_days=3,
                today=dt.date(2026, 7, 16),
                now=baseline_at,
            ),
            1,
        )
        first = observer.run_pending(
            self.connection,
            now=baseline_at,
            binance_requester=lambda symbols, timeout: {
                "ETHUSDT": {"symbol": "ETHUSDT", "price": "100"}
            },
        )
        self.assertEqual(first["completed"], 1)
        self.assertEqual(observer.schedule_followup_jobs(self.connection), 3)

        jobs = {
            row["observation_window"]: row
            for row in self.connection.execute("SELECT * FROM market_jobs")
        }
        self.assertEqual(jobs["t_plus_5m"]["status"], "PENDING")
        self.assertEqual(jobs["t_plus_30m"]["status"], "PENDING")
        second = observer.run_pending(
            self.connection,
            now=baseline_at + dt.timedelta(minutes=5, seconds=30),
            binance_requester=lambda symbols, timeout: {
                "ETHUSDT": {"symbol": "ETHUSDT", "price": "105"}
            },
        )
        self.assertEqual(second["completed"], 1)
        self.assertEqual(second["missed_windows"], 0)
        self.assertEqual(second["metrics_upserted"], 1)
        horizon_snapshot = self.connection.execute(
            """SELECT s.* FROM market_snapshots s JOIN market_jobs j
               ON j.market_job_id=s.market_job_id
               WHERE j.observation_window='t_plus_5m'"""
        ).fetchone()
        self.assertEqual(horizon_snapshot["data_scope"], "observer_relative_t_plus_5m")
        self.assertEqual(horizon_snapshot["freshness_status"], "window_capture_lag_30s")
        metric = self.connection.execute(
            "SELECT * FROM event_market_metrics WHERE metric_name LIKE 'observer_return_t_plus_5m_pct__%'"
        ).fetchone()
        self.assertEqual(metric["metric_value"], "5.000000")
        self.assertEqual(metric["metric_scope"], "post_event_audit_only")
        self.assertEqual(metric["allowed_as_model_feature"], 0)

    def test_missed_horizon_is_closed_without_quote_substitution(self) -> None:
        baseline_at = dt.datetime(2026, 7, 18, 12, 0, tzinfo=dt.timezone.utc)
        observer.schedule_jobs(
            self.connection,
            freshness_days=3,
            today=dt.date(2026, 7, 16),
            now=baseline_at,
        )
        observer.run_pending(
            self.connection,
            now=baseline_at,
            binance_requester=lambda symbols, timeout: {
                "ETHUSDT": {"symbol": "ETHUSDT", "price": "100"}
            },
        )
        observer.schedule_followup_jobs(self.connection)
        requests = []

        def requester(symbols, timeout):
            requests.append(symbols)
            return {"ETHUSDT": {"symbol": "ETHUSDT", "price": "999"}}

        result = observer.run_pending(
            self.connection,
            now=baseline_at + dt.timedelta(minutes=8),
            binance_requester=requester,
        )
        self.assertEqual(result["missed_windows"], 1)
        self.assertEqual(result["completed"], 0)
        self.assertEqual(requests, [])
        missed = self.connection.execute(
            "SELECT * FROM market_jobs WHERE observation_window='t_plus_5m'"
        ).fetchone()
        self.assertEqual(missed["status"], "MISSED_WINDOW")
        self.assertIn("no historical quote substituted", missed["last_error"])
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM market_snapshots WHERE market_job_id=?",
                (missed["market_job_id"],),
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
