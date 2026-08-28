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
        raw_json: str | None = None,
    ) -> None:
        if raw_json is None:
            raw_json = json.dumps({"item": {"company": "Fixture Subject"}})
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

    def test_subject_unresolved_observation_never_enters_canonical_ledger(self) -> None:
        self.add_observation(
            "unknown-subject",
            "Unnamed issuer files for Chapter 11 bankruptcy",
            "https://example.test/unknown-subject",
            raw_json="{}",
        )

        result = extractor.process_pending(self.connection, limit=10)

        self.assertEqual(result["subject_filtered"], 1)
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0],
            0,
        )
        job = self.connection.execute("SELECT status,last_error FROM observation_jobs").fetchone()
        self.assertEqual(job["status"], "COMPLETED_SUBJECT_FILTERED")
        self.assertEqual(job["last_error"], "subject_unresolved_not_canonical")

    def test_ecb_cultural_notice_stays_a_source_observation_not_a_financial_event(self) -> None:
        upsert_source(
            self.connection,
            source_id="ecb_press",
            name="European Central Bank press and speeches",
            source_type="official_primary_feed",
            authority_tier="P0_official",
        )
        self.add_observation(
            "ecb-cultural-days-concert",
            "ECB Cultural Days concert celebrates European music",
            "https://www.ecb.europa.eu/press/cultural/example.en.html",
            source_id="ecb_press",
            raw_json=json.dumps(
                {
                    "item": {
                        "title": "ECB Cultural Days concert celebrates European music",
                        "summary": (
                            "The European Central Bank welcomes visitors to an evening concert."
                        ),
                    }
                }
            ),
        )

        result = extractor.process_pending(self.connection, limit=10)

        self.assertEqual(result["no_candidate"], 1)
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0],
            0,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0],
            1,
        )

    def test_legacy_ecb_cultural_candidate_is_retracted_without_deleting_capture(self) -> None:
        upsert_source(
            self.connection,
            source_id="ecb_press",
            name="European Central Bank press and speeches",
            source_type="official_primary_feed",
            authority_tier="P0_official",
        )
        self.add_observation(
            "legacy-ecb-concert",
            "ECB Cultural Days concert celebrates European music",
            "https://www.ecb.europa.eu/press/cultural/legacy.en.html",
            source_id="ecb_press",
            raw_json="{}",
        )
        now = utc_now()
        observation_id = stable_id("OBS", "ecb_press", "legacy-ecb-concert")
        self.connection.execute(
            """INSERT INTO canonical_events VALUES (
               'legacy-ecb-event',1,'candidate','candidate','macro_policy','central_bank_policy',
               '2026-07-15',?,?,NULL,NULL,'European Central Bank',NULL,NULL,
               'ecb_press',1)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO event_versions VALUES (
               'legacy-ecb-event',1,?,'candidate','candidate','macro_policy',
               'central_bank_policy',NULL,'{}','legacy_import')""",
            (now,),
        )
        self.connection.execute(
            "INSERT INTO event_observations VALUES (?,?,?,?)",
            (
                "legacy-ecb-event",
                observation_id,
                "official_discovery_candidate",
                now,
            ),
        )
        self.connection.commit()

        retracted = extractor.retract_nonfinancial_official_candidates(self.connection)

        event = self.connection.execute(
            "SELECT status,current_version FROM canonical_events WHERE event_id='legacy-ecb-event'"
        ).fetchone()
        relation = self.connection.execute(
            "SELECT relation_type FROM event_observations WHERE event_id='legacy-ecb-event'"
        ).fetchone()
        self.assertEqual(retracted, 1)
        self.assertEqual(event["status"], "rejected")
        self.assertEqual(event["current_version"], 2)
        self.assertEqual(relation["relation_type"], "filtered_aggregated_noise")
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM raw_observations WHERE observation_id=?",
                (observation_id,),
            ).fetchone()[0],
            1,
        )

    def test_legal_company_name_in_headline_satisfies_subject_gate(self) -> None:
        self.add_observation(
            "named-subject",
            "Example Corp files for Chapter 11 bankruptcy",
            "https://example.test/named-subject",
            raw_json="{}",
        )

        result = extractor.process_pending(self.connection, limit=10)

        self.assertEqual(result["candidates"], 1)
        event = self.connection.execute("SELECT company_name FROM canonical_events").fetchone()
        self.assertEqual(event["company_name"], "Example Corp")

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

    def test_semantic_gate_reaudits_completed_review_job_and_audits_rejection(self) -> None:
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
            """INSERT INTO event_versions VALUES (
               'legacy-event',1,?,'candidate','candidate','earnings','earnings_or_guidance',
               NULL,'{}','legacy_import')""",
            (now,),
        )
        self.connection.execute(
            "INSERT INTO event_observations VALUES (?,?,?,?)",
            ("legacy-event", observation_id, "aggregated_discovery_candidate", now),
        )
        self.connection.execute(
            """INSERT INTO pipeline_jobs VALUES (
               'legacy-job','legacy-event','live_primary_evidence_review',
               'COMPLETED_DISCOVERY_FILTERED',50,0,?,NULL,'{}',?,?)""",
            (now, now, now),
        )
        self.connection.execute(
            """INSERT INTO assets(
               asset_id,asset_type,symbol,provider_symbol,venue,currency,
               metadata_json,created_at,updated_at)
               VALUES('legacy-asset','equity','NVDA','NVDA','NASDAQ','USD','{}',?,?)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO event_asset_impacts(
               impact_id,event_id,asset_id,relation_type,direction,impact_score,
               confidence,reason_codes_json,assessment_source,mapping_decision_id,
               market_observation_allowed,no_trading,created_at,updated_at)
               VALUES('legacy-impact','legacy-event','legacy-asset','PRIMARY','ABSTAIN',0,
                      1.0,'[]','automatic_asset_mapping_v1:legacy-rule',NULL,1,1,?,?)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO market_jobs(
               market_job_id,event_id,event_version,asset_id,provider,
               observation_window,status,scheduled_at,completed_at,attempts,last_error,no_trading)
               VALUES('legacy-market-job','legacy-event',1,'legacy-asset','twelve_data',
                      'T+30m','PENDING',?,NULL,0,NULL,1)""",
            (now,),
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
        self.assertEqual(event["status"], "rejected")
        self.assertEqual(event["label_status"], "rejected")
        self.assertIsNone(event["manual_grade"])
        self.assertEqual(job["status"], "COMPLETED_DISCOVERY_FILTERED")
        self.assertIn("semantic_gate", job["last_error"])
        version = self.connection.execute(
            "SELECT facts_json,change_reason FROM event_versions WHERE event_id='legacy-event' AND version=2"
        ).fetchone()
        self.assertIn("raw_observations_preserved", version["facts_json"])
        self.assertIn("semantic_gate", version["change_reason"])
        relation = self.connection.execute(
            "SELECT relation_type FROM event_observations WHERE event_id='legacy-event'"
        ).fetchone()
        self.assertEqual(relation["relation_type"], "filtered_aggregated_noise")
        impact = self.connection.execute(
            """SELECT market_observation_allowed FROM event_asset_impacts
               WHERE event_id='legacy-event'"""
        ).fetchone()
        market_job = self.connection.execute(
            """SELECT status,last_error FROM market_jobs
               WHERE event_id='legacy-event'"""
        ).fetchone()
        self.assertEqual(impact["market_observation_allowed"], 0)
        self.assertEqual(market_job["status"], "CANCELLED_EVENT_REJECTED")
        self.assertIn("semantic_gate", market_job["last_error"])
        facts = json.loads(version["facts_json"])
        self.assertEqual(facts["discovery_filter"]["automatic_impacts_deactivated"], 1)
        self.assertEqual(facts["discovery_filter"]["unfinished_market_jobs_cancelled"], 1)

    def test_mixed_cluster_filters_noise_observation_but_keeps_valid_event(self) -> None:
        now = utc_now()
        valid_title = "Strait of Hormuz blockade disrupts oil shipping"
        noise_title = "Live: Officials testify about the war with Iran"
        for external_id, title in (("mixed-valid", valid_title), ("mixed-noise", noise_title)):
            self.add_observation(
                external_id,
                title,
                f"https://example.test/{external_id}",
                raw_json=json.dumps({"item": {"title": title, "coins": ["BTC"]}}),
            )

        self.connection.execute(
            """INSERT INTO canonical_events VALUES (
               'mixed-event',1,'candidate','candidate','geopolitical','conflict_or_blockade',
               '2026-07-15',?,?,NULL,NULL,NULL,NULL,'B_P2_discovery_only','opennews_free',1)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO event_versions VALUES (
               'mixed-event',1,?,'candidate','candidate','geopolitical','conflict_or_blockade',
               NULL,'{}','legacy_import')""",
            (now,),
        )
        for external_id in ("mixed-valid", "mixed-noise"):
            self.connection.execute(
                "INSERT INTO event_observations VALUES (?,?,?,?)",
                (
                    "mixed-event",
                    stable_id("OBS", "opennews_free", external_id),
                    "aggregated_discovery_candidate",
                    now,
                ),
            )
        self.connection.execute(
            """INSERT INTO pipeline_jobs VALUES (
               'mixed-job','mixed-event','live_primary_evidence_review',
               'PENDING_PRIMARY_EVIDENCE',50,0,?,NULL,'{}',?,?)""",
            (now, now, now),
        )
        self.connection.commit()

        retracted = extractor.retract_filtered_opennews_candidates(self.connection)

        event = self.connection.execute(
            "SELECT status FROM canonical_events WHERE event_id='mixed-event'"
        ).fetchone()
        relations = {
            row["external_id"]: row["relation_type"]
            for row in self.connection.execute(
                """SELECT r.external_id,eo.relation_type
                   FROM event_observations eo JOIN raw_observations r
                     ON r.observation_id=eo.observation_id
                   WHERE eo.event_id='mixed-event'"""
            ).fetchall()
        }
        self.assertEqual(retracted, 0)
        self.assertEqual(event["status"], "candidate")
        self.assertEqual(relations["mixed-valid"], "aggregated_discovery_candidate")
        self.assertEqual(relations["mixed-noise"], "filtered_aggregated_noise")

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
        self.assertEqual(result["no_candidate"], 3)
        self.assertEqual(result["source_shape_filtered"], 1)

    def test_multi_topic_nvidia_bitcoin_digest_stays_out_of_event_ledger(self) -> None:
        title = (
            "Nvidia shares jump after another earnings beat, bitcoin runs into a "
            "major wall of supply around $80,000, and AI-generated security reports "
            "put Bitcoin's Lightning Network on alert"
        )
        self.add_observation(
            "nvidia-bitcoin-digest",
            title,
            "https://example.test/nvidia-bitcoin-digest",
            raw_json=json.dumps(
                {"item": {"title": title, "coins": ["BTC"]}}
            ),
        )

        result = extractor.process_pending(self.connection, limit=10)

        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["source_shape_filtered"], 1)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0],
            0,
        )
        job = self.connection.execute(
            "SELECT status,last_error FROM observation_jobs"
        ).fetchone()
        self.assertEqual(job["status"], "COMPLETED_SCOPE_FILTERED")
        self.assertIn("multi_topic_digest", job["last_error"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0],
            1,
        )

    def test_multi_topic_asian_market_digest_stays_out_of_event_ledger(self) -> None:
        title = (
            "Asian stocks edge lower at the start of a key week. MSCI Asia falls "
            "0.1% and South Korea's KOSPI drops 1.2%. Alibaba announces an HK$80 "
            "billion share issue, while investors await Nvidia earnings and a "
            "Federal Reserve speech."
        )
        self.add_observation(
            "asian-market-digest",
            title,
            "https://example.test/asian-market-digest",
            raw_json=json.dumps(
                {"item": {"title": title, "coins": ["NVDA"]}}
            ),
        )

        result = extractor.process_pending(self.connection, limit=10)

        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["source_shape_filtered"], 1)
        job = self.connection.execute(
            "SELECT status,last_error FROM observation_jobs"
        ).fetchone()
        self.assertEqual(job["status"], "COMPLETED_SCOPE_FILTERED")
        self.assertIn("multi_topic_digest", job["last_error"])

    def test_causal_cross_clause_macro_story_remains_one_atomic_event(self) -> None:
        title = "Federal Reserve raises interest rates, sending Bitcoin lower"
        self.add_observation(
            "fed-rates-bitcoin",
            title,
            "https://example.test/fed-rates-bitcoin",
            raw_json=json.dumps(
                {"item": {"title": title, "coins": ["BTC"]}}
            ),
        )

        result = extractor.process_pending(self.connection, limit=10)

        self.assertEqual(result["candidates"], 1)
        event = self.connection.execute(
            "SELECT event_type,company_name,ticker_at_event FROM canonical_events"
        ).fetchone()
        self.assertEqual(event["event_type"], "monetary_policy")
        self.assertEqual(event["company_name"], "Federal Reserve")
        self.assertIsNone(event["ticker_at_event"])
        facts = json.loads(
            self.connection.execute("SELECT facts_json FROM event_versions").fetchone()[0]
        )
        self.assertEqual(facts["source_shape"], "SINGLE_EVENT")
        self.assertEqual(facts["affected_assets"], ["BTC"])
        self.assertEqual(facts["event_claim_text"], title)

    def test_live_roundup_ai_benchmark_hack_and_fed_name_collision_are_scope_filtered(self) -> None:
        fixtures = (
            (
                "iran-live",
                "Live: Officials testify about the US war with Iran. Here's what happened today",
                ["BTC", "CL"],
            ),
            (
                "ai-benchmark",
                "OpenAI Models Escaped Locked Test Environment, Hacked Hugging Face to Cheat on Benchmark",
                ["WLD", "OPENAI"],
            ),
            (
                "fed-name",
                "The Fed rang the alarm about Anthropic's Mythos AI model",
                ["XYZ-SP500"],
            ),
        )
        for external_id, title, coins in fixtures:
            self.add_observation(
                external_id,
                title,
                f"https://example.test/{external_id}",
                raw_json=json.dumps({"item": {"title": title, "coins": coins}}),
            )
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["scope_filtered"], 3)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0],
            0,
        )

    def test_specific_policy_action_and_exchange_hack_remain_admitted(self) -> None:
        fixtures = (
            (
                "rate-action",
                "South Korea raises interest rates after an inflation surprise",
                "https://example.test/rate-action",
            ),
            (
                "exchange-hack",
                "Watchdog begins sanctions against Upbit operator over last November's hack",
                "https://example.test/exchange-hack",
            ),
        )
        for external_id, title, url in fixtures:
            self.add_observation(
                external_id,
                title,
                url,
                raw_json=json.dumps(
                    {"item": {"title": title, "coins": [], "company": "Fixture Subject"}}
                ),
            )
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["scope_filtered"], 0)

    def test_gold_market_commentary_is_not_a_monetary_policy_event(self) -> None:
        title = (
            "Middle East tensions lift risk aversion; Brent tops $91 and GOLD nears "
            "4340 as markets await Federal Reserve meeting minutes for rate clues"
        )
        self.add_observation(
            "gold-awaiting-fed-minutes",
            title,
            "https://x.com/FirstSquawk/status/2089870834508927268",
            raw_json=json.dumps(
                {
                    "item": {
                        "title": title,
                        "coins": ["XAU", "XYZ-GOLD"],
                        "company": "GOLD",
                        "score": 90,
                        "grade": "A+",
                        "signal": "long",
                    }
                }
            ),
        )

        result = extractor.process_pending(self.connection, limit=10)

        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["scope_filtered"], 1)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0],
            0,
        )
        row = self.connection.execute("SELECT * FROM latest_source_content").fetchone()
        self.assertEqual(
            extractor.extract_canonical_subject(
                row, extractor.RULE_BY_TYPE["monetary_policy"]
            ),
            ("Federal Reserve", None),
        )

    def test_macro_actor_is_subject_and_unmentioned_provider_coin_is_not_an_asset(self) -> None:
        title = "Federal Reserve released meeting minutes and kept policy rates unchanged"
        self.add_observation(
            "fed-released-minutes",
            title,
            "https://example.test/fed-minutes",
            raw_json=json.dumps(
                {"item": {"title": title, "coins": ["XYZ-GOLD"]}}
            ),
        )

        result = extractor.process_pending(self.connection, limit=10)

        self.assertEqual(result["candidates"], 1)
        event = self.connection.execute(
            "SELECT company_name,ticker_at_event FROM canonical_events"
        ).fetchone()
        self.assertEqual(event["company_name"], "Federal Reserve")
        self.assertIsNone(event["ticker_at_event"])
        facts = json.loads(
            self.connection.execute("SELECT facts_json FROM event_versions").fetchone()[0]
        )
        self.assertNotIn("affected_assets", facts)
        self.assertNotIn("signal", facts)
        self.assertNotIn("grade", facts)

    def test_opennews_provider_summary_can_supply_missing_action(self) -> None:
        self.add_observation(
            "summary-only-bankruptcy",
            "Example Corp corporate update",
            "https://example.test/example-update",
            raw_json=json.dumps(
                {
                    "item": {
                        "title": "Example Corp corporate update",
                        "summary_en": (
                            "Example Corp filed a voluntary Chapter 11 bankruptcy petition."
                        ),
                        "company": "Example Corp",
                    }
                }
            ),
        )

        result = extractor.process_pending(self.connection, limit=10)

        self.assertEqual(result["candidates"], 1)
        event = self.connection.execute(
            "SELECT event_type,company_name FROM canonical_events"
        ).fetchone()
        self.assertEqual(event["event_type"], "bankruptcy")
        self.assertEqual(event["company_name"], "Example Corp")

    def test_provider_asset_tag_requires_story_level_identity(self) -> None:
        unrelated = "Coinbase reports a security breach affecting customer records"
        self.add_observation(
            "unrelated-tag",
            unrelated,
            "https://example.test/coinbase",
            raw_json=json.dumps({"item": {"title": unrelated, "coins": ["BTC"]}}),
        )
        direct = "Bitcoin exchange wallet hacked and BTC funds stolen"
        self.add_observation(
            "direct-tag",
            direct,
            "https://example.test/bitcoin",
            raw_json=json.dumps({"item": {"title": direct, "coins": ["BTC"]}}),
        )
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 2)
        tickers = {
            row["ticker_at_event"]
            for row in self.connection.execute(
                "SELECT ticker_at_event FROM canonical_events"
            ).fetchall()
        }
        self.assertEqual(tickers, {None, "BTC"})

    def test_provider_asset_tag_does_not_match_ordinary_prose_words(self) -> None:
        fixtures = (
            ("RED", "Tanker attacked as tensions rise in the Red Sea"),
            ("BRIDGE", "Wanchain Cardano bridge reportedly exploited"),
            ("STEEL", "Oil prices rise after a shipping blockade"),
        )
        for symbol, title in fixtures:
            raw_json = json.dumps({"item": {"title": title, "coins": [symbol]}})
            self.assertIsNone(extractor.extract_symbol(raw_json, title))

    def test_existing_unsubstantiated_provider_asset_tag_is_repaired_with_audit_version(self) -> None:
        title = "SK Hynix shares jump after a quarterly earnings surprise"
        self.add_observation(
            "legacy-skhy-tag",
            title,
            "https://example.test/legacy-skhy-tag",
            raw_json=json.dumps({"item": {"title": title, "coins": ["XYZ-SKHY"]}}),
        )
        observation_id = stable_id("OBS", "opennews_free", "legacy-skhy-tag")
        now = utc_now()
        self.connection.execute(
            """INSERT INTO canonical_events VALUES (
               'legacy-skhy-event',1,'candidate','candidate','earnings','earnings_or_guidance',
               '2026-07-15',?,?,NULL,'SKHY',NULL,NULL,'B_P2_discovery_only','opennews_free',1)""",
            (now, now),
        )
        self.connection.execute(
            """INSERT INTO event_versions VALUES (
               'legacy-skhy-event',1,?,'candidate','candidate','earnings','earnings_or_guidance',
               NULL,'{}','legacy_import')""",
            (now,),
        )
        self.connection.execute(
            "INSERT INTO event_observations VALUES (?,?,?,?)",
            ("legacy-skhy-event", observation_id, "aggregated_discovery_candidate", now),
        )
        self.connection.execute(
            """INSERT INTO pipeline_jobs VALUES (
               'legacy-skhy-job','legacy-skhy-event','live_primary_evidence_review',
               'PENDING_PRIMARY_EVIDENCE',50,0,?,NULL,'{}',?,?)""",
            (now, now, now),
        )
        self.connection.commit()

        repaired = extractor.repair_opennews_asset_tags(self.connection)

        event = self.connection.execute(
            "SELECT current_version,ticker_at_event,status FROM canonical_events "
            "WHERE event_id='legacy-skhy-event'"
        ).fetchone()
        version = self.connection.execute(
            "SELECT facts_json,change_reason FROM event_versions "
            "WHERE event_id='legacy-skhy-event' AND version=2"
        ).fetchone()
        self.assertEqual(repaired, 1)
        self.assertEqual(event["current_version"], 2)
        self.assertIsNone(event["ticker_at_event"])
        self.assertEqual(event["status"], "candidate")
        self.assertIn('"previous_provider_tag":"SKHY"', version["facts_json"])
        self.assertIn('"validated_symbol":null', version["facts_json"])
        self.assertEqual(version["change_reason"], "opennews_asset_tag_story_validation")

    def test_legacy_same_day_entity_duplicates_are_clustered_without_losing_observations(self) -> None:
        now = utc_now()
        stories = (
            (
                "legacy-sk-a",
                "SK Hynix shares jump after earnings beat",
                "legacy-sk-event-a",
                "legacy-sk-job-a",
            ),
            (
                "legacy-sk-b",
                "SK 海力士公布强劲季度业绩，股价上涨",
                "legacy-sk-event-b",
                "legacy-sk-job-b",
            ),
        )
        for external_id, title, event_id, job_id in stories:
            self.add_observation(
                external_id,
                title,
                f"https://example.test/{external_id}",
                raw_json=json.dumps({"item": {"title": title, "coins": []}}),
            )
            observation_id = stable_id("OBS", "opennews_free", external_id)
            self.connection.execute(
                """INSERT INTO canonical_events VALUES (
                   ?,1,'candidate','candidate','earnings','earnings_or_guidance',
                   '2026-07-15',?,?,NULL,NULL,NULL,NULL,'B_P2_discovery_only','opennews_free',1)""",
                (event_id, now, now),
            )
            self.connection.execute(
                """INSERT INTO event_versions VALUES (
                   ?,1,?,'candidate','candidate','earnings','earnings_or_guidance',
                   NULL,'{}','legacy_import')""",
                (event_id, now),
            )
            self.connection.execute(
                "INSERT INTO event_observations VALUES (?,?,?,?)",
                (event_id, observation_id, "aggregated_discovery_candidate", now),
            )
            self.connection.execute(
                """INSERT INTO pipeline_jobs VALUES (
                   ?,?,'live_primary_evidence_review','PENDING_PRIMARY_EVIDENCE',
                   50,0,?,NULL,'{}',?,?)""",
                (job_id, event_id, now, now, now),
            )
        self.connection.commit()

        reconciled = extractor.reconcile_opennews_candidate_duplicates(self.connection)

        active = self.connection.execute(
            "SELECT event_id FROM canonical_events WHERE status='candidate'"
        ).fetchall()
        rejected = self.connection.execute(
            "SELECT event_id,current_version FROM canonical_events WHERE status='rejected'"
        ).fetchone()
        support_count = self.connection.execute(
            """SELECT COUNT(*) FROM event_observations
               WHERE event_id=? AND relation_type='aggregated_duplicate_support'""",
            (active[0]["event_id"],),
        ).fetchone()[0]
        duplicate_job = self.connection.execute(
            "SELECT status,last_error FROM pipeline_jobs WHERE event_id=?",
            (rejected["event_id"],),
        ).fetchone()
        self.assertEqual(reconciled, 1)
        self.assertEqual(len(active), 1)
        self.assertEqual(rejected["current_version"], 2)
        self.assertEqual(support_count, 1)
        self.assertEqual(duplicate_job["status"], "COMPLETED_DUPLICATE_CLUSTER")
        self.assertIn(active[0]["event_id"], duplicate_job["last_error"])

    def test_p2_backpressure_does_not_block_p0(self) -> None:
        upsert_source(
            self.connection,
            source_id="sec_litigation_releases",
            name="SEC",
            source_type="official_primary_feed",
            authority_tier="P0_official",
        )
        self.add_observation(
            "p2-a",
            "Example One files for Chapter 11 bankruptcy",
            "https://example.test/p2-a",
        )
        self.add_observation(
            "p2-b",
            "Example Two files for Chapter 11 bankruptcy",
            "https://example.test/p2-b",
        )
        self.add_observation(
            "p0-sec",
            "SEC charges Example Three with accounting fraud",
            "https://www.sec.gov/litigation/p0-sec",
            source_id="sec_litigation_releases",
        )
        result = extractor.process_pending(
            self.connection,
            limit=10,
            p2_pending_cap=1,
            p2_cycle_cap=1,
        )
        self.assertEqual(result["candidates"], 2)
        self.assertEqual(result["backpressure_filtered"], 1)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM canonical_events WHERE discovery_source='sec_litigation_releases'"
            ).fetchone()[0],
            1,
        )

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

    def test_sec_official_filing_stays_a_discovery_lead_until_document_admission(self) -> None:
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
        self.assertEqual(result["candidates"], 0)
        self.assertEqual(result["discovery_leads"], 1)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM canonical_events").fetchone()[0],
            0,
        )
        lead = self.connection.execute("SELECT * FROM discovery_leads").fetchone()
        self.assertEqual(lead["status"], "PENDING_ENRICHMENT")
        self.assertEqual(lead["company_name"], "Example Corp")
        self.assertEqual(lead["proposed_event_type"], "earnings_or_guidance")
        self.assertIsNone(lead["canonical_event_id"])
        observation_job = self.connection.execute(
            """SELECT j.status,j.last_error
               FROM observation_jobs j
               JOIN raw_observations r ON r.observation_id=j.observation_id
               WHERE r.external_id='sec-one'"""
        ).fetchone()
        self.assertEqual(observation_job["status"], "COMPLETED_DISCOVERY_LEAD")
        self.assertEqual(observation_job["last_error"], "sec_parse_before_canonical")

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
            raw_json="{}",
        )
        self.add_observation(
            "fed-two",
            "Federal Reserve announces second monetary policy action",
            "https://www.federalreserve.gov/release-two.htm",
            source_id="federal_reserve_press",
            raw_json="{}",
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
