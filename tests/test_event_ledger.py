from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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

    def test_schema_15_migrates_market_jobs_and_preserves_old_rows(self) -> None:
        now = ledger.utc_now()
        self.connection.execute(
            """INSERT INTO canonical_events VALUES (
               'old-event',1,'candidate','candidate','macro_policy','inflation_release',
               '2026-08-26',?,?,NULL,NULL,NULL,NULL,NULL,'test',1)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO event_versions VALUES (
               'old-event',1,?,'candidate','candidate','macro_policy',
               'inflation_release',NULL,'{}','fixture')""",
            (now,),
        )
        self.connection.execute(
            """INSERT INTO assets VALUES (
               'old-asset','etf','GLD','GLD','TwelveData','USD','{}',?,?)""",
            (now, now),
        )
        self.connection.commit()
        self.connection.close()

        legacy = sqlite3.connect(self.db_path)
        legacy.execute("PRAGMA foreign_keys=OFF")
        legacy.execute("DROP TABLE market_jobs")
        legacy.execute(
            """
            CREATE TABLE market_jobs (
                market_job_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,
                asset_id TEXT NOT NULL,provider TEXT NOT NULL,
                observation_window TEXT NOT NULL,status TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,completed_at TEXT,attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,no_trading INTEGER NOT NULL DEFAULT 1 CHECK(no_trading=1),
                UNIQUE(event_id,asset_id,provider,observation_window)
            )
            """
        )
        legacy.execute(
            """INSERT INTO market_jobs VALUES (
               'legacy-job','old-event','old-asset','twelve_data','initial','PENDING',
               ?,NULL,0,NULL,1)""",
            (now,),
        )
        legacy.commit()
        legacy.close()

        self.connection = ledger.open_ledger(self.db_path)
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(market_jobs)")
        }
        self.assertIn("event_version", columns)
        migrated = self.connection.execute(
            "SELECT * FROM market_jobs WHERE market_job_id='legacy-job'"
        ).fetchone()
        self.assertEqual(migrated["event_version"], 1)
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_schema_16_expands_mapping_roles_without_losing_current_rows(self) -> None:
        now = ledger.utc_now()
        self.connection.execute(
            """INSERT INTO canonical_events VALUES (
               'role-event',1,'candidate','candidate','macro_policy','policy_decision',
               '2026-08-28',?,?,NULL,NULL,NULL,NULL,NULL,'test',1)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO event_versions VALUES (
               'role-event',1,?,'candidate','candidate','macro_policy',
               'policy_decision',NULL,'{}','fixture')""",
            (now,),
        )
        self.connection.execute(
            """INSERT INTO assets VALUES (
               'asset-role','etf','SPY','SPY','TwelveData','USD','{}',?,?)""",
            (now, now),
        )
        self.connection.commit()
        self.connection.close()

        legacy = sqlite3.connect(self.db_path)
        legacy.execute("PRAGMA foreign_keys=OFF")
        legacy.executescript(
            """
            DROP TABLE event_asset_mapping_receipts;
            DROP TABLE event_asset_mapping_decisions;
            CREATE TABLE event_asset_mapping_decisions (
                decision_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,
                event_version INTEGER NOT NULL,policy_version TEXT NOT NULL,
                policy_sha256 TEXT NOT NULL,observation_id TEXT NOT NULL,
                source_content_sha256 TEXT NOT NULL,source_published_at TEXT,
                local_received_at TEXT,
                decision TEXT NOT NULL CHECK (decision IN ('MAPPED','NO_MATCH')),
                rule_id TEXT,asset_count INTEGER NOT NULL CHECK (asset_count BETWEEN 0 AND 3),
                created_at TEXT NOT NULL,
                no_trading INTEGER NOT NULL DEFAULT 1 CHECK (no_trading=1),
                FOREIGN KEY (event_id,event_version) REFERENCES event_versions(event_id,version),
                UNIQUE (event_id,event_version,policy_sha256,observation_id,source_content_sha256)
            );
            CREATE TABLE event_asset_mapping_receipts (
                receipt_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,
                event_version INTEGER NOT NULL,mapping_decision_id TEXT NOT NULL,
                asset_id TEXT NOT NULL,relation_type TEXT NOT NULL,
                display_role TEXT NOT NULL CHECK (display_role IN (
                    'DIRECT_SECURITY','MARKET_BENCHMARK','SECTOR_PROXY','THEMATIC_PROXY'
                )),
                proxy_label TEXT NOT NULL,rule_id TEXT NOT NULL,
                policy_version TEXT NOT NULL,policy_sha256 TEXT NOT NULL,
                mapping_rank INTEGER NOT NULL CHECK (mapping_rank BETWEEN 1 AND 3),
                confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                decision TEXT NOT NULL CHECK (decision IN ('SELECTED','REJECTED_CAP','SUPERSEDED')),
                reason_codes_json TEXT NOT NULL,created_at TEXT NOT NULL,
                no_trading INTEGER NOT NULL DEFAULT 1 CHECK (no_trading=1),
                FOREIGN KEY (event_id) REFERENCES canonical_events(event_id),
                FOREIGN KEY (event_id,event_version) REFERENCES event_versions(event_id,version),
                FOREIGN KEY (mapping_decision_id) REFERENCES event_asset_mapping_decisions(decision_id),
                FOREIGN KEY (asset_id) REFERENCES assets(asset_id)
            );
            """
        )
        legacy.execute(
            """INSERT INTO event_asset_mapping_decisions VALUES (
               'role-decision','role-event',1,'mapping-v1',?,'role-observation',?,
               '2026-08-28T00:00:00+00:00','2026-08-28T00:01:00+00:00',
               'MAPPED','role-rule',1,?,1)""",
            ("a" * 64, "b" * 64, now),
        )
        legacy.execute(
            """INSERT INTO event_asset_mapping_receipts VALUES (
               'role-receipt','role-event',1,'role-decision','asset-role','MACRO_PROXY',
               'THEMATIC_PROXY','市场代理','role-rule','mapping-v1',?,1,0.9,
               'SELECTED','[]',?,1)""",
            ("a" * 64, now),
        )
        legacy.commit()
        legacy.close()

        self.connection = ledger.open_ledger(self.db_path)

        self.assertEqual(
            self.connection.execute(
                "SELECT display_role FROM event_asset_mapping_receipts"
            ).fetchone()[0],
            "THEMATIC_PROXY",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM event_asset_mapping_receipts_legacy_schema16_roles"
            ).fetchone()[0],
            1,
        )
        self.connection.execute(
            "UPDATE event_asset_mapping_receipts SET display_role='DIRECT_ASSET'"
        )
        self.connection.execute(
            "UPDATE event_asset_mapping_receipts SET display_role='US_LISTED_PROXY'"
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        linked_tables = {
            row["table"]
            for row in self.connection.execute(
                "PRAGMA foreign_key_list(market_job_anchor_links)"
            )
        }
        self.assertIn("market_jobs", linked_tables)
        self.assertNotIn("market_jobs_schema14", linked_tables)

    def test_schema_15_repairs_early_mapping_tables_and_preserves_safe_rows(self) -> None:
        now = ledger.utc_now()
        self.connection.execute(
            """INSERT INTO canonical_events VALUES (
               'mapping-event',1,'candidate','candidate','macro_policy','policy_decision',
               '2026-08-26',?,?,NULL,NULL,NULL,NULL,NULL,'test',1)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO event_versions VALUES (
               'mapping-event',1,?,'candidate','candidate','macro_policy',
               'policy_decision',NULL,'{}','fixture')""",
            (now,),
        )
        self.connection.execute(
            """INSERT INTO sources VALUES (
               'mapping-source','Mapping source','official','P1',1,1,?,?)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO raw_observations VALUES (
               'obs-1','mapping-source','external-1',?,?,
               'Initial title','Initial summary',NULL,?,'{}','captured')""",
            (now, now, "c" * 64),
        )
        self.connection.execute(
            """INSERT INTO source_revisions VALUES (
               'revision-1','obs-1','mapping-source','external-1',1,'edit',?, ?,
               'Revised title','Revised summary','{}')""",
            (now, "b" * 64),
        )
        self.connection.execute(
            """INSERT INTO event_observations VALUES (
               'mapping-event','obs-1','discovery_source',?)""",
            (now,),
        )
        self.connection.execute(
            """INSERT INTO assets VALUES (
               'asset-gld','etf','GLD','GLD','TwelveData','USD','{}',?,?)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO assets VALUES (
               'asset-uso','etf','USO','USO','TwelveData','USD','{}',?,?)""",
            (now, now),
        )
        for impact_id, asset_id, decision_id, rule_id in (
            ("impact-valid", "asset-gld", "decision-1", "macro-gold"),
            ("impact-invalid", "asset-uso", "missing-decision", "macro-oil"),
        ):
            self.connection.execute(
                """INSERT INTO event_asset_impacts(
                       impact_id,event_id,asset_id,relation_type,direction,impact_score,
                       confidence,reason_codes_json,assessment_source,mapping_decision_id,
                       market_observation_allowed,no_trading,created_at,updated_at
                   ) VALUES (?, 'mapping-event',?,'MACRO_PROXY','ABSTAIN',0,0.9,'[]',
                             ?,?,1,1,?,?)""",
                (
                    impact_id,
                    asset_id,
                    f"automatic_asset_mapping_v1:{rule_id}",
                    decision_id,
                    now,
                    now,
                ),
            )
            self.connection.execute(
                """INSERT INTO market_jobs(
                       market_job_id,event_id,event_version,asset_id,provider,
                       observation_window,status,scheduled_at,completed_at,attempts,
                       last_error,no_trading
                   ) VALUES (?, 'mapping-event',1,?,'twelve_data','t_plus_30m',
                             'PENDING',?,NULL,0,NULL,1)""",
                (f"job-{asset_id}", asset_id, now),
            )
        self.connection.commit()
        self.connection.close()

        legacy = sqlite3.connect(self.db_path)
        legacy.execute("PRAGMA foreign_keys=OFF")
        legacy.executescript(
            """
            DROP TABLE event_asset_mapping_receipts;
            DROP TABLE event_asset_mapping_decisions;
            CREATE TABLE event_asset_mapping_decisions(
                decision_id TEXT,event_id TEXT,event_version INTEGER,
                policy_version TEXT,policy_sha256 TEXT,observation_id TEXT,
                source_content_sha256 TEXT,decision TEXT,rule_id TEXT,
                asset_count INTEGER,created_at TEXT
            );
            CREATE TABLE event_asset_mapping_receipts(
                receipt_id TEXT,event_id TEXT,event_version INTEGER,
                mapping_decision_id TEXT,asset_id TEXT,relation_type TEXT,
                display_role TEXT,proxy_label TEXT,rule_id TEXT,policy_version TEXT,
                policy_sha256 TEXT,mapping_rank INTEGER,confidence REAL,
                decision TEXT,reason_codes_json TEXT,created_at TEXT
            );
            """
        )
        legacy.execute(
            """INSERT INTO event_asset_mapping_decisions VALUES (
               'decision-1','mapping-event',1,'mapping-v1',?,'obs-1',?,
               'MAPPED','macro-gold',1,?)""",
            ("a" * 64, "b" * 64, now),
        )
        legacy.execute(
            """INSERT INTO event_asset_mapping_receipts VALUES (
               'receipt-1','mapping-event',1,'decision-1','asset-gld','MACRO_PROXY',
               'THEMATIC_PROXY','黄金ETF代理','macro-gold','mapping-v1',?,1,
               0.9,'SELECTED','[]',?)""",
            ("a" * 64, now),
        )
        legacy.commit()
        legacy.close()

        self.connection = ledger.open_ledger(self.db_path)
        decision = self.connection.execute(
            "SELECT * FROM event_asset_mapping_decisions WHERE decision_id='decision-1'"
        ).fetchone()
        receipt = self.connection.execute(
            "SELECT * FROM event_asset_mapping_receipts WHERE receipt_id='receipt-1'"
        ).fetchone()
        self.assertIsNotNone(decision)
        self.assertIsNotNone(receipt)
        self.assertEqual(decision["no_trading"], 1)
        self.assertEqual(receipt["no_trading"], 1)
        impact_states = {
            row["asset_id"]: row["market_observation_allowed"]
            for row in self.connection.execute(
                "SELECT asset_id,market_observation_allowed FROM event_asset_impacts"
            )
        }
        self.assertEqual(impact_states, {"asset-gld": 1, "asset-uso": 0})
        job_states = {
            row["asset_id"]: row["status"]
            for row in self.connection.execute(
                "SELECT asset_id,status FROM market_jobs"
            )
        }
        self.assertEqual(job_states["asset-gld"], "PENDING")
        self.assertEqual(
            job_states["asset-uso"], "CANCELLED_MAPPING_MIGRATION_INVALID"
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM event_asset_mapping_decisions_legacy_schema15"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM event_asset_mapping_receipts_legacy_schema15"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            ledger._primary_key_columns(
                self.connection, "event_asset_mapping_decisions"
            ),
            ("decision_id",),
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_schema_15_archives_unproven_mapping_chains_and_disables_impacts(self) -> None:
        now = ledger.utc_now()
        cases = (
            ("policy-mismatch", "a" * 64, "c" * 64, "b" * 64, 1, "valid"),
            ("count-mismatch", "a" * 64, "a" * 64, "b" * 64, 2, "valid"),
            (
                "invalid-hash",
                "not-a-sha256",
                "not-a-sha256",
                "b" * 64,
                1,
                "valid",
            ),
            ("fictional-observation", "a" * 64, "a" * 64, "b" * 64, 1, "missing"),
            ("wrong-event", "a" * 64, "a" * 64, "b" * 64, 1, "wrong_event"),
            ("wrong-source-hash", "a" * 64, "a" * 64, "b" * 64, 1, "wrong_hash"),
        )
        self.connection.execute(
            """INSERT INTO sources VALUES (
               'mapping-invalid-source','Mapping invalid source','official','P1',1,1,?,?)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO canonical_events VALUES (
               'mapping-provenance-owner',1,'candidate','candidate','macro_policy',
               'policy_decision','2026-08-26',?,?,NULL,NULL,NULL,NULL,NULL,'test',1)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO event_versions VALUES (
               'mapping-provenance-owner',1,?,'candidate','candidate','macro_policy',
               'policy_decision',NULL,'{}','fixture')""",
            (now,),
        )
        for (
            case,
            _decision_hash,
            _receipt_hash,
            source_hash,
            _asset_count,
            provenance,
        ) in cases:
            event_id = f"mapping-{case}"
            asset_id = f"asset-{case}"
            decision_id = f"decision-{case}"
            observation_id = f"obs-{case}"
            self.connection.execute(
                """INSERT INTO canonical_events VALUES (
                   ?,1,'candidate','candidate','macro_policy','policy_decision',
                   '2026-08-26',?,?,NULL,NULL,NULL,NULL,NULL,'test',1)""",
                (event_id, now, now),
            )
            self.connection.execute(
                """INSERT INTO event_versions VALUES (
                   ?,1,?,'candidate','candidate','macro_policy',
                   'policy_decision',NULL,'{}','fixture')""",
                (event_id, now),
            )
            if provenance != "missing":
                actual_source_hash = "d" * 64 if provenance == "wrong_hash" else source_hash
                self.connection.execute(
                    """INSERT INTO raw_observations VALUES (
                       ?,'mapping-invalid-source',?,?,?,'Title','Summary',NULL,?,
                       '{}','captured')""",
                    (
                        observation_id,
                        f"external-{case}",
                        now,
                        now,
                        actual_source_hash,
                    ),
                )
                linked_event_id = (
                    "mapping-provenance-owner"
                    if provenance == "wrong_event"
                    else event_id
                )
                self.connection.execute(
                    "INSERT INTO event_observations VALUES (?,?, 'discovery_source',?)",
                    (linked_event_id, observation_id, now),
                )
            self.connection.execute(
                """INSERT INTO assets VALUES (
                   ?,'etf',?,?, 'TwelveData','USD','{}',?,?)""",
                (asset_id, case.upper(), case.upper(), now, now),
            )
            self.connection.execute(
                """INSERT INTO event_asset_impacts(
                       impact_id,event_id,asset_id,relation_type,direction,impact_score,
                       confidence,reason_codes_json,assessment_source,mapping_decision_id,
                       market_observation_allowed,no_trading,created_at,updated_at
                   ) VALUES (?,?,?,'MACRO_PROXY','ABSTAIN',0,0.9,'[]',?,?,1,1,?,?)""",
                (
                    f"impact-{case}",
                    event_id,
                    asset_id,
                    "automatic_asset_mapping_v1:macro-gold",
                    decision_id,
                    now,
                    now,
                ),
            )
            self.connection.execute(
                """INSERT INTO market_jobs(
                       market_job_id,event_id,event_version,asset_id,provider,
                       observation_window,status,scheduled_at,completed_at,attempts,
                       last_error,no_trading
                   ) VALUES (?,?,1,?,'twelve_data','t_plus_30m','PENDING',?,NULL,0,NULL,1)""",
                (f"job-{case}", event_id, asset_id, now),
            )
        self.connection.commit()
        self.connection.close()

        legacy = sqlite3.connect(self.db_path)
        legacy.execute("PRAGMA foreign_keys=OFF")
        legacy.executescript(
            """
            DROP TABLE event_asset_mapping_receipts;
            DROP TABLE event_asset_mapping_decisions;
            CREATE TABLE event_asset_mapping_decisions(
                decision_id TEXT,event_id TEXT,event_version INTEGER,
                policy_version TEXT,policy_sha256 TEXT,observation_id TEXT,
                source_content_sha256 TEXT,decision TEXT,rule_id TEXT,
                asset_count INTEGER,created_at TEXT
            );
            CREATE TABLE event_asset_mapping_receipts(
                receipt_id TEXT,event_id TEXT,event_version INTEGER,
                mapping_decision_id TEXT,asset_id TEXT,relation_type TEXT,
                display_role TEXT,proxy_label TEXT,rule_id TEXT,policy_version TEXT,
                policy_sha256 TEXT,mapping_rank INTEGER,confidence REAL,
                decision TEXT,reason_codes_json TEXT,created_at TEXT
            );
            """
        )
        for (
            case,
            decision_hash,
            receipt_hash,
            source_hash,
            asset_count,
            _provenance,
        ) in cases:
            event_id = f"mapping-{case}"
            asset_id = f"asset-{case}"
            decision_id = f"decision-{case}"
            observation_id = f"obs-{case}"
            legacy.execute(
                """INSERT INTO event_asset_mapping_decisions VALUES (
                   ?,?,1,'mapping-v1',?,?,?,'MAPPED','macro-gold',?,?)""",
                (
                    decision_id,
                    event_id,
                    decision_hash,
                    observation_id,
                    source_hash,
                    asset_count,
                    now,
                ),
            )
            legacy.execute(
                """INSERT INTO event_asset_mapping_receipts VALUES (
                   ?,?,1,?,?,'MACRO_PROXY','THEMATIC_PROXY','黄金ETF代理',
                   'macro-gold','mapping-v1',?,1,0.9,'SELECTED','[]',?)""",
                (
                    f"receipt-{case}",
                    event_id,
                    decision_id,
                    asset_id,
                    receipt_hash,
                    now,
                ),
            )
        legacy.commit()
        legacy.close()

        self.connection = ledger.open_ledger(self.db_path)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM event_asset_mapping_decisions"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM event_asset_mapping_receipts"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM event_asset_impacts
                    WHERE market_observation_allowed=0"""
            ).fetchone()[0],
            len(cases),
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM market_jobs
                    WHERE status='CANCELLED_MAPPING_MIGRATION_INVALID'"""
            ).fetchone()[0],
            len(cases),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM event_asset_mapping_decisions_legacy_schema15"
            ).fetchone()[0],
            len(cases),
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM event_asset_mapping_receipts_legacy_schema15"
            ).fetchone()[0],
            len(cases),
        )

    def test_schema_15_archives_market_bars_that_cannot_bind_an_asset(self) -> None:
        self.connection.close()
        legacy = sqlite3.connect(self.db_path)
        legacy.execute("PRAGMA foreign_keys=OFF")
        legacy.executescript(
            """
            DROP TABLE market_bars;
            CREATE TABLE market_bars(
                provider TEXT,provider_symbol TEXT,interval TEXT,bar_time TEXT,
                price TEXT,raw_json TEXT,fetched_at TEXT
            );
            INSERT INTO market_bars VALUES (
                'twelve_data','GLD','1min','2026-08-26T00:00:00+00:00',
                '100','{}','2026-08-26T00:01:00+00:00'
            );
            """
        )
        legacy.commit()
        legacy.close()

        self.connection = ledger.open_ledger(self.db_path)
        self.assertIn(
            "asset_id",
            set(ledger._table_columns(self.connection, "market_bars")),
        )
        self.assertEqual(
            ledger._primary_key_columns(self.connection, "market_bars"),
            ("provider", "asset_id", "interval", "bar_time"),
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM market_bars").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM market_bars_legacy_schema15"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_schema_15_migrates_only_market_bars_with_proven_instrument_identity(self) -> None:
        now = ledger.utc_now()
        for values in (
            ("asset-gld", "etf", "GLD", "GLD", "TwelveData", "USD"),
            ("asset-eth", "crypto", "ETH", "ETH/USD", "Binance", "USD"),
        ):
            self.connection.execute(
                """INSERT INTO assets(
                       asset_id,asset_type,symbol,provider_symbol,venue,currency,
                       metadata_json,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,'{}',?,?)""",
                (*values, now, now),
            )
        self.connection.commit()
        self.connection.close()

        legacy = sqlite3.connect(self.db_path)
        legacy.execute("PRAGMA foreign_keys=OFF")
        legacy.executescript(
            """
            DROP TABLE market_bars;
            CREATE TABLE market_bars(
                provider TEXT,asset_id TEXT,provider_symbol TEXT,interval TEXT,
                bar_time TEXT,price TEXT,raw_json TEXT,fetched_at TEXT,
                read_only INTEGER,no_trading INTEGER
            );
            INSERT INTO market_bars VALUES (
                ' TWELVE_DATA ','asset-gld',' gld ','1MIN',
                '2026-08-26T00:00:00+00:00','100','{}',
                '2026-08-26T00:01:00+00:00',1,1
            );
            INSERT INTO market_bars VALUES (
                'twelve_data','asset-gld','USO','1min',
                '2026-08-26T00:01:00+00:00','50','{}',
                '2026-08-26T00:02:00+00:00',1,1
            );
            INSERT INTO market_bars VALUES (
                'binance_public','asset-eth',' ethusdt ','1min',
                '2026-08-26T00:00:00+00:00','3000','{}',
                '2026-08-26T00:01:00+00:00',1,1
            );
            INSERT INTO market_bars VALUES (
                'twelve_data','asset-eth','ETH','1min',
                '2026-08-26T00:02:00+00:00','3001','{}',
                '2026-08-26T00:03:00+00:00',1,1
            );
            """
        )
        legacy.commit()
        legacy.close()

        self.connection = ledger.open_ledger(self.db_path)
        migrated = self.connection.execute(
            """SELECT provider,asset_id,provider_symbol,interval
                 FROM market_bars ORDER BY provider,asset_id"""
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in migrated],
            [
                ("binance_public", "asset-eth", "ETHUSDT", "1min"),
                ("twelve_data", "asset-gld", "GLD", "1min"),
            ],
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM market_bars_legacy_schema15"
            ).fetchone()[0],
            4,
        )
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_unversioned_metrics_only_migrate_when_event_is_provably_v1(self) -> None:
        now = ledger.utc_now()
        for event_id, current_version in (("only-v1", 1), ("evolved", 2)):
            self.connection.execute(
                """INSERT INTO canonical_events VALUES (
                   ?,?,'candidate','candidate','macro_policy','policy_decision',
                   '2026-08-26',?,?,NULL,NULL,NULL,NULL,NULL,'test',1)""",
                (event_id, current_version, now, now),
            )
            for version in range(1, current_version + 1):
                self.connection.execute(
                    """INSERT INTO event_versions VALUES (
                       ?,?,?,'candidate','candidate','macro_policy',
                       'policy_decision',NULL,'{}','fixture')""",
                    (event_id, version, now),
                )
        self.connection.commit()
        self.connection.close()

        legacy = sqlite3.connect(self.db_path)
        legacy.execute("PRAGMA foreign_keys=OFF")
        legacy.executescript(
            """
            DROP TABLE event_market_metrics;
            CREATE TABLE event_market_metrics(
                metric_id TEXT PRIMARY KEY,event_id TEXT NOT NULL,provider TEXT NOT NULL,
                stable_id TEXT,ticker_at_event TEXT,event_date TEXT NOT NULL,
                event_trade_date TEXT,benchmark_ticker TEXT,metric_name TEXT NOT NULL,
                metric_value TEXT NOT NULL,metric_value_type TEXT NOT NULL,
                metric_scope TEXT NOT NULL,allowed_for_discovery_rank INTEGER NOT NULL,
                allowed_as_model_feature INTEGER NOT NULL,created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,UNIQUE(event_id,provider,metric_name)
            );
            """
        )
        for event_id in ("only-v1", "evolved"):
            legacy.execute(
                """INSERT INTO event_market_metrics VALUES (
                   ?,?,'fixture',NULL,'GLD','2026-08-26',NULL,NULL,
                   'reaction_return_t_plus_30m_pct__GLD','1.25','decimal_percent',
                   'post_event_audit_only',0,0,?,?)""",
                (f"metric-{event_id}", event_id, now, now),
            )
        legacy.commit()
        legacy.close()

        self.connection = ledger.open_ledger(self.db_path)
        migrated = self.connection.execute(
            "SELECT event_id,event_version FROM event_market_metrics"
        ).fetchall()
        self.assertEqual(
            [(row["event_id"], row["event_version"]) for row in migrated],
            [("only-v1", 1)],
        )
        archived = self.connection.execute(
            "SELECT event_id FROM event_market_metrics_unversioned_archive ORDER BY event_id"
        ).fetchall()
        self.assertEqual([row["event_id"] for row in archived], ["evolved", "only-v1"])
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_concurrent_open_rechecks_projection_column_after_writer_lock(self) -> None:
        self.connection.close()
        legacy = sqlite3.connect(self.db_path)
        legacy.execute("ALTER TABLE event_asset_impacts DROP COLUMN mapping_decision_id")
        legacy.commit()
        legacy.close()

        barrier = threading.Barrier(2)
        call_lock = threading.Lock()
        initial_checks = 0
        original = ledger._v15_projection_columns_are_current

        def synchronized_initial_check(connection: sqlite3.Connection) -> bool:
            nonlocal initial_checks
            result = original(connection)
            with call_lock:
                wait_for_peer = initial_checks < 2
                if wait_for_peer:
                    initial_checks += 1
            if wait_for_peer:
                barrier.wait(timeout=10)
            return result

        def open_and_inspect() -> tuple[int, int]:
            connection = ledger.open_ledger(self.db_path)
            try:
                column_count = sum(
                    row["name"] == "mapping_decision_id"
                    for row in connection.execute(
                        "PRAGMA table_info(event_asset_impacts)"
                    ).fetchall()
                )
                violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
                return column_count, violations
            finally:
                connection.close()

        with mock.patch.object(
            ledger,
            "_v15_projection_columns_are_current",
            side_effect=synchronized_initial_check,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: open_and_inspect(), range(2)))

        self.assertEqual(results, [(1, 0), (1, 0)])
        self.connection = ledger.open_ledger(self.db_path)

    def test_concurrent_open_rechecks_archive_rebuild_after_writer_lock(self) -> None:
        self.connection.close()
        legacy = sqlite3.connect(self.db_path)
        legacy.execute("PRAGMA foreign_keys=OFF")
        legacy.executescript(
            """
            DROP TABLE market_bars;
            CREATE TABLE market_bars(
                provider TEXT,provider_symbol TEXT,interval TEXT,bar_time TEXT,
                price TEXT,raw_json TEXT,fetched_at TEXT
            );
            INSERT INTO market_bars VALUES (
                'twelve_data','GLD','1min','2026-08-26T00:00:00+00:00',
                '100','{}','2026-08-26T00:01:00+00:00'
            );
            """
        )
        legacy.commit()
        legacy.close()

        barrier = threading.Barrier(2)
        call_lock = threading.Lock()
        initial_checks = 0
        original = ledger._market_bars_are_current

        def synchronized_initial_check(connection: sqlite3.Connection) -> bool:
            nonlocal initial_checks
            result = original(connection)
            with call_lock:
                wait_for_peer = initial_checks < 2
                if wait_for_peer:
                    initial_checks += 1
            if wait_for_peer:
                barrier.wait(timeout=10)
            return result

        def open_and_inspect() -> tuple[tuple[str, ...], int, int, int]:
            connection = ledger.open_ledger(self.db_path)
            try:
                columns = ledger._table_columns(connection, "market_bars")
                archive_count = connection.execute(
                    "SELECT COUNT(*) FROM market_bars_legacy_schema15"
                ).fetchone()[0]
                violations = len(connection.execute("PRAGMA foreign_key_check").fetchall())
                foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
                return columns, int(archive_count), violations, foreign_keys
            finally:
                connection.close()

        with mock.patch.object(
            ledger,
            "_market_bars_are_current",
            side_effect=synchronized_initial_check,
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: open_and_inspect(), range(2)))

        for columns, archive_count, violations, foreign_keys in results:
            self.assertIn("asset_id", columns)
            self.assertEqual(archive_count, 1)
            self.assertEqual(violations, 0)
            self.assertEqual(foreign_keys, 1)
        self.connection = ledger.open_ledger(self.db_path)

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
