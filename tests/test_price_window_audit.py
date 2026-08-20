"""Contract tests for the post-event price window audit.

The audit exists to make one claim provable: a window labelled ``t_plus_30m``
was measured from a stated anchor, captured inside its grace period, and — if it
was missed — never quietly filled in later.  These tests build a ledger that
violates each of those in turn and assert the audit says so.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger, utc_now  # noqa: E402
import audit_price_windows as audit  # noqa: E402


BASE = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


class PriceWindowAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.db_path = Path(self._dir.name) / "ledger.sqlite3"
        connection = open_ledger(self.db_path)
        self.addCleanup(connection.close)
        self.connection = connection
        self._seed_minimum_graph()

    def _seed_minimum_graph(self) -> None:
        now = utc_now()
        self.connection.execute(
            """INSERT INTO sources(source_id,name,source_type,authority_tier,read_only,enabled,
                                   created_at,updated_at)
               VALUES('sec_current_filings','SEC','official_feed','P0_official',1,1,?,?)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO assets(asset_id,asset_type,symbol,provider_symbol,venue,metadata_json,
                                  created_at,updated_at)
               VALUES('AST1','equity','TEST','TEST','TEST_VENUE','{}',?,?)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO canonical_events(
                   event_id,current_version,status,label_status,event_family,event_type,
                   event_date,first_seen_at,last_updated_at,discovery_source,no_trading,company_name)
               VALUES('EVT1',1,'verified','verified','delisting_or_suspension','delisted',
                      '2026-08-01',?,?,'sec_current_filings',1,'Test Corp')""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO event_versions VALUES (
                   'EVT1',1,?,'verified','verified','delisting_or_suspension',
                   'delisted',NULL,'{}','test fixture')""",
            (now,),
        )
        self.connection.commit()

    def _add_job(
        self,
        job_id: str,
        window: str,
        status: str,
        *,
        scheduled: datetime,
        completed: datetime | None = None,
        provider: str | None = None,
        anchored: bool = True,
    ) -> None:
        # market_jobs is unique on (event, asset, provider, window), which mirrors
        # reality: two providers may observe the same window for the same event.
        provider_name = provider or f"provider_{job_id.lower()}"
        self.connection.execute(
            """INSERT INTO market_jobs(
                   market_job_id,event_id,asset_id,provider,observation_window,status,
                   scheduled_at,completed_at,attempts,last_error,no_trading)
               VALUES(?,'EVT1','AST1',?,?,?,?,?,0,NULL,1)""",
            (
                job_id,
                provider_name,
                window,
                status,
                _iso(scheduled),
                _iso(completed) if completed else None,
            ),
        )
        if anchored:
            expected_offset = audit.WINDOW_OFFSET_SECONDS.get(window, 3600)
            anchor_at = scheduled - timedelta(seconds=expected_offset)
            anchor_id = f"ANCHOR-{job_id}"
            self.connection.execute(
                """INSERT INTO market_event_anchors(
                       anchor_id,event_id,event_version,asset_id,provider,
                       declared_anchor_kind,reaction_anchor_at,source_published_at,
                       local_received_at,known_at,timestamp_precision,anchor_status,
                       anchor_lag_seconds,unsupported_windows_json,reason_code,
                       contract_version,created_at,updated_at,no_trading)
                   VALUES(?,'EVT1',1,'AST1',?,'filing_effective',?,?,?,?,'EXACT_TIMESTAMP',
                          'EXACT',60,'[]',NULL,'market-anchor-v1',?,?,1)""",
                (
                    anchor_id,
                    provider_name,
                    _iso(anchor_at),
                    _iso(anchor_at),
                    _iso(anchor_at + timedelta(minutes=1)),
                    _iso(anchor_at + timedelta(minutes=1)),
                    _iso(BASE),
                    _iso(BASE),
                ),
            )
            self.connection.execute(
                """INSERT INTO market_job_anchor_links VALUES (
                       ?,?,?, 'market-windows-v2',?)""",
                (job_id, anchor_id, expected_offset, _iso(BASE)),
            )
        self.connection.commit()

    def _add_snapshot(self, job_id: str, captured: datetime, snapshot_id: str) -> None:
        self.connection.execute(
            """INSERT INTO market_snapshots(
                   snapshot_id,market_job_id,event_id,asset_id,provider,provider_symbol,
                   data_scope,price,currency,provider_as_of,captured_at,freshness_status,
                   raw_json,read_only,no_trading)
               VALUES(?,?,'EVT1','AST1','snapshot_provider','TEST','market_data_only','10.0','USD',
                      NULL,?,'fresh','{}',1,1)""",
            (snapshot_id, job_id, _iso(captured)),
        )
        self.connection.commit()

    # ── 1. anchor ────────────────────────────────────────────────
    def test_exact_version_bound_anchor_matches_the_declared_anchor(self) -> None:

        self._add_job("J1", "t_plus_5m", "COMPLETED", scheduled=BASE, completed=BASE)
        report = audit.build_report(self.db_path)
        anchor = report["anchor"]

        self.assertTrue(anchor["anchor_integrity_holds"])
        self.assertEqual(anchor["legacy_jobs_without_anchor"], 0)
        self.assertEqual(anchor["jobs_with_declaration_mismatch"], 0)
        family = anchor["families"][0]
        self.assertEqual(family["event_family"], "delisting_or_suspension")
        self.assertEqual(family["declared_anchor"], "filing_effective")
        self.assertTrue(family["anchor_matches_declaration"])
        self.assertEqual(report["status"], "PASS")

    def test_legacy_job_without_anchor_is_reported_not_reinterpreted(self) -> None:
        self._add_job(
            "J1", "t_plus_5m", "COMPLETED", scheduled=BASE, completed=BASE, anchored=False
        )
        report = audit.build_report(self.db_path)
        self.assertEqual(report["anchor"]["legacy_jobs_without_anchor"], 1)
        self.assertFalse(report["anchor"]["anchor_integrity_holds"])
        self.assertEqual(report["status"], "ATTENTION")

    def test_capture_before_known_at_is_a_contract_violation(self) -> None:
        self._add_job("J1", "initial", "COMPLETED", scheduled=BASE, completed=BASE)
        self._add_snapshot("J1", BASE + timedelta(seconds=30), "SNAP1")
        self.connection.execute(
            "UPDATE market_event_anchors SET known_at=? WHERE anchor_id='ANCHOR-J1'",
            (_iso(BASE + timedelta(minutes=1)),),
        )
        self.connection.commit()
        report = audit.build_report(self.db_path)
        self.assertEqual(report["anchor"]["captures_before_known_at"], 1)
        self.assertEqual(report["status"], "ATTENTION")

    # ── 2. fulfilment ────────────────────────────────────────────
    def test_fulfilment_keeps_open_jobs_in_the_denominator(self) -> None:
        """An unfinished window is still a window the product did not deliver."""

        self._add_job("J1", "t_plus_5m", "COMPLETED", scheduled=BASE, completed=BASE)
        self._add_job("J2", "t_plus_5m", "MISSED_WINDOW", scheduled=BASE, completed=BASE)
        self._add_job("J3", "t_plus_5m", "PENDING", scheduled=BASE)
        report = audit.build_report(self.db_path)
        window = report["fulfilment"]["windows"][0]

        self.assertEqual(window["scheduled"], 3)
        self.assertEqual(window["completed"], 1)
        self.assertEqual(window["missed"], 1)
        self.assertEqual(window["still_open"], 1)
        self.assertAlmostEqual(window["fulfilment_pct"], 33.33, places=1)
        self.assertEqual(report["fulfilment"]["fulfilment_pct"], 33.33)

    # ── 3. lateness ──────────────────────────────────────────────
    def test_capture_outside_grace_is_counted_not_shown_as_on_time(self) -> None:
        """t_plus_5m has a 120s grace; a capture at +360s must be flagged."""

        self._add_job("J1", "t_plus_5m", "COMPLETED", scheduled=BASE, completed=BASE)
        self._add_snapshot("J1", BASE + timedelta(seconds=360), "SNAP1")
        self._add_job("J2", "t_plus_5m", "COMPLETED", scheduled=BASE, completed=BASE)
        self._add_snapshot("J2", BASE + timedelta(seconds=30), "SNAP2")

        report = audit.build_report(self.db_path)
        window = report["lateness"]["windows"][0]

        self.assertEqual(window["captures"], 2)
        self.assertEqual(window["grace_seconds"], 120)
        self.assertEqual(window["captured_outside_grace"], 1)
        self.assertEqual(window["lag_max_seconds"], 360.0)
        self.assertEqual(report["lateness"]["captured_outside_grace_total"], 1)

    # ── 4. no backfill ───────────────────────────────────────────
    def test_clean_ledger_proves_the_no_backfill_guarantee(self) -> None:
        self._add_job("J1", "t_plus_1d", "MISSED_WINDOW", scheduled=BASE, completed=BASE + timedelta(minutes=31))
        report = audit.build_report(self.db_path)

        self.assertEqual(report["backfill"]["missed_windows"], 1)
        self.assertEqual(report["backfill"]["missed_windows_carrying_snapshots"], 0)
        self.assertTrue(report["backfill"]["no_backfill_holds"])

    def test_a_quote_written_onto_a_missed_window_is_caught(self) -> None:
        """This is the violation the guarantee exists to prevent."""

        missed_at = BASE + timedelta(minutes=31)
        self._add_job("J1", "t_plus_1d", "MISSED_WINDOW", scheduled=BASE, completed=missed_at)
        self._add_snapshot("J1", missed_at + timedelta(hours=2), "SNAP1")

        report = audit.build_report(self.db_path)
        backfill = report["backfill"]

        self.assertFalse(backfill["no_backfill_holds"])
        self.assertEqual(backfill["missed_windows_carrying_snapshots"], 1)
        self.assertEqual(backfill["backfill_violations"][0]["market_job_id"], "J1")
        self.assertEqual(report["status"], "ATTENTION")

    # ── 5. leakage ───────────────────────────────────────────────
    def test_post_event_metrics_cannot_be_stored_as_a_model_feature(self) -> None:
        """The database itself refuses the leak; the audit confirms the stored rows."""

        now = utc_now()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """INSERT INTO event_market_metrics(
                       metric_id,event_id,provider,stable_id,ticker_at_event,event_date,
                       event_trade_date,benchmark_ticker,metric_name,metric_value,
                       metric_value_type,metric_scope,allowed_for_discovery_rank,
                       allowed_as_model_feature,created_at,updated_at)
                   VALUES('M1','EVT1','test_provider','STB1','TEST','2026-08-01',NULL,NULL,
                          'ret_1d','0.1','float','post_event_audit_only',0,1,?,?)""",
                (now, now),
            )
        self.connection.rollback()

        report = audit.build_report(self.db_path)
        self.assertTrue(report["leakage"]["isolation_holds"])
        self.assertEqual(report["leakage"]["used_as_model_feature"], 0)

    # ── report shape ─────────────────────────────────────────────
    def test_markdown_renders_every_section(self) -> None:
        self._add_job("J1", "t_plus_5m", "COMPLETED", scheduled=BASE, completed=BASE)
        self._add_snapshot("J1", BASE + timedelta(seconds=30), "SNAP1")
        text = audit.render_markdown(audit.build_report(self.db_path))
        for heading in ("Anchor correctness", "Window fulfilment", "Capture lateness", "No backfill", "Leakage isolation"):
            self.assertIn(heading, text)


if __name__ == "__main__":
    unittest.main()
