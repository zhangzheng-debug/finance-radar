from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import event_ledger as ledger


class EventLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "ledger.sqlite3"
        self.connection = ledger.open_ledger(self.db_path)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def queue_row(self) -> dict[str, str]:
        return {
            "queue_rank": "1",
            "event_candidate_id": "C1",
            "stable_id": "permaticker:1",
            "ticker_at_event": "AAA",
            "company_name": "AAA Corp",
            "event_date": "2026-01-01",
            "event_family": "bankruptcy_or_distress",
            "event_type": "bankruptcy_liquidation",
            "detection_rule": "action == bankruptcyliquidation",
            "detection_value": "bankruptcyliquidation",
            "priority_score": "125",
            "provisional_grade_cap": "A++_candidate",
            "sec_filings_url": "https://www.sec.gov/example",
        }

    def test_source_health_aggregate_has_covering_index(self) -> None:
        indexes = {
            row[1]
            for row in self.connection.execute("PRAGMA index_list('raw_observations')")
        }
        self.assertIn("idx_raw_observations_source_received", indexes)

    def test_import_is_idempotent_and_preserves_versions(self) -> None:
        queue = [self.queue_row()]
        passages = [
            {
                "event_candidate_id": "C1",
                "accession_number": "0001-26-000001",
                "filing_date": "2026-01-01",
                "form": "8-K",
                "items": "1.03",
                "filing_document_url": "https://www.sec.gov/doc",
                "text_sha256": "abc",
                "evidence_passage": "Filed Chapter 11.",
                "matched_keywords": "chapter 11",
                "passage_score": "10",
                "passage_status": "candidate_passage",
            }
        ]
        adjudications = [
            {
                "event_candidate_id": "C1",
                "label_status": "verified",
                "canonical_event_family": "distress_equity_death",
                "canonical_event_type": "chapter_11",
                "manual_grade": "A++",
            }
        ]
        first = ledger.import_active_research(
            self.connection,
            queue_rows=queue,
            passage_rows=passages,
            adjudication_rows=adjudications,
            market_rows=[
                {
                    "event_candidate_id": "C1",
                    "provider": "sharadar",
                    "stable_id": "permaticker:1",
                    "ticker_at_event": "AAA",
                    "event_date": "2026-01-01",
                    "event_trade_date": "2026-01-02",
                    "benchmark_ticker": "SPY",
                    "metric_name": "ret_1d",
                    "metric_value": "-0.25",
                    "metric_value_type": "float",
                    "metric_scope": "post_event_audit_only",
                    "allowed_for_discovery_rank": "false",
                    "allowed_as_model_feature": "false",
                }
            ],
        )
        second = ledger.import_active_research(
            self.connection,
            queue_rows=queue,
            passage_rows=passages,
            adjudication_rows=adjudications,
            market_rows=[
                {
                    "event_candidate_id": "C1",
                    "provider": "sharadar",
                    "stable_id": "permaticker:1",
                    "ticker_at_event": "AAA",
                    "event_date": "2026-01-01",
                    "event_trade_date": "2026-01-02",
                    "benchmark_ticker": "SPY",
                    "metric_name": "ret_1d",
                    "metric_value": "-0.25",
                    "metric_value_type": "float",
                    "metric_scope": "post_event_audit_only",
                    "allowed_for_discovery_rank": "false",
                    "allowed_as_model_feature": "false",
                }
            ],
        )
        self.assertEqual(first.events, second.events)
        summary = ledger.ledger_summary(self.connection)
        self.assertEqual(summary["table_counts"]["canonical_events"], 1)
        self.assertEqual(summary["table_counts"]["event_versions"], 2)
        self.assertEqual(summary["table_counts"]["event_evidence"], 1)
        self.assertEqual(summary["table_counts"]["event_market_metrics"], 1)
        self.assertEqual(summary["event_status"], {"verified": 1})
        self.assertEqual(summary["no_trading_violations"], 0)
        self.assertEqual(summary["auto_verification_violations"], 0)
        self.assertEqual(summary["market_metric_scope_violations"], 0)

    def test_latest_source_content_exposes_newest_revision_without_mutating_raw(self) -> None:
        ledger.upsert_source(
            self.connection,
            source_id="official",
            name="Official source",
            source_type="official_primary",
            authority_tier="P0",
        )
        now = ledger.utc_now()
        observation_id, _ = ledger.record_source_observation(
            self.connection,
            source_id="official",
            external_id="item-1",
            source_published_at=now,
            local_received_at=now,
            title="Initial title",
            summary="Initial summary",
            canonical_url="https://example.test/item-1",
            content_sha256="initial",
            raw_json='{"version":1}',
            revision_kind="new",
        )
        ledger.record_source_observation(
            self.connection,
            source_id="official",
            external_id="item-1",
            source_published_at=now,
            local_received_at=now,
            title="Corrected title",
            summary="Corrected summary",
            canonical_url="https://example.test/item-1",
            content_sha256="corrected",
            raw_json='{"version":2}',
            revision_kind="edit",
        )
        raw = self.connection.execute(
            "SELECT title,summary FROM raw_observations WHERE observation_id=?",
            (observation_id,),
        ).fetchone()
        latest = self.connection.execute(
            "SELECT title,summary,latest_revision_no FROM latest_source_content WHERE observation_id=?",
            (observation_id,),
        ).fetchone()
        self.assertEqual((raw["title"], raw["summary"]), ("Initial title", "Initial summary"))
        self.assertEqual(
            (latest["title"], latest["summary"], latest["latest_revision_no"]),
            ("Corrected title", "Corrected summary", 2),
        )

    def test_registered_manual_evidence_is_preserved_outside_passage_window(self) -> None:
        adjudication = {
            "event_candidate_id": "C1",
            "label_status": "verified",
            "canonical_event_family": "distress_equity_death",
            "canonical_event_type": "old_common_cancelled_without_consideration",
            "manual_grade": "S",
            "evidence_date": "2026-01-10",
            "evidence_form": "8-K",
            "evidence_item": "1.03",
            "evidence_url": "https://www.sec.gov/manual-terminal-event",
            "evidence_summary": "Confirmed plan cancelled old common without consideration.",
        }
        ledger.import_active_research(
            self.connection,
            queue_rows=[self.queue_row()],
            passage_rows=[],
            adjudication_rows=[adjudication],
        )
        evidence = self.connection.execute(
            "SELECT evidence_url,evidence_status,auto_verification_allowed "
            "FROM event_evidence"
        ).fetchone()
        self.assertEqual(evidence["evidence_url"], adjudication["evidence_url"])
        self.assertEqual(evidence["evidence_status"], "accepted_manual_primary_evidence")
        self.assertEqual(evidence["auto_verification_allowed"], 0)

    def test_explicit_readjudication_updates_current_state_and_appends_version(self) -> None:
        verified = {
            "event_candidate_id": "C1",
            "label_status": "verified",
            "canonical_event_family": "insolvency_restructuring",
            "canonical_event_type": "interim_judicial_management",
            "manual_grade": "A++",
        }
        rejected = {
            "event_candidate_id": "C1",
            "label_status": "rejected",
            "canonical_event_family": "temporal_event_proxy",
            "canonical_event_type": "later_proxy_for_earlier_court_order",
            "manual_grade": "rejected",
        }
        ledger.import_active_research(
            self.connection,
            queue_rows=[self.queue_row()],
            passage_rows=[],
            adjudication_rows=[verified],
        )
        ledger.import_active_research(
            self.connection,
            queue_rows=[self.queue_row()],
            passage_rows=[],
            adjudication_rows=[rejected],
        )
        event = self.connection.execute(
            "SELECT current_version,status,event_family,event_type,manual_grade "
            "FROM canonical_events WHERE event_id=?",
            (ledger.canonical_event_id("C1"),),
        ).fetchone()
        versions = self.connection.execute(
            "SELECT version,status FROM event_versions WHERE event_id=? ORDER BY version",
            (ledger.canonical_event_id("C1"),),
        ).fetchall()
        self.assertEqual(
            tuple(event),
            (
                3,
                "rejected",
                "temporal_event_proxy",
                "later_proxy_for_earlier_court_order",
                "rejected",
            ),
        )
        self.assertEqual(
            [(row["version"], row["status"]) for row in versions],
            [(1, "candidate"), (2, "verified"), (3, "rejected")],
        )


if __name__ == "__main__":
    unittest.main()
