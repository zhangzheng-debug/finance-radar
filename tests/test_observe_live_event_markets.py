from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger, stable_json, utc_now
import observe_live_event_markets as observer


class LiveMarketObserverTests(unittest.TestCase):
    ANCHOR = dt.datetime(2026, 7, 16, 12, 0, tzinfo=dt.timezone.utc)

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
            """INSERT INTO sources VALUES (
               'src','Example Source','official_page','P0_official',1,1,?,?)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO raw_observations VALUES (
               'obs','src','external',?,?, 'Evidence title','Evidence summary',
               'https://example.test/evidence','hash','{}','stored')""",
            (self.ANCHOR.isoformat(), (self.ANCHOR + dt.timedelta(minutes=1)).isoformat()),
        )
        self.connection.execute(
            """INSERT INTO event_versions VALUES (
               'evt',1,?,'verified','verified','security','incident',NULL,?,?)""",
            (
                now,
                stable_json({"public_fact_summary": "Example confirmed a security incident."}),
                "test",
            ),
        )
        self.connection.execute(
            """INSERT INTO event_evidence VALUES (
               'evd','evt','obs','https://example.test/evidence','2026-07-16',NULL,NULL,
               'Example confirmed a security incident in its official notice.',NULL,100,
               'primary_exact',0,?,?)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO event_evidence_relations VALUES (
               'evt','evd',1,'SCOPED_MATCH',1,1,1,'CONFIRMED','fingerprint',
               'event-relation-v1','test',?)""",
            (now,),
        )
        self.connection.execute(
            """INSERT INTO event_fact_workflow VALUES (
               'evt',1,'EVIDENCE_READY','[]','fingerprint','event-admission-v1',?)""",
            (now,),
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
            self.connection,
            freshness_days=3,
            today=dt.date(2026, 7, 16),
            now=self.ANCHOR,
        )
        self.assertEqual(inserted, 6)

        job = self.connection.execute(
            "SELECT * FROM market_jobs WHERE observation_window='initial'"
        ).fetchone()
        self.assertEqual(job["provider"], "binance_public")

        def binance_requester(symbols, timeout):
            self.assertEqual(symbols, ["ETHUSDT"])
            return {"ETHUSDT": {"symbol": "ETHUSDT", "price": "2010.25"}}

        before_known = observer.run_pending(
            self.connection, now=self.ANCHOR, binance_requester=binance_requester
        )
        self.assertEqual(before_known["requested"], 0)
        result = observer.run_pending(
            self.connection,
            now=self.ANCHOR + dt.timedelta(minutes=1),
            binance_requester=binance_requester,
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
        self.assertEqual(snapshot["freshness_status"], "window_capture_lag_60s")
        self.assertEqual(snapshot["data_scope"], "reaction_anchor_relative_initial")
        self.assertIn('"declared_anchor_kind":"source_published"', snapshot["raw_json"])
        self.assertNotIn("credential", snapshot["raw_json"].lower())
        self.assertIn('"order_endpoint_called":false', snapshot["raw_json"])
        self.assertEqual(
            observer.schedule_jobs(
                self.connection,
                freshness_days=3,
                today=dt.date(2026, 7, 16),
                now=self.ANCHOR,
            ),
            0,
        )

    def test_default_provider_path_uses_timestamped_minute_bar(self) -> None:
        observer.schedule_jobs(
            self.connection,
            freshness_days=3,
            today=dt.date(2026, 7, 16),
            now=self.ANCHOR,
        )

        def minute_bar(symbol, scheduled_at, timeout):
            self.assertEqual(symbol, "ETHUSDT")
            self.assertEqual(scheduled_at, self.ANCHOR.isoformat())
            return {
                "symbol": symbol,
                "price": "2010.25",
                "provider_as_of": self.ANCHOR.isoformat(),
                "interval": "1min",
                "price_kind": "bar_close",
                "open": "2000",
                "high": "2020",
                "low": "1995",
                "close": "2010.25",
                "volume": "150",
            }

        result = observer.run_pending(
            self.connection,
            now=self.ANCHOR + dt.timedelta(minutes=1),
            binance_bar_requester=minute_bar,
        )
        self.assertEqual(result["completed"], 1)
        snapshot = self.connection.execute("SELECT * FROM market_snapshots").fetchone()
        self.assertEqual(snapshot["provider_as_of"], self.ANCHOR.isoformat())
        self.assertIn('"interval":"1min"', snapshot["raw_json"])
        self.assertIn('"price_kind":"bar_close"', snapshot["raw_json"])

    def test_binance_minute_bar_rejects_wrong_timestamp(self) -> None:
        start_ms = int(self.ANCHOR.timestamp() * 1000)
        payload = [[start_ms, "100", "102", "99", "101", "10", start_ms + 59_999]]
        normalized = observer.normalize_binance_minute_bar(
            payload, symbol="ETHUSDT", scheduled_at=self.ANCHOR.isoformat()
        )
        self.assertEqual(normalized["price"], "101")
        self.assertEqual(normalized["volume"], "10")

        with self.assertRaisesRegex(RuntimeError, "outside requested window"):
            observer.normalize_binance_minute_bar(
                payload,
                symbol="ETHUSDT",
                scheduled_at=(self.ANCHOR + dt.timedelta(minutes=1)).isoformat(),
            )

    def test_evidence_ready_candidate_is_observed_before_formal_adjudication(self) -> None:
        self.connection.execute(
            "UPDATE canonical_events SET status='candidate',label_status='candidate' WHERE event_id='evt'"
        )
        self.connection.execute(
            "UPDATE event_versions SET status='candidate',label_status='candidate' WHERE event_id='evt'"
        )
        self.connection.commit()
        inserted = observer.schedule_jobs(
            self.connection,
            freshness_days=3,
            today=dt.date(2026, 7, 16),
            now=self.ANCHOR,
        )
        self.assertEqual(inserted, 6)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM event_market_metrics").fetchone()[0],
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
                self.connection,
                freshness_days=3,
                today=dt.date(2026, 7, 16),
                now=self.ANCHOR,
            ),
            12,
        )

        first = observer.run_pending(
            self.connection,
            now=self.ANCHOR + dt.timedelta(minutes=1),
            api_key="",
            binance_requester=lambda symbols, timeout: {
                "ETHUSDT": {"symbol": "ETHUSDT", "price": "2000"}
            },
        )
        self.assertEqual(first["completed"], 1)
        self.assertEqual(first["skipped_missing_key"], 1)
        statuses = {
            row["provider"]: row["status"]
            for row in self.connection.execute(
                "SELECT provider,status FROM market_jobs WHERE observation_window='initial'"
            )
        }
        self.assertEqual(statuses["binance_public"], "COMPLETED")
        self.assertEqual(statuses["twelve_data"], "PENDING")

        second = observer.run_pending(
            self.connection,
            now=self.ANCHOR + dt.timedelta(minutes=1),
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
        baseline_at = self.ANCHOR
        self.assertEqual(
            observer.schedule_jobs(
                self.connection,
                freshness_days=3,
                today=dt.date(2026, 7, 16),
                now=baseline_at,
            ),
            6,
        )
        first = observer.run_pending(
            self.connection,
            now=baseline_at + dt.timedelta(minutes=1),
            binance_requester=lambda symbols, timeout: {
                "ETHUSDT": {"symbol": "ETHUSDT", "price": "100"}
            },
        )
        self.assertEqual(first["completed"], 1)
        self.assertEqual(observer.schedule_followup_jobs(self.connection), 0)

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
        self.assertEqual(horizon_snapshot["data_scope"], "reaction_anchor_relative_t_plus_5m")
        self.assertEqual(horizon_snapshot["freshness_status"], "window_capture_lag_30s")
        metric = self.connection.execute(
            "SELECT * FROM event_market_metrics WHERE metric_name LIKE 'reaction_return_t_plus_5m_pct__%'"
        ).fetchone()
        self.assertEqual(metric["metric_value"], "5.000000")
        self.assertEqual(metric["metric_scope"], "post_event_audit_only")
        self.assertEqual(metric["allowed_as_model_feature"], 0)

    def test_missed_horizon_is_closed_without_quote_substitution(self) -> None:
        baseline_at = self.ANCHOR
        observer.schedule_jobs(
            self.connection,
            freshness_days=3,
            today=dt.date(2026, 7, 16),
            now=baseline_at,
        )
        observer.run_pending(
            self.connection,
            now=baseline_at + dt.timedelta(minutes=1),
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

    def test_legacy_observer_relative_snapshots_are_not_relabelled_as_reaction_metrics(self) -> None:
        captured = self.ANCHOR.isoformat()
        for job_id, window, price in (
            ("legacy-initial", "initial", "100"),
            ("legacy-five", "t_plus_5m", "105"),
        ):
            self.connection.execute(
                """INSERT INTO market_jobs VALUES (
                   ?,'evt','asset','binance_public',?,'COMPLETED',?,?,1,NULL,1)""",
                (job_id, window, captured, captured),
            )
            self.connection.execute(
                """INSERT INTO market_snapshots VALUES (
                   ?,?,'evt','asset','binance_public','ETHUSDT','legacy_observer_relative',
                   ?,'USDT',NULL,?,'legacy','{}',1,1)""",
                (f"snap-{job_id}", job_id, price, captured),
            )
        self.connection.commit()
        self.assertEqual(observer.upsert_horizon_metrics(self.connection), 0)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM event_market_metrics").fetchone()[0],
            0,
        )

    def test_date_only_source_time_is_recorded_but_never_scheduled_as_intraday(self) -> None:
        self.connection.execute(
            "UPDATE raw_observations SET source_published_at='2026-07-16' WHERE observation_id='obs'"
        )
        self.connection.commit()
        inserted = observer.schedule_jobs(
            self.connection,
            freshness_days=3,
            today=dt.date(2026, 7, 16),
            now=self.ANCHOR,
        )
        self.assertEqual(inserted, 0)
        anchor = self.connection.execute("SELECT * FROM market_event_anchors").fetchone()
        self.assertEqual(anchor["anchor_status"], "UNAVAILABLE")
        self.assertEqual(anchor["timestamp_precision"], "DATE_ONLY")
        self.assertEqual(anchor["reason_code"], "source_published_date_only")

    def test_equity_next_close_requires_explicit_exchange_calendar_metadata(self) -> None:
        now = utc_now()
        metadata = stable_json(
            {
                "session_timezone": "America/New_York",
                "regular_close_local": "16:00",
                "trading_weekdays": [0, 1, 2, 3, 4],
                "holidays": [],
            }
        )
        self.connection.execute(
            """INSERT INTO assets VALUES (
               'equity','equity','TEST','TEST','NYSE','USD',?,?,?)""",
            (metadata, now, now),
        )
        self.connection.execute(
            """INSERT INTO event_asset_impacts VALUES (
               'impact-equity','evt','equity','PRIMARY','ABSTAIN',25,0.3,'[]','test',1,1,?,?)""",
            (now, now),
        )
        self.connection.commit()
        inserted = observer.schedule_jobs(
            self.connection,
            freshness_days=3,
            today=dt.date(2026, 7, 16),
            now=self.ANCHOR,
        )
        self.assertEqual(inserted, 13)
        close_job = self.connection.execute(
            "SELECT * FROM market_jobs WHERE asset_id='equity' AND observation_window='next_close'"
        ).fetchone()
        self.assertEqual(close_job["scheduled_at"], "2026-07-16T20:00:00+00:00")
        link = self.connection.execute(
            "SELECT * FROM market_job_anchor_links WHERE market_job_id=?",
            (close_job["market_job_id"],),
        ).fetchone()
        self.assertEqual(link["offset_seconds"], 8 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
