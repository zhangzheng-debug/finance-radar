from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import enqueue_observation_job, open_ledger, stable_id, upsert_source, utc_now
import live_candidate_extractor as extractor


class LiveCandidateExtractorTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def add_observation(
        self,
        external_id: str,
        title: str,
        url: str,
        *,
        source_id: str = "opennews_free",
        published_at: str = "2026-07-15T10:00:00Z",
        raw_json: str = "{}",
    ) -> None:
        now = utc_now()
        observation_id = stable_id("OBS", source_id, external_id)
        self.connection.execute(
            """INSERT INTO raw_observations VALUES (
               ?,?,?,?,?,?,?,'',?,?,'captured')""",
            (
                observation_id,
                source_id,
                external_id,
                published_at,
                now,
                title,
                title,
                hashlib.sha256(title.encode()).hexdigest(),
                raw_json,
            ),
        )
        self.connection.execute(
            "UPDATE raw_observations SET canonical_url=? WHERE observation_id=?",
            (url, observation_id),
        )
        enqueue_observation_job(
            self.connection,
            observation_id=observation_id,
            job_type="extract_live_event_candidate",
            priority=90,
            payload={},
        )
        self.connection.commit()

    def test_candidate_remains_unverified_and_creates_evidence_job(self) -> None:
        self.add_observation(
            "macro:news:1",
            "Example Corp files for Chapter 11 bankruptcy",
            "https://example.test/story?tracking=1",
        )
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 1)
        event = self.connection.execute("SELECT * FROM canonical_events").fetchone()
        self.assertEqual(event["status"], "candidate")
        self.assertEqual(event["event_type"], "bankruptcy")
        self.assertIsNone(event["manual_grade"])
        self.assertEqual(event["no_trading"], 1)
        job = self.connection.execute("SELECT * FROM pipeline_jobs").fetchone()
        self.assertEqual(job["status"], "PENDING_PRIMARY_EVIDENCE")

    def test_same_canonical_url_clusters_and_no_rule_completes(self) -> None:
        self.add_observation("a", "Firm announces a cross-border merger", "https://x.com/a/1")
        self.add_observation("b", "Firm announces a cross-border merger", "https://twitter.com/a/1")
        self.add_observation("c", "Ordinary product update", "https://example.test/c")
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["unique_events"], 1)
        self.assertEqual(result["no_candidate"], 1)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0], 1
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM event_observations").fetchone()[0], 2
        )

    def test_title_variations_with_same_url_cluster_as_one_story(self) -> None:
        self.add_observation(
            "nvidia-a",
            "Nvidia announces an earnings update",
            "https://example.test/nvidia-update?utm_source=a",
        )
        self.add_observation(
            "nvidia-b",
            "Nvidia reports quarterly earnings update",
            "https://example.test/nvidia-update?utm_source=b",
        )
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["unique_events"], 1)

    def test_changed_external_id_reuses_existing_story_event(self) -> None:
        self.add_observation(
            "first-provider-id",
            "Example Corp announces quarterly earnings",
            "https://example.test/results?first=1",
        )
        first = extractor.process_pending(self.connection, limit=10)
        first_event = first["event_ids"][0]
        self.add_observation(
            "replacement-provider-id",
            "Example Corp reports quarterly earnings",
            "https://example.test/results?replacement=1",
        )
        second = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(second["event_ids"], [first_event])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0],
            1,
        )

    def test_semantic_gate_retracts_review_job_without_rejecting_event(self) -> None:
        now = utc_now()
        self.add_observation(
            "legacy-commentary",
            "Nvidia opening could unlock surprise earnings upside",
            "https://example.test/commentary",
            raw_json=json.dumps(
                {"item": {"title": "Nvidia opening could unlock surprise earnings upside"}}
            ),
        )
        observation_id = stable_id("OBS", "opennews_free", "legacy-commentary")
        self.connection.execute(
            """INSERT INTO canonical_events VALUES (
               'legacy-event',1,'candidate','candidate','earnings','earnings_or_guidance',
               '2026-07-15',?,?,NULL,NULL,NULL,NULL,'B_P2_discovery_only','opennews_free',1)""",
            (now, now),
        )
        self.connection.execute(
            "INSERT INTO event_observations VALUES (?,?,?,?)",
            ("legacy-event", observation_id, "aggregated_discovery_candidate", now),
        )
        self.connection.execute(
            """INSERT INTO pipeline_jobs VALUES (
               'legacy-job','legacy-event','live_primary_evidence_review',
               'PENDING_PRIMARY_EVIDENCE',50,0,?,NULL,'{}',?,?)""",
            (now, now, now),
        )
        self.connection.commit()

        retracted = extractor.retract_filtered_opennews_candidates(self.connection)
        event = self.connection.execute(
            "SELECT status,label_status,manual_grade FROM canonical_events WHERE event_id='legacy-event'"
        ).fetchone()
        job = self.connection.execute(
            "SELECT status,last_error FROM pipeline_jobs WHERE job_id='legacy-job'"
        ).fetchone()

        self.assertEqual(retracted, 1)
        self.assertEqual(event["status"], "candidate")
        self.assertEqual(event["label_status"], "candidate")
        self.assertIsNone(event["manual_grade"])
        self.assertEqual(job["status"], "COMPLETED_DISCOVERY_FILTERED")
        self.assertIn("semantic_gate", job["last_error"])

    def test_opennews_duplicate_attaches_to_verified_primary_without_auto_reject(self) -> None:
        now = utc_now()
        upsert_source(
            self.connection,
            source_id="ostium_official_x",
            name="Ostium official",
            source_type="project_primary",
            authority_tier="P1_primary",
        )
        self.connection.execute(
            """INSERT INTO raw_observations VALUES (
               'official-obs','ostium_official_x','official','2026-07-15',?,?,?,
               'https://x.com/Ostium/status/1','hash','{}','captured')""",
            (now, "Ostium pauses all trading after OLP vault issue", "Ostium pauses all trading"),
        )
        self.connection.execute(
            """INSERT INTO canonical_events VALUES (
               'official-event',2,'verified','verified','security_incident',
               'protocol_incident_trading_paused','2026-07-15',?,?,NULL,NULL,'Ostium',
               'A','A','ostium_official_x',1)""",
            (now, now),
        )
        self.connection.execute(
            "INSERT INTO event_observations VALUES (?,?,?,?)",
            ("official-event", "official-obs", "confirming_primary_evidence", now),
        )
        self.connection.commit()
        self.add_observation(
            "ostium-aggregate",
            "Ostium pauses trading after vault exploit",
            "https://example.test/ostium-story",
            published_at="2026-07-16T01:00:00Z",
        )

        result = extractor.process_pending(self.connection, limit=10)
        candidate_event = result["event_ids"][0]
        job = self.connection.execute(
            "SELECT status,last_error FROM pipeline_jobs WHERE event_id=?",
            (candidate_event,),
        ).fetchone()
        candidate = self.connection.execute(
            "SELECT status,label_status,manual_grade FROM canonical_events WHERE event_id=?",
            (candidate_event,),
        ).fetchone()
        support = self.connection.execute(
            """SELECT relation_type FROM event_observations
               WHERE event_id='official-event' AND observation_id=?""",
            (stable_id("OBS", "opennews_free", "ostium-aggregate"),),
        ).fetchone()

        self.assertEqual(result["duplicate_events_reconciled"], 1)
        self.assertEqual(job["status"], "COMPLETED_DUPLICATE_CLUSTER")
        self.assertIn("official-event", job["last_error"])
        self.assertEqual(candidate["status"], "candidate")
        self.assertEqual(candidate["label_status"], "candidate")
        self.assertIsNone(candidate["manual_grade"])
        self.assertEqual(support["relation_type"], "aggregated_duplicate_support")

    def test_opennews_research_digest_and_conditional_commentary_are_not_events(self) -> None:
        fixtures = (
            (
                "primer",
                "How crypto venues are building financial operating systems",
                "Research primer commissioned by Example Exchange. " + "analysis " * 500,
            ),
            (
                "digest",
                "STOCKS, BONDS GAIN AS SOFT INFLATION EASES FED HIKE CONCERNS\n"
                "• First market item\n• Second market item\n• Third market item",
                None,
            ),
            (
                "conditional",
                "Nvidia opening could unlock surprise earnings upside",
                None,
            ),
            (
                "non-negative-control",
                "BHP maintained cost guidance despite higher diesel prices and inflation",
                None,
            ),
        )
        for external_id, title, raw_title in fixtures:
            item_title = raw_title or title
            self.add_observation(
                external_id,
                title,
                f"https://example.test/{external_id}",
                raw_json=json.dumps({"item": {"title": item_title}}),
            )
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["no_candidate"], 4)

    def test_entity_action_date_clusters_different_headlines(self) -> None:
        self.add_observation(
            "ostium-a", "Ostium pauses trading after vault exploit", "https://example.test/a"
        )
        self.add_observation(
            "ostium-b", "Perp DEX Ostium appears hacked for $18m", "https://example.test/b"
        )
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["unique_events"], 1)

    def test_same_recurring_title_on_different_dates_does_not_collide(self) -> None:
        self.add_observation(
            "cpi-june",
            "Consumer Price Index official data release",
            "https://example.test/cpi",
            published_at="2026-07-15T10:00:00Z",
        )
        self.add_observation(
            "cpi-july",
            "Consumer Price Index official data release",
            "https://example.test/cpi",
            published_at="2026-08-15T10:00:00Z",
        )
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["unique_events"], 2)

    def test_sec_official_filing_maps_item_and_stays_candidate(self) -> None:
        upsert_source(
            self.connection,
            source_id="sec_current_filings",
            name="SEC",
            source_type="official_primary_feed",
            authority_tier="P0_official",
        )
        raw_json = json.dumps(
            {
                "item": {
                    "form": "8-K",
                    "items": ["2.02"],
                    "company": "Example Corp",
                }
            }
        )
        self.add_observation(
            "sec-one",
            "8-K - Example Corp (0001234567) (Filer)",
            "https://www.sec.gov/Archives/example-index.htm",
            source_id="sec_current_filings",
            raw_json=raw_json,
        )
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 1)
        event = self.connection.execute("SELECT * FROM canonical_events").fetchone()
        self.assertEqual(event["event_type"], "earnings_or_guidance")
        self.assertEqual(event["company_name"], "Example Corp")
        self.assertEqual(event["provisional_grade_cap"], "A_P0_official_candidate")
        self.assertEqual(event["status"], "candidate")
        relation = self.connection.execute("SELECT relation_type FROM event_observations").fetchone()
        self.assertEqual(relation["relation_type"], "official_primary_candidate")

    def test_same_day_official_releases_with_distinct_urls_do_not_cluster(self) -> None:
        upsert_source(
            self.connection,
            source_id="federal_reserve_press",
            name="Federal Reserve",
            source_type="official_primary_feed",
            authority_tier="P0_official",
        )
        self.add_observation(
            "fed-one",
            "Federal Reserve announces first monetary policy action",
            "https://www.federalreserve.gov/release-one.htm",
            source_id="federal_reserve_press",
        )
        self.add_observation(
            "fed-two",
            "Federal Reserve announces second monetary policy action",
            "https://www.federalreserve.gov/release-two.htm",
            source_id="federal_reserve_press",
        )
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["unique_events"], 2)

    def test_fed_category_controls_event_type_and_filters_other_announcements(self) -> None:
        upsert_source(
            self.connection,
            source_id="federal_reserve_press",
            name="Federal Reserve",
            source_type="official_primary_feed",
            authority_tier="P0_official",
        )
        self.add_observation(
            "fed-enforcement",
            "Federal Reserve Board issues enforcement action",
            "https://www.federalreserve.gov/enforcement.htm",
            source_id="federal_reserve_press",
            raw_json=json.dumps({"item": {"category": "Enforcement Actions"}}),
        )
        self.add_observation(
            "fed-other",
            "Federal Reserve notes an institutional anniversary",
            "https://www.federalreserve.gov/other.htm",
            source_id="federal_reserve_press",
            raw_json=json.dumps({"item": {"category": "Other Announcements"}}),
        )
        self.add_observation(
            "fed-enforcement-termination",
            "Federal Reserve Board announces termination of enforcement action",
            "https://www.federalreserve.gov/enforcement-termination.htm",
            source_id="federal_reserve_press",
            raw_json=json.dumps({"item": {"category": "Enforcement Actions"}}),
        )
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["no_candidate"], 1)
        event_types = {
            row["event_type"]
            for row in self.connection.execute("SELECT event_type FROM canonical_events").fetchall()
        }
        self.assertEqual(
            event_types,
            {"enforcement_action", "enforcement_action_termination"},
        )

    def test_high_value_official_feeds_use_source_specific_event_types(self) -> None:
        fixtures = (
            ("cftc_enforcement", "CFTC", "CFTC charges a commodity pool operator", "cftc_enforcement_action"),
            ("sec_litigation_releases", "SEC", "Example issuer and its CEO", "sec_litigation_release"),
            ("sec_trading_suspensions", "SEC", "Trading suspended in Example Holdings", "trading_suspension"),
            ("fda_medwatch", "FDA", "Early Alert: ventilator issue from Example Medical", "product_safety_alert"),
            ("ftc_press", "FTC", "FTC takes action against Example Corp", "enforcement_action"),
            ("fdic_press_releases", "FDIC", "Example Bank assumes insured deposits of Failed Bank", "bank_receivership"),
        )
        for index, (source_id, name, title, _event_type) in enumerate(fixtures):
            upsert_source(
                self.connection,
                source_id=source_id,
                name=name,
                source_type="official_primary_feed",
                authority_tier="P0_official",
            )
            self.add_observation(
                f"official-{index}",
                title,
                f"https://example.test/{source_id}/{index}",
                source_id=source_id,
            )
        result = extractor.process_pending(self.connection, limit=20)
        self.assertEqual(result["candidates"], len(fixtures))
        event_types = {
            row["event_type"]
            for row in self.connection.execute("SELECT event_type FROM canonical_events").fetchall()
        }
        self.assertEqual(event_types, {row[3] for row in fixtures})

    def test_low_signal_ftc_and_fdic_releases_are_not_event_candidates(self) -> None:
        for index, (source_id, name, title) in enumerate((
            ("ftc_press", "FTC", "FTC announces a public workshop"),
            ("ftc_press", "FTC", "FTC endorses a state supreme court proposal"),
            ("cftc_enforcement", "CFTC", "CFTC grants five whistleblower awards"),
            ("fdic_press_releases", "FDIC", "FDIC announces senior staff appointments"),
        )):
            upsert_source(
                self.connection,
                source_id=source_id,
                name=name,
                source_type="official_primary_feed",
                authority_tier="P0_official",
            )
            self.add_observation(
                f"{source_id}-{index}",
                title,
                f"https://example.test/{source_id}",
                source_id=source_id,
            )
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["no_candidate"], 4)

    def test_fdic_enforcement_order_list_is_digest_not_single_company_action(self) -> None:
        upsert_source(
            self.connection,
            source_id="fdic_press_releases",
            name="FDIC",
            source_type="official_primary_feed",
            authority_tier="P0_official",
        )
        self.add_observation(
            "fdic-orders",
            "FDIC publishes enforcement actions for June 2026",
            "https://example.test/fdic-orders",
            source_id="fdic_press_releases",
        )
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 1)
        event = self.connection.execute("SELECT event_type FROM canonical_events").fetchone()
        self.assertEqual(event["event_type"], "bank_enforcement_orders_digest")


if __name__ == "__main__":
    unittest.main()
