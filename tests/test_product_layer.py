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
from app.ops.backup import create_and_verify, create_weekly_snapshot
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
        self.assertEqual(providers["ibkr_tws_readonly"]["status"], "LOCAL_PROBE_ONLY")
        self.assertTrue(capabilities["boundary"]["no_trading"])
        self.assertTrue(all(not item["order_endpoints_present"] for item in providers.values()))
        self.assertEqual(
            capabilities["horizon_policy"]["windows"],
            ["t_plus_5m", "t_plus_30m", "t_plus_1d"],
        )

    def test_api_contract_and_admin_boundary(self) -> None:
        with TestClient(create_app(self.settings)) as client:
            health = client.get("/api/v1/health")
            self.assertEqual(health.status_code, 200)
            payload = health.json()
            self.assertIn("schema_version", payload)
            self.assertIn("trace_id", payload)
            self.assertIn("generated_at", payload)
            self.assertEqual(payload["data"]["ledger"]["counts"]["canonical_events"], 1)
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
            archive = client.get("/api/v1/evidence/archive")
            self.assertEqual(archive.status_code, 200)
            self.assertTrue(archive.json()["data"]["policy"]["immutable"])
            facets = client.get("/api/v1/events/facets")
            self.assertEqual(facets.status_code, 200)
            self.assertTrue(facets.json()["data"]["read_only"])
            filtered = client.get("/api/v1/events", params={"source": "test"})
            self.assertEqual(filtered.status_code, 200)
            self.assertEqual(filtered.json()["data"]["total"], 1)
            detail = client.get("/api/v1/events/evt-1")
            self.assertEqual(detail.status_code, 200)
            self.assertTrue(detail.json()["data"]["model_shadow_output"]["no_trading"])
            denied = client.post("/api/v1/replays/positive_earnings_non_target/run")
            self.assertEqual(denied.status_code, 403)
            allowed = client.post(
                "/api/v1/replays/positive_earnings_non_target/run",
                headers={"X-Admin-Token": "test-secret"},
            )
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(allowed.json()["data"]["final_label"], "NON_TARGET")

    def test_structured_evidence_agent_persists_trace_objects_and_override(self) -> None:
        with TestClient(create_app(self.settings)) as client:
            denied = client.post("/api/v1/events/evt-1/agent/run")
            self.assertEqual(denied.status_code, 403)
            run = client.post(
                "/api/v1/events/evt-1/agent/run",
                headers={"X-Admin-Token": "test-secret"},
            )
            self.assertEqual(run.status_code, 200)
            result = run.json()["data"]
            self.assertEqual(result["status"], "EVIDENCE_READY")
            self.assertFalse(result["llm_used"])
            self.assertEqual(result["model_provider"], "deterministic_guarded_fallback")
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
                    "reason": "Verified the exact primary-source passage",
                    "review_status": "REVIEWED_NO_CHANGE",
                },
            )
            self.assertEqual(override.status_code, 200)
            self.assertTrue(override.json()["data"]["no_trading"])
            trace = client.get("/api/v1/events/evt-1/trace").json()["data"]
            self.assertEqual(len(trace["agent_decisions"]), 1)
            self.assertEqual(len(trace["human_overrides"]), 1)
            self.assertEqual(len(trace["evidence_objects"]), 1)
            self.assertEqual(trace["evidence_objects"][0]["object_kind"], "EXACT_EXCERPT")
            self.assertTrue(trace["evidence_objects"][0]["integrity_verified"])
            archive = client.get("/api/v1/evidence/archive").json()["data"]
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
                },
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["error"]["code"], "AGENT_DECISION_REQUIRED")

    def test_evidence_agent_forces_insufficient_when_exact_passage_is_missing(self) -> None:
        with closing(sqlite3.connect(self.ledger_path)) as connection:
            connection.execute("DELETE FROM event_evidence WHERE event_id='evt-1'")
            connection.commit()
        with TestClient(create_app(self.settings)) as client:
            response = client.post(
                "/api/v1/events/evt-1/agent/run",
                headers={"X-Admin-Token": "test-secret"},
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

    def test_router_abstains_routes_positive_and_flags_downside(self) -> None:
        router = RiskRouter(Path(self.temp_dir.name) / "missing.joblib")
        positive = router.predict("Record revenue and profit growth beat estimates; guidance raised with a new partnership.")
        risk = router.predict("The issuer filed Chapter 11 bankruptcy after default and faces liquidation.")
        uncertain = router.predict("Management published a routine update.")
        self.assertEqual(positive["label"], "NON_TARGET")
        self.assertEqual(risk["label"], "RISK_REVIEW")
        self.assertEqual(uncertain["label"], "ABSTAIN")
        self.assertTrue(all(item["no_trading"] for item in (positive, risk, uncertain)))

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
        )
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["verification"]["quick_check"], "ok")
        self.assertEqual(result["verification"]["counts"]["canonical_events"], 1)
        self.assertTrue(result["verification"]["isolated_restore"])
        self.assertEqual(operations.health()["counts"]["backup_runs"], 1)
        self.assertEqual(operations.latest_backup()["status"], "VERIFIED")

    def test_production_backup_defaults_preserve_month_and_quarter(self) -> None:
        parameters = inspect.signature(create_and_verify).parameters
        self.assertEqual(parameters["retention"].default, 30)
        self.assertEqual(parameters["weekly_retention"].default, 12)

    def test_backup_retention_removes_sqlite_companions(self) -> None:
        backup_dir = Path(self.temp_dir.name) / "retention"
        backup_dir.mkdir()
        old = backup_dir / "finance_radar_20260101T000000Z.sqlite3"
        new = backup_dir / "finance_radar_20260102T000000Z.sqlite3"
        for path in (old, Path(f"{old}-wal"), Path(f"{old}-shm"), new):
            path.write_bytes(b"fixture")
        os.utime(old, (1, 1))
        os.utime(new, (2, 2))
        removed = prune_backups(backup_dir, retention=1)
        self.assertEqual(set(removed), {str(old), f"{old}-wal", f"{old}-shm"})
        self.assertTrue(new.exists())

    def test_weekly_snapshot_is_verified_idempotent_and_retained(self) -> None:
        backup_dir = Path(self.temp_dir.name) / "weekly-source"
        backup_dir.mkdir()
        create_and_verify(
            self.ledger_path,
            backup_dir,
            OperationsRepository(self.settings.operations_db),
            retention=14,
            weekly_retention=8,
        )
        daily = max(backup_dir.glob("finance_radar_*.sqlite3"), key=lambda path: path.stat().st_mtime)
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
