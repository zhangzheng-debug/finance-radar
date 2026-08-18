from __future__ import annotations

import json
import inspect
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.api.main import create_app
from app.config import Settings
from app.models import RiskRouter
from app.ops.backup import create_and_verify, create_weekly_snapshot, verify_restore
from app.ops.backup import prune_backups
from app.workers.continuous import execute_cycle
from app.services import ReplayService
from app.storage import LedgerRepository, OperationsRepository
from app.storage import EvidenceObjectStore
from event_ledger import open_ledger, utc_now


class ProductLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.ledger_path = root / "ledger.sqlite3"
        connection = open_ledger(self.ledger_path)
        now = utc_now()
        connection.execute(
            "INSERT INTO sources VALUES ('sec','SEC','official_primary','P0',1,1,?,?)",
            (now, now),
        )
        connection.execute(
            """INSERT INTO raw_observations VALUES (
               'obs-1','sec','filing-1','2026-07-17',?,'Bankruptcy filing','Chapter 11 petition',
               'https://sec.example/filing','hash','{}','captured')""",
            (now,),
        )
        connection.execute(
            """INSERT INTO canonical_events VALUES (
               'evt-1',1,'verified','verified','bankruptcy_or_distress','chapter_11','2026-07-17',
               ?,?,'stable-1','TST','Test Company','A++','A++','test',1)""",
            (now, now),
        )
        connection.execute(
            """INSERT INTO event_versions VALUES (
               'evt-1',1,?,'verified','verified','bankruptcy_or_distress','chapter_11','A++',?,
               'fixture created')""",
            (now, json.dumps({"evidence_summary": "The issuer filed a voluntary Chapter 11 petition."})),
        )
        connection.execute(
            "INSERT INTO event_observations VALUES ('evt-1','obs-1','primary',?)",
            (now,),
        )
        connection.execute(
            """INSERT INTO event_evidence VALUES (
               'evidence-1','evt-1','obs-1','https://sec.example/filing','2026-07-17','8-K','1.03',
               'The issuer and subsidiaries filed voluntary Chapter 11 petitions.','chapter 11',10,
               'confirmed',0,?,?)""",
            (now, now),
        )
        connection.commit()
        connection.close()
        # Complete recovery bundles preserve these two roots even when no
        # object/report has been written yet.
        (root / "evidence_objects").mkdir()
        (root / "reports").mkdir()
        self.settings = Settings(
            ledger_db=self.ledger_path,
            operations_db=root / "ops.sqlite3",
            artifact_dir=root / "artifacts",
            evidence_object_dir=root / "evidence_objects",
            replay_dir=ROOT / "replay" / "cases",
            demo_mode="RECENT_CAPTURE",
            admin_token="test-secret",
            api_base_url="http://testserver",
            web_base_url="http://testserver",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_repository_exposes_evidence_linked_event(self) -> None:
        repository = LedgerRepository(self.ledger_path)
        health = repository.health()
        self.assertEqual(health["schema_version"], 12)
        self.assertEqual(health["audit"]["trading_boundary_violations"], 0)
        detail = repository.event_detail("evt-1")
        self.assertEqual(detail["event"]["no_trading"], 1)
        self.assertEqual(len(repository.event_evidence("evt-1")), 1)
        self.assertGreaterEqual(len(repository.event_timeline("evt-1")), 2)
        facets = repository.event_facets()
        self.assertEqual(facets["families"], [{"value": "bankruptcy_or_distress", "count": 1}])
        self.assertEqual(facets["sources"], [{"value": "test", "count": 1}])
        self.assertTrue(facets["read_only"])
        self.assertEqual(repository.list_events(source="test")["total"], 1)
        self.assertEqual(repository.list_events(source="missing")["total"], 0)
        self.assertEqual(repository.list_events(query="test")["total"], 1)
        listed = repository.list_events(query="Chapter 11 petition")
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["items"][0]["source_title"], "Bankruptcy filing")
        self.assertEqual(listed["items"][0]["source_summary"], "Chapter 11 petition")
        source = repository.list_source_health()[0]
        self.assertEqual(source["observations"], 1)
        self.assertEqual(source["cursor_status"], "STATIC_IMPORTED")
        self.assertIsNotNone(source["last_success_at"])

    def test_product_metrics_report_samples_and_unavailable_states_honestly(self) -> None:
        report = LedgerRepository(self.ledger_path).product_metrics(window_days=30)
        metrics = {item["id"]: item for item in report["metrics"]}
        self.assertEqual(report["window"]["days"], 30)
        self.assertTrue(report["engineering_health_is_not_product_quality"])
        self.assertEqual(metrics["citable_evidence_coverage"]["status"], "MEASURED")
        self.assertEqual(metrics["citable_evidence_coverage"]["value"], 100.0)
        self.assertEqual(metrics["evidence_closure_rate"]["value"], 100.0)
        self.assertEqual(metrics["boundary_violations"]["value"], 0.0)
        self.assertEqual(metrics["formal_conclusion_accuracy"]["status"], "UNAVAILABLE")
        self.assertIn("human", metrics["formal_conclusion_accuracy"]["source"])

        with TestClient(create_app(self.settings)) as client:
            response = client.get("/api/v1/product/metrics?window_days=30")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["window"]["days"], 30)

    def test_overview_separates_rough_reviewed_from_pending_review(self) -> None:
        connection = open_ledger(self.ledger_path)
        now = utc_now()
        connection.execute(
            "UPDATE canonical_events SET status='candidate',label_status='candidate' WHERE event_id='evt-1'"
        )
        connection.execute(
            """INSERT INTO pipeline_jobs VALUES (
               'review-job','evt-1','live_primary_evidence_review','PENDING_HUMAN_REVIEW',
               50,0,?,NULL,'{}',?,?)""",
            (now, now, now),
        )
        connection.commit()
        connection.close()

        repository = LedgerRepository(self.ledger_path)
        pending = repository.overview(run_integrity_check=False)
        self.assertEqual(pending["review_queue"], 1)
        self.assertEqual(pending["rough_reviewed"], 0)

        connection = open_ledger(self.ledger_path)
        connection.execute(
            "UPDATE pipeline_jobs SET status='COMPLETED_AUTHORIZED_ROUGH_REVIEW' WHERE job_id='review-job'"
        )
        connection.commit()
        connection.close()
        completed = repository.overview(run_integrity_check=False)
        self.assertEqual(completed["review_queue"], 0)
        self.assertEqual(completed["rough_reviewed"], 1)

    def test_filtered_observation_is_auditable_but_not_used_as_preferred_source(self) -> None:
        connection = open_ledger(self.ledger_path)
        now = utc_now()
        connection.execute(
            """INSERT INTO raw_observations VALUES (
               'obs-noise','sec','noise-1','2026-07-17',?,'Live roundup noise',
               'unrelated commentary','https://sec.example/noise','noise-hash','{}','captured')""",
            (now,),
        )
        connection.execute(
            """INSERT INTO event_observations VALUES (
               'evt-1','obs-noise','filtered_aggregated_noise',?)""",
            (now,),
        )
        connection.commit()
        connection.close()

        repository = LedgerRepository(self.ledger_path)
        listed = repository.list_events(status="verified")
        self.assertEqual(listed["items"][0]["source_title"], "Bankruptcy filing")
        self.assertEqual(repository.list_events(query="roundup noise")["total"], 0)
        detail = repository.event_detail("evt-1")
        self.assertEqual(detail["preferred_source"]["title"], "Bankruptcy filing")
        timeline = repository.event_timeline("evt-1")
        self.assertTrue(
            any(
                item["kind"] == "observation"
                and item["payload"].get("relation_type") == "filtered_aggregated_noise"
                for item in timeline
            )
        )

    def test_market_capabilities_distinguish_persisted_providers_and_local_probe(self) -> None:
        connection = open_ledger(self.ledger_path)
        now = utc_now()
        connection.execute(
            """INSERT INTO assets VALUES (
               'asset-crypto','crypto','ETH','ETH/USD','registry','USD','{}',?,?)""",
            (now, now),
        )
        connection.execute(
            """INSERT INTO market_jobs VALUES (
               'job-crypto','evt-1','asset-crypto','binance_public','initial','COMPLETED',?,?,1,NULL,1)""",
            (now, now),
        )
        connection.execute(
            """INSERT INTO market_snapshots VALUES (
               'snap-crypto','job-crypto','evt-1','asset-crypto','binance_public','ETHUSDT',
               'latest_public_spot_price','2000','USDT',NULL,?,
               'provider_timestamp_unavailable','{}',1,1)""",
            (now,),
        )
        connection.commit()
        connection.close()

        capabilities = LedgerRepository(self.ledger_path).market_capabilities()
        providers = {item["provider_id"]: item for item in capabilities["providers"]}
        self.assertEqual(providers["binance_public"]["status"], "OBSERVED")
        self.assertEqual(providers["binance_public"]["snapshots"], 1)
        self.assertEqual(providers["binance_public"]["freshness_status"], "FRESH_CAPTURE")
        self.assertFalse(providers["binance_public"]["continuous_feed"])
        self.assertEqual(
            providers["binance_public"]["activity_scope"],
            "EVENT_TRIGGERED_SNAPSHOTS",
        )
        self.assertEqual(providers["ibkr_tws_readonly"]["status"], "LOCAL_PROBE_ONLY")
        self.assertEqual(
            providers["ibkr_tws_readonly"]["freshness_status"],
            "NOT_APPLICABLE_LOCAL_PROBE",
        )
        self.assertTrue(capabilities["boundary"]["no_trading"])
        self.assertTrue(all(not item["order_endpoints_present"] for item in providers.values()))
        self.assertEqual(
            capabilities["horizon_policy"]["windows"],
            ["t_plus_5m", "t_plus_30m", "t_plus_1d"],
        )
        self.assertFalse(capabilities["horizon_policy"]["continuous_quote_feed"])

    def test_api_contract_and_admin_boundary(self) -> None:
        with TestClient(create_app(self.settings)) as client:
            live = client.get("/api/v1/live")
            self.assertEqual(live.status_code, 200)
            self.assertEqual(live.json()["data"]["status"], "ok")
            self.assertEqual(live.json()["data"]["database_checks"], "not_run")
            health = client.get("/api/v1/health")
            self.assertEqual(health.status_code, 200)
            payload = health.json()
            self.assertIn("schema_version", payload)
            self.assertIn("trace_id", payload)
            self.assertIn("generated_at", payload)
            self.assertEqual(payload["data"]["ledger"]["counts"]["canonical_events"], 1)
            self.assertEqual(payload["data"]["ledger"]["database"], self.ledger_path.name)
            self.assertEqual(payload["data"]["operations"]["database"], self.settings.operations_db.name)
            self.assertIn("raw_official_source_snapshots", payload["data"]["capabilities"])
            self.assertEqual(payload["data"]["operations"]["worker_window_24h"]["status"], "NO_DATA")
            overview = client.get("/api/v1/overview")
            self.assertEqual(overview.status_code, 200)
            self.assertGreaterEqual(overview.json()["data"]["timing"]["latest_event_age_seconds"], 0)
            self.assertIsNone(overview.json()["data"]["timing"]["worker_cycle_duration_seconds"])
            market = client.get("/api/v1/market/capabilities")
            self.assertEqual(market.status_code, 200)
            self.assertEqual(len(market.json()["data"]["providers"]), 3)
            self.assertTrue(market.json()["data"]["boundary"]["read_only"])
            archive = client.get(
                "/api/v1/evidence/archive",
                headers={"X-Admin-Token": "test-secret"},
            )
            self.assertEqual(archive.status_code, 200)
            self.assertTrue(archive.json()["data"]["policy"]["immutable"])
            self.assertIn("coverage", archive.json()["data"])
            self.assertEqual(archive.json()["data"]["coverage"]["missing_links"], 0)
            self.assertEqual(archive.json()["data"]["coverage"]["coverage_pct"], 100.0)
            self.assertEqual(archive.json()["data"]["coverage"]["terminal_policy_exclusions"], 0)
            facets = client.get("/api/v1/events/facets")
            self.assertEqual(facets.status_code, 200)
            self.assertTrue(facets.json()["data"]["read_only"])
            filtered = client.get("/api/v1/events", params={"source": "test"})
            self.assertEqual(filtered.status_code, 200)
            self.assertEqual(filtered.json()["data"]["total"], 1)
            detail = client.get("/api/v1/events/evt-1")
            self.assertEqual(detail.status_code, 200)
            self.assertTrue(detail.json()["data"]["model_shadow_output"]["no_trading"])
            self.assertTrue(
                detail.json()["data"]["model_input_contract"]["excludes_event_taxonomy_shortcuts"]
            )
            self.assertEqual(
                detail.json()["data"]["model_shadow_output"]["scope_gate"]["decision"],
                "ADMIT_RISK_SCOPE",
            )
            denied = client.post("/api/v1/replays/positive_earnings_non_target/run")
            self.assertEqual(denied.status_code, 403)
            allowed = client.post(
                "/api/v1/replays/positive_earnings_non_target/run",
                headers={"X-Admin-Token": "test-secret"},
            )
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(allowed.json()["data"]["final_label"], "NON_TARGET")

    def test_mutations_fail_closed_when_admin_token_is_not_configured(self) -> None:
        settings = replace(self.settings, admin_token=None)
        with TestClient(create_app(settings)) as client:
            response = client.post("/api/v1/replays/positive_earnings_non_target/run")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "OPERATOR_MUTATIONS_DISABLED")

    def test_reviewer_and_operator_tokens_enforce_distinct_capabilities(self) -> None:
        settings = replace(
            self.settings,
            reviewer_token="review-secret",
            operator_token="operate-secret",
        )
        with TestClient(create_app(settings)) as client:
            reviewer_headers = {"X-Reviewer-Token": "review-secret"}
            operator_headers = {"X-Operator-Token": "operate-secret"}

            self.assertEqual(
                client.get("/api/v1/events/evt-1/trace", headers=reviewer_headers).status_code,
                200,
            )
            self.assertEqual(
                client.get("/api/v1/adjudication/status", headers=reviewer_headers).status_code,
                200,
            )
            self.assertEqual(
                client.get("/api/v1/model/status", headers=reviewer_headers).status_code,
                403,
            )

            self.assertEqual(
                client.get("/api/v1/model/status", headers=operator_headers).status_code,
                200,
            )
            self.assertEqual(
                client.get("/api/v1/evidence/archive", headers=operator_headers).status_code,
                200,
            )
            self.assertEqual(
                client.get("/api/v1/events/evt-1/trace", headers=operator_headers).status_code,
                403,
            )

    def test_structured_evidence_agent_persists_trace_objects_and_override(self) -> None:
        with TestClient(create_app(self.settings)) as client:
            denied = client.post("/api/v1/events/evt-1/agent/run")
            self.assertEqual(denied.status_code, 403)
            run = client.post(
                "/api/v1/events/evt-1/agent/run",
                headers={"X-Admin-Token": "test-secret"},
                json={"audit_write_confirmed": True, "evidence_change_confirmed": True},
            )
            self.assertEqual(run.status_code, 200)
            result = run.json()["data"]
            self.assertEqual(result["status"], "EVIDENCE_READY")
            self.assertFalse(result["llm_used"])
            self.assertEqual(result["model_provider"], "deterministic_guarded_fallback")
            self.assertEqual(
                result["audit_write_confirmation"],
                {"confirmed": True, "evidence_change_confirmed": True},
            )
            self.assertTrue(result["guardrails"]["structured_output"])
            self.assertFalse(result["guardrails"]["model_can_assign_final_s"])
            self.assertEqual(len(result["claims"]), 1)
            self.assertEqual(len(result["evidence_edges"]), 1)
            object_hash = result["evidence_edges"][0]["object_sha256"]
            object_path = result["evidence_edges"][0]["object_path"]
            self.assertTrue(EvidenceObjectStore(self.settings.evidence_object_dir).verify(object_path, object_hash))

            override = client.post(
                "/api/v1/events/evt-1/human-override",
                headers={"X-Admin-Token": "test-secret"},
                json={
                    "actor": "student-reviewer",
                    "reason": "Verified SEC 8-K Item 1.03 passage against the incident summary.",
                    "review_status": "REVIEWED_NO_CHANGE",
                    "reviewer_attestation": True,
                },
            )
            self.assertEqual(override.status_code, 200)
            self.assertTrue(override.json()["data"]["no_trading"])
            trace = client.get(
                "/api/v1/events/evt-1/trace",
                headers={"X-Admin-Token": "test-secret"},
            ).json()["data"]
            self.assertEqual(len(trace["agent_decisions"]), 1)
            self.assertEqual(len(trace["human_overrides"]), 1)
            self.assertEqual(len(trace["evidence_objects"]), 1)
            self.assertEqual(trace["evidence_objects"][0]["object_kind"], "EXACT_EXCERPT")
            self.assertTrue(trace["evidence_objects"][0]["integrity_verified"])
            archive = client.get(
                "/api/v1/evidence/archive",
                headers={"X-Admin-Token": "test-secret"},
            ).json()["data"]
            self.assertEqual(archive["exact_excerpts"], 1)
            self.assertEqual(archive["source_snapshots"], 0)
            self.assertEqual(archive["integrity_failures_in_recent_sample"], 0)

    def test_human_override_requires_agent_decision(self) -> None:
        with TestClient(create_app(self.settings)) as client:
            response = client.post(
                "/api/v1/events/evt-1/human-override",
                headers={"X-Admin-Token": "test-secret"},
                json={
                    "actor": "student-reviewer",
                    "reason": "No agent decision exists yet",
                    "review_status": "HUMAN_REVIEW",
                    "reviewer_attestation": True,
                },
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"]["code"], "AGENT_DECISION_REQUIRED")

    def test_human_override_rejects_agent_decision_after_evidence_changes(self) -> None:
        with TestClient(create_app(self.settings)) as client:
            run = client.post(
                "/api/v1/events/evt-1/agent/run",
                headers={"X-Admin-Token": "test-secret"},
                json={"audit_write_confirmed": True, "evidence_change_confirmed": True},
            )
            self.assertEqual(run.status_code, 200)

            with closing(sqlite3.connect(self.ledger_path)) as connection:
                connection.execute(
                    """UPDATE event_evidence
                       SET evidence_passage=evidence_passage || ' Material revision.'
                       WHERE event_id='evt-1'"""
                )
                connection.commit()

            response = client.post(
                "/api/v1/events/evt-1/human-override",
                headers={"X-Admin-Token": "test-secret"},
                json={
                    "actor": "student-reviewer",
                    "reason": "Reviewed the revised SEC passage against the current incident summary.",
                    "review_status": "HUMAN_REVIEW",
                    "reviewer_attestation": True,
                },
            )

            self.assertEqual(response.status_code, 409)
            error = response.json()["error"]
            self.assertEqual(error["code"], "STALE_AGENT_DECISION")
            self.assertFalse(error["details"]["receipt_matches"])
            self.assertEqual(
                error["details"]["decision_event_version"],
                error["details"]["current_event_version"],
            )
            trace = client.get(
                "/api/v1/events/evt-1/trace",
                headers={"X-Admin-Token": "test-secret"},
            ).json()["data"]
            self.assertEqual(trace["human_overrides"], [])

    def test_evidence_agent_requires_explicit_audit_write_confirmation(self) -> None:
        with TestClient(create_app(self.settings)) as client:
            response = client.post(
                "/api/v1/events/evt-1/agent/run",
                headers={"X-Admin-Token": "test-secret"},
            )
            self.assertEqual(response.status_code, 422)

    def test_closed_event_agent_run_requires_evidence_change_confirmation(self) -> None:
        with closing(sqlite3.connect(self.ledger_path)) as connection:
            connection.execute("UPDATE canonical_events SET status='verified' WHERE event_id='evt-1'")
            connection.commit()
        with TestClient(create_app(self.settings)) as client:
            response = client.post(
                "/api/v1/events/evt-1/agent/run",
                headers={"X-Admin-Token": "test-secret"},
                json={"audit_write_confirmed": True},
            )
            self.assertEqual(response.status_code, 422)
            self.assertEqual(
                response.json()["error"]["code"],
                "EVIDENCE_CHANGE_CONFIRMATION_REQUIRED",
            )

    def test_human_override_rejects_placeholder_attribution(self) -> None:
        with TestClient(create_app(self.settings)) as client:
            response = client.post(
                "/api/v1/events/evt-1/human-override",
                headers={"X-Admin-Token": "test-secret"},
                json={
                    "actor": "defense-reviewer",
                    "reason": "Verified the exact primary-source passage",
                    "review_status": "REVIEWED_NO_CHANGE",
                    "reviewer_attestation": True,
                },
            )
            self.assertEqual(response.status_code, 422)
            self.assertEqual(
                response.json()["error"]["code"], "SPECIFIC_REVIEWER_ID_REQUIRED"
            )

    def test_human_override_rejects_whitespace_only_normalized_attribution(self) -> None:
        with TestClient(create_app(self.settings)) as client:
            blank_actor = client.post(
                "/api/v1/events/evt-1/human-override",
                headers={"X-Admin-Token": "test-secret"},
                json={
                    "actor": "   ",
                    "reason": "A reviewer checked the current evidence for this specific event.",
                    "review_status": "REVIEWED_NO_CHANGE",
                    "reviewer_attestation": True,
                },
            )
            self.assertEqual(blank_actor.status_code, 422)
            self.assertEqual(
                blank_actor.json()["error"]["code"], "SPECIFIC_REVIEWER_ID_REQUIRED"
            )

            blank_reason = client.post(
                "/api/v1/events/evt-1/human-override",
                headers={"X-Admin-Token": "test-secret"},
                json={
                    "actor": "student-reviewer",
                    "reason": "                    ",
                    "review_status": "REVIEWED_NO_CHANGE",
                    "reviewer_attestation": True,
                },
            )
            self.assertEqual(blank_reason.status_code, 422)
            self.assertEqual(
                blank_reason.json()["error"]["code"], "SPECIFIC_REVIEW_RATIONALE_REQUIRED"
            )

    def test_evidence_agent_forces_insufficient_when_exact_passage_is_missing(self) -> None:
        with closing(sqlite3.connect(self.ledger_path)) as connection:
            connection.execute("DELETE FROM event_evidence WHERE event_id='evt-1'")
            connection.commit()
        with TestClient(create_app(self.settings)) as client:
            response = client.post(
                "/api/v1/events/evt-1/agent/run",
                headers={"X-Admin-Token": "test-secret"},
                json={"audit_write_confirmed": True, "evidence_change_confirmed": True},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["status"], "INSUFFICIENT")
            self.assertEqual(response.json()["data"]["evidence_edges"], [])

    def test_evidence_agent_forces_human_review_on_contradiction(self) -> None:
        with closing(sqlite3.connect(self.ledger_path)) as connection:
            connection.execute(
                "UPDATE event_evidence SET evidence_status='contradicted_by_primary' WHERE event_id='evt-1'"
            )
            connection.commit()
        with TestClient(create_app(self.settings)) as client:
            response = client.post(
                "/api/v1/events/evt-1/agent/run",
                headers={"X-Admin-Token": "test-secret"},
                json={"audit_write_confirmed": True, "evidence_change_confirmed": True},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["status"], "HUMAN_REVIEW")
            self.assertEqual(response.json()["data"]["evidence_edges"][0]["relation"], "CONTRADICTS")

    def test_evidence_agent_never_accepts_the_canonical_conflicted_status(self) -> None:
        with closing(sqlite3.connect(self.ledger_path)) as connection:
            connection.execute(
                "UPDATE event_evidence SET evidence_status='conflicted' WHERE event_id='evt-1'"
            )
            connection.commit()
        with TestClient(create_app(self.settings)) as client:
            response = client.post(
                "/api/v1/events/evt-1/agent/run",
                headers={"X-Admin-Token": "test-secret"},
                json={"audit_write_confirmed": True, "evidence_change_confirmed": True},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["data"]["status"], "HUMAN_REVIEW")
            self.assertEqual(response.json()["data"]["evidence_edges"][0]["relation"], "CONTRADICTS")

    def test_api_has_no_trading_routes(self) -> None:
        application = create_app(self.settings)
        route_paths = {route.path.lower() for route in application.routes}
        for forbidden in ("orders", "positions", "balances", "brokerage", "trade_execution"):
            self.assertFalse(any(forbidden in path for path in route_paths), forbidden)

    def test_public_api_rate_limit_is_visible_and_enforced(self) -> None:
        settings = replace(self.settings, api_rate_limit_per_minute=2)
        headers = {"X-Forwarded-For": "203.0.113.7"}
        with TestClient(create_app(settings)) as client:
            first = client.get("/api/v1/health", headers=headers)
            second = client.get("/api/v1/health", headers=headers)
            blocked = client.get("/api/v1/health", headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["X-RateLimit-Limit"], "2")
        self.assertEqual(second.headers["X-RateLimit-Remaining"], "0")
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.headers["Retry-After"], "60")
        self.assertEqual(blocked.json()["error"]["code"], "RATE_LIMITED")

    def test_untrusted_forwarded_for_cannot_create_fresh_rate_buckets(self) -> None:
        settings = replace(self.settings, api_rate_limit_per_minute=2)
        application = create_app(settings)
        with TestClient(application, client=("198.51.100.20", 50000)) as client:
            first = client.get("/api/v1/health", headers={"X-Forwarded-For": "203.0.113.1"})
            second = client.get("/api/v1/health", headers={"X-Forwarded-For": "203.0.113.2"})
            blocked = client.get("/api/v1/health", headers={"X-Forwarded-For": "203.0.113.3"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(list(application.state.rate_buckets), ["198.51.100.20"])

    def test_trusted_proxy_uses_only_a_valid_x_real_ip(self) -> None:
        settings = replace(
            self.settings,
            api_rate_limit_per_minute=1,
            api_trusted_proxy_hosts=("127.0.0.1",),
        )
        with TestClient(create_app(settings), client=("127.0.0.1", 50000)) as client:
            first = client.get("/api/v1/health", headers={"X-Real-IP": "203.0.113.10"})
            blocked = client.get(
                "/api/v1/health",
                headers={"X-Real-IP": "203.0.113.10", "X-Forwarded-For": "198.51.100.8"},
            )
            other = client.get("/api/v1/health", headers={"X-Real-IP": "203.0.113.11"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(other.status_code, 200)

    def test_rate_bucket_cardinality_has_a_hard_memory_bound(self) -> None:
        settings = replace(
            self.settings,
            api_rate_limit_per_minute=100,
            api_rate_limit_max_clients=8,
            api_trusted_proxy_hosts=("127.0.0.1",),
        )
        application = create_app(settings)
        with TestClient(application, client=("127.0.0.1", 50000)) as client:
            for suffix in range(1, 33):
                response = client.get(
                    "/api/v1/health",
                    headers={"X-Real-IP": f"198.51.100.{suffix}"},
                )
                self.assertEqual(response.status_code, 200)
        self.assertEqual(application.state.rate_bucket_limit, 8)
        self.assertLessEqual(len(application.state.rate_buckets), 8)

    def test_admin_token_authentication_uses_constant_time_comparison(self) -> None:
        with patch("app.api.main.secrets.compare_digest", return_value=False) as compare:
            with TestClient(create_app(self.settings)) as client:
                response = client.post(
                    "/api/v1/replays/missing/run",
                    headers={"X-Admin-Token": "wrong"},
                )
        self.assertEqual(response.status_code, 403)
        compare.assert_called_once_with("wrong", "test-secret")

    def test_router_abstains_routes_positive_and_flags_downside(self) -> None:
        router = RiskRouter(Path(self.temp_dir.name) / "missing.joblib")
        positive = router.predict("Record revenue and profit growth beat estimates; guidance raised with a new partnership.")
        risk = router.predict("The issuer filed Chapter 11 bankruptcy after default and faces liquidation.")
        uncertain = router.predict("Management published a routine update.")
        ai_benchmark = router.predict(
            "OpenAI models escaped a secure test environment and hacked Hugging Face to cheat on a benchmark evaluation."
        )
        fed_collision = router.predict(
            "The Fed rang the alarm about Anthropic's AI model but had to go months without it."
        )
        self.assertEqual(positive["label"], "NON_TARGET")
        self.assertEqual(risk["label"], "RISK_REVIEW")
        self.assertEqual(uncertain["label"], "ABSTAIN")
        self.assertEqual(ai_benchmark["label"], "NON_TARGET")
        self.assertEqual(fed_collision["label"], "ABSTAIN")
        self.assertEqual(ai_benchmark["runtime"], "scope_guardrail")
        self.assertTrue(
            all(
                item["no_trading"]
                for item in (positive, risk, uncertain, ai_benchmark, fed_collision)
            )
        )

    def test_corrupt_model_artifact_degrades_visibly_and_safely(self) -> None:
        artifact = Path(self.temp_dir.name) / "corrupt.joblib"
        artifact.write_bytes(b"not-a-joblib-model")
        router = RiskRouter(artifact)
        status = router.status()
        prediction = router.predict("The issuer filed a voluntary Chapter 11 bankruptcy petition.")
        self.assertEqual(status["status"], "fallback")
        self.assertIsNotNone(status["load_error"])
        self.assertEqual(prediction["runtime"], "fallback")
        self.assertTrue(prediction["no_trading"])
        self.assertTrue(prediction["shadow"])

    def test_all_replay_cases_meet_frozen_expectations(self) -> None:
        operations = OperationsRepository(self.settings.operations_db)
        replay = ReplayService(
            self.settings.replay_dir,
            RiskRouter(Path(self.temp_dir.name) / "missing.joblib"),
            operations,
        )
        results = [replay.run(case["case_id"]) for case in replay.cases()]
        self.assertEqual(len(results), 4)
        self.assertTrue(all(result["expectation_met"] for result in results))
        self.assertTrue(all(not result["external_network_used"] for result in results))
        risk_case = next(result for result in results if result["case_id"] == "sec_bankruptcy_verified")
        self.assertEqual(risk_case["steps"][0]["shadow_decision"]["label"], "ABSTAIN")
        self.assertFalse(risk_case["steps"][0]["shadow_decision"]["alert_eligible"])
        self.assertTrue(risk_case["steps"][1]["shadow_decision"]["alert_eligible"])
        correction_case = next(
            result for result in results if result["case_id"] == "sec_filing_corrected_abstain"
        )
        self.assertTrue(correction_case["steps"][0]["shadow_decision"]["alert_eligible"])
        self.assertEqual(correction_case["steps"][1]["evidence_state"]["status"], "CONFLICT_REVIEW")
        self.assertEqual(correction_case["steps"][1]["shadow_decision"]["label"], "ABSTAIN")
        self.assertFalse(correction_case["steps"][1]["shadow_decision"]["alert_eligible"])
        self.assertEqual(correction_case["steps"][1]["observation"]["revision_kind"], "CORRECTION")
        self.assertEqual(correction_case["steps"][1]["observation"]["supersedes_step"], 1)
        self.assertEqual(len(operations.replay_runs()), 4)
        self.assertTrue(all(run["status"] == "COMPLETED" for run in operations.replay_runs()))

    def test_online_backup_and_isolated_restore_drill(self) -> None:
        operations = OperationsRepository(self.settings.operations_db)
        result = create_and_verify(
            self.ledger_path,
            Path(self.temp_dir.name) / "backups",
            operations,
            retention=2,
            evidence_dir=self.settings.evidence_object_dir,
            report_dir=Path(self.temp_dir.name) / "reports",
        )
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["verification"]["quick_check"], "ok")
        self.assertEqual(result["verification"]["counts"]["canonical_events"], 1)
        self.assertTrue(result["verification"]["isolated_restore"])
        self.assertTrue(result["verification"]["manifest_verified"])
        self.assertTrue(Path(result["manifest_path"]).is_file())
        self.assertTrue((Path(result["backup_path"]) / "operations.sqlite3").is_file())
        restored = verify_restore(Path(result["backup_path"]))
        self.assertTrue(restored["manifest_verified"])
        self.assertEqual(restored["operations"]["quick_check"], "ok")
        self.assertEqual(operations.health()["counts"]["backup_runs"], 1)
        self.assertEqual(operations.latest_backup()["status"], "VERIFIED")

    def test_production_backup_defaults_keep_one_latest_verified_daily_bundle(self) -> None:
        parameters = inspect.signature(create_and_verify).parameters
        self.assertEqual(parameters["retention"].default, 1)
        self.assertEqual(parameters["weekly_retention"].default, 0)

    def test_backup_retention_removes_sqlite_companions(self) -> None:
        backup_dir = Path(self.temp_dir.name) / "retention"
        backup_dir.mkdir()
        old = backup_dir / "finance_radar_20260101T000000Z.sqlite3"
        new = backup_dir / "finance_radar_20260102T000000Z.sqlite3"
        for path in (old, Path(f"{old}-wal"), Path(f"{old}-shm"), Path(f"{old}-journal"), new):
            path.write_bytes(b"fixture")
        os.utime(old, (1, 1))
        os.utime(new, (2, 2))
        removed = prune_backups(backup_dir, retention=1)
        self.assertEqual(set(removed), {str(old), f"{old}-wal", f"{old}-shm", f"{old}-journal"})
        self.assertTrue(new.exists())

    def test_backup_retention_protects_the_bundle_just_verified_despite_skewed_mtime(self) -> None:
        backup_dir = Path(self.temp_dir.name) / "retention-protected"
        backup_dir.mkdir()
        skewed_old = backup_dir / "finance_radar_20990101T000000Z.sqlite3"
        just_verified = backup_dir / "finance_radar_20260102T000000Z.sqlite3"
        skewed_old.write_bytes(b"old-but-future-dated")
        just_verified.write_bytes(b"newly-verified")
        os.utime(skewed_old, (9_999_999, 9_999_999))
        os.utime(just_verified, (1, 1))

        removed = prune_backups(backup_dir, retention=1, verified_path=just_verified)

        self.assertEqual(removed, [str(skewed_old)])
        self.assertFalse(skewed_old.exists())
        self.assertTrue(just_verified.exists())

    def test_weekly_snapshot_is_verified_idempotent_and_retained(self) -> None:
        backup_dir = Path(self.temp_dir.name) / "weekly-source"
        backup_dir.mkdir()
        create_and_verify(
            self.ledger_path,
            backup_dir,
            OperationsRepository(self.settings.operations_db),
            retention=14,
            weekly_retention=8,
            evidence_dir=self.settings.evidence_object_dir,
            report_dir=Path(self.temp_dir.name) / "reports",
        )
        daily = max(
            (path for path in backup_dir.glob("finance_radar_*") if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
        )
        weekly_dir = backup_dir / "manual-weekly"
        first = create_weekly_snapshot(
            daily,
            weekly_dir,
            at=datetime(2026, 7, 18, tzinfo=timezone.utc),
        )
        second = create_weekly_snapshot(
            daily,
            weekly_dir,
            at=datetime(2026, 7, 18, tzinfo=timezone.utc),
        )
        self.assertEqual(first["status"], "CREATED")
        self.assertEqual(second["status"], "RETAINED")
        self.assertEqual(first["verification"]["quick_check"], "ok")
        self.assertEqual(len(list(weekly_dir.glob("*.sqlite3"))), 1)

    def test_zero_weekly_retention_disables_and_prunes_snapshots(self) -> None:
        weekly_dir = Path(self.temp_dir.name) / "weekly-disabled"
        weekly_dir.mkdir()
        old = weekly_dir / "finance_radar_week_2026-W29.sqlite3"
        for path in (old, Path(f"{old}-wal"), Path(f"{old}-shm"), Path(f"{old}-journal")):
            path.write_bytes(b"fixture")

        result = create_weekly_snapshot(
            self.ledger_path,
            weekly_dir,
            retention=0,
            at=datetime(2026, 7, 18, tzinfo=timezone.utc),
        )

        self.assertEqual(result["status"], "DISABLED")
        self.assertIsNone(result["backup_path"])
        self.assertEqual(result["backup_bytes"], 0)
        self.assertIsNone(result["verification"])
        self.assertEqual(
            set(result["pruned"]),
            {str(old), f"{old}-wal", f"{old}-shm", f"{old}-journal"},
        )
        self.assertEqual(list(weekly_dir.glob("*.sqlite3")), [])
        self.assertEqual(list(weekly_dir.glob("*.sqlite3-*")), [])

    @patch("app.workers.continuous.subprocess.run")
    def test_worker_records_partial_source_failure_as_degraded(self, run_mock) -> None:
        report_path = ROOT / "reports" / "live_cycle_latest.json"
        prior = report_path.read_bytes() if report_path.exists() else None
        report_path.parent.mkdir(parents=True, exist_ok=True)
        def partial_run(*args, **kwargs):
            report_path.write_text(
                json.dumps({"finished_at": "2026-07-18T00:00:00Z", "official_sources": {"errors": ["one feed"]}}),
                encoding="utf-8",
            )
            result = Mock()
            result.returncode = 1
            result.stdout = ""
            result.stderr = "one source failed"
            return result
        run_mock.side_effect = partial_run
        try:
            operations = OperationsRepository(self.settings.operations_db)
            status, result = execute_cycle(
                self.settings,
                operations,
                send=False,
                timeout=1,
                health_only=False,
            )
            self.assertEqual(status, "DEGRADED")
            self.assertEqual(result["process"]["returncode"], 1)
            persisted = operations.latest_worker_cycle()
            self.assertEqual(persisted["status"], "DEGRADED")
            self.assertEqual(operations.get_state("worker_heartbeat")["status"], "DEGRADED")
        finally:
            if prior is None:
                report_path.unlink(missing_ok=True)
            else:
                report_path.write_bytes(prior)

    @patch("app.workers.continuous.subprocess.run")
    def test_worker_does_not_reuse_stale_report_after_child_crash(self, run_mock) -> None:
        report_path = ROOT / "reports" / "live_cycle_latest.json"
        prior = report_path.read_bytes() if report_path.exists() else None
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps({"finished_at": "stale", "official_sources": {"old": True}}),
            encoding="utf-8",
        )
        run_mock.return_value.returncode = 1
        run_mock.return_value.stdout = ""
        run_mock.return_value.stderr = "import crash"
        try:
            operations = OperationsRepository(self.settings.operations_db)
            status, result = execute_cycle(
                self.settings,
                operations,
                send=False,
                timeout=1,
                health_only=False,
            )
            self.assertEqual(status, "FAILED")
            self.assertNotIn("finished_at", result)
            self.assertFalse(report_path.exists())
        finally:
            if prior is None:
                report_path.unlink(missing_ok=True)
            else:
                report_path.write_bytes(prior)

    def test_worker_runtime_window_is_evidence_based(self) -> None:
        operations = OperationsRepository(self.settings.operations_db)
        with closing(operations.connect()) as connection:
            for index in range(25):
                started = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc).timestamp() + index * 3600
                started_at = datetime.fromtimestamp(started, timezone.utc).isoformat()
                finished_at = datetime.fromtimestamp(started + 10, timezone.utc).isoformat()
                connection.execute(
                    "INSERT INTO worker_cycles VALUES (?,?,?,?,?,?)",
                    (f"window-{index}", started_at, finished_at, "SUCCESS", "{}", None),
                )
            connection.commit()
        window = operations.worker_window(
            hours=24,
            expected_interval_seconds=3600,
            now=datetime(2026, 7, 18, 12, 15, tzinfo=timezone.utc),
        )
        self.assertTrue(window["complete"])
        self.assertEqual(window["status"], "PASS")
        self.assertEqual(window["success_rate"], 1.0)
        self.assertGreaterEqual(window["observed_hours"], 24)


if __name__ == "__main__":
    unittest.main()
