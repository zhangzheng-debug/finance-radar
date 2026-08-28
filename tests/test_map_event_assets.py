from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger, utc_now  # noqa: E402
from map_event_assets import map_event_assets  # noqa: E402
import observe_live_event_markets as observer  # noqa: E402
from audit_live_pipeline import audit  # noqa: E402
from app.models.event_asset_mapping import load_asset_mapping_policy  # noqa: E402
from app.models.issuer_directory import IssuerDirectory  # noqa: E402


class EventAssetMappingPersistenceTests(unittest.TestCase):
    TODAY = dt.date(2026, 8, 26)
    PUBLISHED = dt.datetime(2026, 8, 25, 14, 0, tzinfo=dt.timezone.utc)

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = open_ledger(Path(self.temp_dir.name) / "ledger.sqlite3")
        now = utc_now()
        self.connection.execute(
            """INSERT INTO sources VALUES (
               'news','News','news_api','P2',1,1,?,?)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO raw_observations VALUES (
               'obs','news','external',?,?,?,?,'https://example.test/story','hash','{}','stored')""",
            (
                self.PUBLISHED.isoformat(),
                (self.PUBLISHED + dt.timedelta(minutes=1)).isoformat(),
                "Iranian missile attacks threaten commercial shipping near Hormuz",
                "Oil tankers and energy supply routes were affected.",
            ),
        )
        self.connection.execute(
            """INSERT INTO canonical_events VALUES (
               'evt',1,'candidate','candidate','geopolitical',
               'active_iranian_attacks_and_threats_to_commercial_shipping',
               '2026-08-25',?,?,NULL,NULL,'Iran',NULL,NULL,'news',1)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO event_versions VALUES (
               'evt',1,?,'candidate','candidate','geopolitical',
               'active_iranian_attacks_and_threats_to_commercial_shipping',
               NULL,'{}','capture')""",
            (now,),
        )
        self.connection.execute(
            """INSERT INTO event_observations VALUES (
               'evt','obs','discovery_capture',?)""",
            (now,),
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_apply_persists_versioned_receipts_and_directionless_projection(self) -> None:
        result = map_event_assets(
            self.connection, freshness_days=14, today=self.TODAY, apply=True
        )

        self.assertEqual(result["mapped_events"], 1)
        self.assertEqual(result["mapped_assets"], 3)
        receipts = self.connection.execute(
            "SELECT * FROM event_asset_mapping_receipts ORDER BY mapping_rank"
        ).fetchall()
        self.assertEqual([row["mapping_rank"] for row in receipts], [1, 2, 3])
        self.assertEqual({row["event_version"] for row in receipts}, {1})
        self.assertEqual({row["decision"] for row in receipts}, {"SELECTED"})
        self.assertTrue(all(len(row["policy_sha256"]) == 64 for row in receipts))
        impacts = self.connection.execute(
            "SELECT * FROM event_asset_impacts ORDER BY assessment_source,asset_id"
        ).fetchall()
        self.assertEqual(len(impacts), 3)
        self.assertEqual({row["direction"] for row in impacts}, {"ABSTAIN"})
        self.assertEqual({row["impact_score"] for row in impacts}, {0})
        self.assertEqual({row["market_observation_allowed"] for row in impacts}, {1})
        decision = self.connection.execute(
            "SELECT * FROM event_asset_mapping_decisions"
        ).fetchone()
        self.assertEqual(decision["decision"], "MAPPED")
        self.assertEqual(decision["asset_count"], 3)
        metadata_rows = self.connection.execute(
            "SELECT symbol,metadata_json FROM assets ORDER BY symbol"
        ).fetchall()
        for row in metadata_rows:
            metadata = json.loads(row["metadata_json"])
            self.assertEqual(metadata["session_timezone"], "America/New_York")
            self.assertEqual(metadata["regular_open_local"], "09:30")
            self.assertEqual(metadata["regular_close_local"], "16:00")

    def test_apply_is_incremental_and_idempotent(self) -> None:
        first = map_event_assets(
            self.connection, freshness_days=14, today=self.TODAY, apply=True
        )
        second = map_event_assets(
            self.connection, freshness_days=14, today=self.TODAY, apply=True
        )
        self.assertEqual(first["selected_events"], 1)
        self.assertEqual(second["selected_events"], 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM event_asset_mapping_receipts"
            ).fetchone()[0],
            3,
        )

    def test_public_issuer_directory_mapping_persists_direct_security(self) -> None:
        self.connection.execute(
            """UPDATE canonical_events
                  SET event_family='earnings',event_type='earnings_or_guidance',
                      discovery_source='opennews_free'
                WHERE event_id='evt'"""
        )
        self.connection.execute(
            """UPDATE event_versions
                  SET event_family='earnings',event_type='earnings_or_guidance'
                WHERE event_id='evt' AND version=1"""
        )
        self.connection.execute(
            """UPDATE raw_observations
                  SET title='NVIDIA Q2 EARNINGS - REVENUE BEATS ESTIMATES',
                      summary='Data-center revenue and guidance were reported.'
                WHERE observation_id='obs'"""
        )
        self.connection.commit()
        directory = IssuerDirectory.from_document(
            {
                "fields": ["cik", "name", "ticker", "exchange"],
                "data": [[1045810, "NVIDIA CORP", "NVDA", "Nasdaq"]],
            },
            source_sha256="b" * 64,
        )

        result = map_event_assets(
            self.connection,
            freshness_days=14,
            today=self.TODAY,
            issuer_directory=directory,
            apply=True,
        )

        self.assertEqual(result["issuer_resolved_events"], 1)
        self.assertEqual(result["mapped_assets"], 2)
        self.assertEqual(
            [
                row["symbol"]
                for row in self.connection.execute(
                    "SELECT symbol FROM assets ORDER BY symbol"
                ).fetchall()
            ],
            ["NVDA", "SPY"],
        )
        decision = self.connection.execute(
            "SELECT decision,rule_id,asset_count FROM event_asset_mapping_decisions"
        ).fetchone()
        self.assertEqual(tuple(decision), ("MAPPED", "resolved-public-company-v1", 2))

    def test_country_proxy_persists_and_schedules_twelve_data_jobs(self) -> None:
        self.connection.execute(
            """UPDATE canonical_events
                  SET event_family='macro_policy',event_type='country_policy_action',
                      discovery_source='opennews_free',company_name='South Korea'
                WHERE event_id='evt'"""
        )
        self.connection.execute(
            """UPDATE event_versions
                  SET event_family='macro_policy',event_type='country_policy_action',
                      facts_json=?
                WHERE event_id='evt' AND version=1""",
            (
                json.dumps(
                    {
                        "source_shape": "SINGLE_EVENT",
                        "event_claim_text": (
                            "South Korea raises interest rates after an inflation surprise"
                        ),
                    }
                ),
            ),
        )
        self.connection.execute(
            """UPDATE raw_observations
                  SET title='South Korea raises interest rates after an inflation surprise',
                      summary='The Bank of Korea announced a country policy action.'
                WHERE observation_id='obs'"""
        )
        self.connection.commit()

        result = map_event_assets(
            self.connection, freshness_days=14, today=self.TODAY, apply=True
        )

        self.assertEqual(result["mapped_events"], 1)
        self.assertEqual(result["mapped_assets"], 1)
        self.assertEqual(result["rule_hits"], {"south-korea-country-market-v1": 1})
        asset = self.connection.execute(
            "SELECT asset_type,symbol,provider_symbol,venue FROM assets"
        ).fetchone()
        self.assertEqual(tuple(asset), ("etf", "EWY", "EWY", "TwelveData"))

        inserted = observer.schedule_jobs(
            self.connection,
            freshness_days=14,
            today=self.TODAY,
            now=self.PUBLISHED + dt.timedelta(days=1),
        )
        self.assertEqual(inserted, 7)
        providers = self.connection.execute(
            "SELECT DISTINCT provider FROM market_jobs"
        ).fetchall()
        self.assertEqual([row["provider"] for row in providers], ["twelve_data"])

    def test_new_event_version_no_match_deactivates_prior_automatic_mappings(self) -> None:
        first = map_event_assets(
            self.connection, freshness_days=14, today=self.TODAY, apply=True
        )
        scheduled = observer.schedule_jobs(
            self.connection,
            freshness_days=14,
            today=self.TODAY,
            now=self.PUBLISHED + dt.timedelta(days=1),
        )
        self.assertEqual(scheduled, 21)
        now = utc_now()
        self.connection.execute(
            """UPDATE canonical_events
                  SET current_version=2,event_family='other',event_type='unmapped'
                WHERE event_id='evt'"""
        )
        self.connection.execute(
            """INSERT INTO event_versions VALUES (
               'evt',2,?,'candidate','candidate','other','unmapped',
               NULL,'{}','correction')""",
            (now,),
        )
        self.connection.commit()

        second = map_event_assets(
            self.connection, freshness_days=14, today=self.TODAY, apply=True
        )

        self.assertEqual(first["mapped_assets"], 3)
        self.assertEqual(second["selected_events"], 1)
        self.assertEqual(second["mapped_events"], 0)
        self.assertEqual(second["unmapped_events"], 1)
        self.assertEqual(second["superseded_impacts"], 3)
        decision = self.connection.execute(
            """SELECT decision,asset_count FROM event_asset_mapping_decisions
                WHERE event_id='evt' AND event_version=2"""
        ).fetchone()
        self.assertEqual((decision["decision"], decision["asset_count"]), ("NO_MATCH", 0))
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM event_asset_impacts
                    WHERE event_id='evt'
                      AND assessment_source LIKE 'automatic_asset_mapping_v1:%'
                      AND market_observation_allowed=1"""
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM event_asset_mapping_receipts
                    WHERE event_id='evt' AND event_version=1"""
            ).fetchone()[0],
            3,
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM market_jobs
                    WHERE event_id='evt' AND status='CANCELLED_MAPPING_SUPERSEDED'"""
            ).fetchone()[0],
            21,
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM market_jobs
                    WHERE event_id='evt' AND status IN ('PENDING','RETRY')"""
            ).fetchone()[0],
            0,
        )

    def test_existing_decision_repairs_missing_receipt_and_disabled_projection(self) -> None:
        map_event_assets(
            self.connection, freshness_days=14, today=self.TODAY, apply=True
        )
        decision_id = self.connection.execute(
            "SELECT decision_id FROM event_asset_mapping_decisions"
        ).fetchone()["decision_id"]
        victim = self.connection.execute(
            """SELECT receipt.receipt_id,impact.impact_id
                 FROM event_asset_mapping_receipts receipt
                 JOIN event_asset_impacts impact
                   ON impact.mapping_decision_id=receipt.mapping_decision_id
                  AND impact.asset_id=receipt.asset_id
                  AND impact.relation_type=receipt.relation_type
                ORDER BY receipt.mapping_rank LIMIT 1"""
        ).fetchone()
        self.connection.execute(
            "DELETE FROM event_asset_mapping_receipts WHERE receipt_id=?",
            (victim["receipt_id"],),
        )
        self.connection.execute(
            """UPDATE event_asset_impacts
                  SET market_observation_allowed=0,mapping_decision_id=NULL
                WHERE impact_id=?""",
            (victim["impact_id"],),
        )
        self.connection.commit()

        repaired = map_event_assets(
            self.connection, freshness_days=14, today=self.TODAY, apply=True
        )
        stable = map_event_assets(
            self.connection, freshness_days=14, today=self.TODAY, apply=True
        )

        self.assertEqual(repaired["selected_events"], 1)
        self.assertEqual(repaired["mapped_events"], 1)
        self.assertEqual(repaired["receipts_inserted"], 1)
        receipts = self.connection.execute(
            """SELECT mapping_rank FROM event_asset_mapping_receipts
                WHERE mapping_decision_id=? ORDER BY mapping_rank""",
            (decision_id,),
        ).fetchall()
        self.assertEqual([row["mapping_rank"] for row in receipts], [1, 2, 3])
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM event_asset_impacts
                    WHERE mapping_decision_id=? AND market_observation_allowed=1
                      AND direction='ABSTAIN' AND impact_score=0 AND no_trading=1""",
                (decision_id,),
            ).fetchone()[0],
            3,
        )
        self.assertEqual(stable["selected_events"], 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM event_asset_mapping_decisions"
            ).fetchone()[0],
            1,
        )

    def test_explicit_event_id_can_backfill_outside_freshness_window(self) -> None:
        self.connection.execute(
            "UPDATE canonical_events SET event_date='2020-01-01' WHERE event_id='evt'"
        )
        self.connection.commit()

        ordinary = map_event_assets(
            self.connection, freshness_days=14, today=self.TODAY, apply=True
        )
        explicit = map_event_assets(
            self.connection,
            freshness_days=14,
            today=self.TODAY,
            event_ids=["evt"],
            apply=True,
        )

        self.assertEqual(ordinary["selected_events"], 0)
        self.assertEqual(explicit["selected_events"], 1)
        self.assertEqual(explicit["mapped_events"], 1)
        self.assertEqual(explicit["mapped_assets"], 3)
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM event_asset_mapping_decisions
                    WHERE event_id='evt'"""
            ).fetchone()[0],
            1,
        )

    def test_relation_type_change_keeps_pending_jobs_for_still_selected_asset(self) -> None:
        base_policy = load_asset_mapping_policy()
        map_event_assets(
            self.connection,
            freshness_days=14,
            today=self.TODAY,
            policy=base_policy,
            apply=True,
        )
        inserted = observer.schedule_jobs(
            self.connection,
            freshness_days=14,
            today=self.TODAY,
            now=self.PUBLISHED + dt.timedelta(days=1),
        )
        self.assertEqual(inserted, 21)
        uso = self.connection.execute(
            "SELECT asset_id FROM assets WHERE provider_symbol='USO' AND venue='TwelveData'"
        ).fetchone()
        uso_asset_id = uso["asset_id"]

        changed_registry = dict(base_policy.asset_registry)
        changed_registry["USO"] = replace(
            changed_registry["USO"],
            relation_type="SECTOR",
            role="SECTOR_PROXY",
            proxy_label="能源行业ETF代理",
        )
        changed_policy = replace(
            base_policy,
            policy_version="event-asset-mapping-v1.0.1-test",
            policy_sha256="b" * 64,
            asset_registry=changed_registry,
        )

        remapped = map_event_assets(
            self.connection,
            freshness_days=14,
            today=self.TODAY,
            policy=changed_policy,
            apply=True,
        )

        self.assertEqual(remapped["selected_events"], 1)
        self.assertEqual(remapped["mapped_assets"], 3)
        self.assertEqual(remapped["superseded_impacts"], 1)
        self.assertEqual(
            self.connection.execute(
                """SELECT market_observation_allowed FROM event_asset_impacts
                    WHERE event_id='evt' AND asset_id=? AND relation_type='MACRO_PROXY'""",
                (uso_asset_id,),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT market_observation_allowed FROM event_asset_impacts
                    WHERE event_id='evt' AND asset_id=? AND relation_type='SECTOR'""",
                (uso_asset_id,),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM market_jobs
                    WHERE event_id='evt' AND asset_id=? AND status='PENDING'""",
                (uso_asset_id,),
            ).fetchone()[0],
            7,
        )
        self.assertEqual(
            self.connection.execute(
                """SELECT COUNT(*) FROM market_jobs
                    WHERE event_id='evt' AND asset_id=?
                      AND status='CANCELLED_MAPPING_SUPERSEDED'""",
                (uso_asset_id,),
            ).fetchone()[0],
            0,
        )

    def test_shadow_mode_reports_without_mapping_writes(self) -> None:
        result = map_event_assets(
            self.connection, freshness_days=14, today=self.TODAY, apply=False
        )
        self.assertEqual(result["mode"], "SHADOW")
        self.assertEqual(result["mapped_events"], 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM event_asset_mapping_decisions"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM event_asset_impacts").fetchone()[0],
            0,
        )

    def test_source_capture_can_schedule_read_only_price_jobs_without_evidence_gate(self) -> None:
        map_event_assets(
            self.connection, freshness_days=14, today=self.TODAY, apply=True
        )
        inserted = observer.schedule_jobs(
            self.connection,
            freshness_days=14,
            today=self.TODAY,
            now=self.PUBLISHED + dt.timedelta(days=1),
        )
        self.assertEqual(inserted, 21)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM market_jobs WHERE status='PENDING'"
            ).fetchone()[0],
            21,
        )
        anchors = self.connection.execute(
            "SELECT DISTINCT declared_anchor_kind,anchor_status FROM market_event_anchors"
        ).fetchall()
        self.assertEqual(
            {(row["declared_anchor_kind"], row["anchor_status"]) for row in anchors},
            {("source_published", "EXACT")},
        )

    def test_candidate_automatic_mapping_passes_read_only_safety_audit(self) -> None:
        map_event_assets(
            self.connection, freshness_days=14, today=self.TODAY, apply=True
        )
        result = audit(self.connection)
        self.assertEqual(result["checks"]["candidate_market_observation_violations"], 0)
        self.assertEqual(
            result["checks"]["automatic_asset_mapping_contract_violations"], 0
        )
        self.assertEqual(result["checks"]["automatic_asset_mapping_cap_violations"], 0)
        self.assertEqual(result["checks"]["mapping_receipt_boundary_violations"], 0)


if __name__ == "__main__":
    unittest.main()
