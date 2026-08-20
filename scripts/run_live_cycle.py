#!/usr/bin/env python3
"""Run one leased Finance Radar live cycle from discovery through outbox."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.models import RiskRouter
from app.services import (
    EvidenceAgent,
    LocalEvidenceModelProvider,
    evidence_receipt_fingerprint,
    run_shadow_batch,
)
from app.storage import EvidenceObjectStore, LedgerRepository, OperationsRepository
from app.services.evidence_agent import PROMPT_VERSION as EVIDENCE_AGENT_CONTRACT_VERSION
from apply_live_asset_relations import apply_relations
from build_live_evidence_review import build_rows, write_outputs
from build_live_review_triage import build as build_review_triage
from build_live_review_triage import write_outputs as write_triage_outputs
from event_ledger import open_ledger, record_source_poll, stable_json, upsert_source, utc_now
from live_candidate_extractor import process_pending, write_report as write_candidate_report
from link_sec_issuer_assets import link_sec_issuer_assets
from observe_live_event_markets import run_pending, schedule_followup_jobs, schedule_jobs
from official_event_collector import collect_all as collect_official_sources
from official_primary_page_enricher import enrich as enrich_official_primary_pages
from opennews_free_collector import collect_category
from sec_filing_enricher import (
    SecFilingClient,
    enrich_pending as enrich_sec_filings,
    materialize_parsed_enrichment_evidence,
    repair_negated_enrichment_matches,
    reclassify_parsed_enrichments,
    reopen_inconclusive_sec_events,
)
from snapshot_evidence_sources import archive_pending as archive_evidence_sources
from snapshot_evidence_sources import write_report as write_snapshot_report
from telegram_alert_outbox import (
    TelegramBotClient,
    deliver_pending,
    enqueue_verified_alerts,
    expire_stale_pending,
    require_bot_config,
)
from telegram_mtproto_listener import load_dotenv


DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_ENV = ROOT / ".env"
DEFAULT_REPORT = ROOT / "reports" / "live_cycle_latest.json"
CYCLE_LEASE_MIN_TTL_SECONDS = 900
CYCLE_LEASE_RENEW_INTERVAL_SECONDS = 60.0


def cycle_lease_ttl_seconds(timeout: float) -> int:
    """Bind the lease to at least twice the outer worker's child timeout."""

    outer_timeout = max(120, int(timeout * 20))
    return max(CYCLE_LEASE_MIN_TTL_SECONDS, outer_timeout * 2)


def acquire_cycle_lease(
    connection: Any,
    *,
    ttl_seconds: int = CYCLE_LEASE_MIN_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> str | None:
    now = now or dt.datetime.now(dt.timezone.utc)
    token = str(uuid.uuid4())
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "DELETE FROM runtime_leases WHERE lease_name='live_cycle' AND expires_at<=?",
            (now.isoformat(),),
        )
        before = connection.total_changes
        connection.execute(
            """INSERT OR IGNORE INTO runtime_leases(
               lease_name,lease_token,acquired_at,expires_at) VALUES ('live_cycle',?,?,?)""",
            (
                token,
                now.isoformat(),
                (now + dt.timedelta(seconds=ttl_seconds)).isoformat(),
            ),
        )
        acquired = connection.total_changes > before
        connection.commit()
        return token if acquired else None
    except Exception:
        connection.rollback()
        raise


def renew_cycle_lease(
    connection: Any,
    token: str,
    *,
    ttl_seconds: int = CYCLE_LEASE_MIN_TTL_SECONDS,
    now: dt.datetime | None = None,
) -> bool:
    reference = now or dt.datetime.now(dt.timezone.utc)
    connection.execute("BEGIN IMMEDIATE")
    try:
        cursor = connection.execute(
            """UPDATE runtime_leases SET expires_at=?
               WHERE lease_name='live_cycle' AND lease_token=?""",
            ((reference + dt.timedelta(seconds=ttl_seconds)).isoformat(), token),
        )
        connection.commit()
        return cursor.rowcount == 1
    except Exception:
        connection.rollback()
        raise


def release_cycle_lease(connection: Any, token: str) -> None:
    connection.execute(
        "DELETE FROM runtime_leases WHERE lease_name='live_cycle' AND lease_token=?", (token,)
    )
    connection.commit()


class CycleLeaseHeartbeat:
    """Renew the live-cycle lease on a separate SQLite connection."""

    def __init__(
        self,
        db_path: Path,
        token: str,
        *,
        ttl_seconds: int,
        interval_seconds: float = CYCLE_LEASE_RENEW_INTERVAL_SECONDS,
    ) -> None:
        self.db_path = db_path
        self.token = token
        self.ttl_seconds = ttl_seconds
        self.interval_seconds = min(max(0.05, interval_seconds), max(0.05, ttl_seconds / 3))
        self.lost = False
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="finance-radar-cycle-lease",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=max(2.0, self.interval_seconds * 2))

    def _run(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            while not self._stop.is_set():
                if connection is None:
                    try:
                        # The main cycle has already initialized the schema.
                        # Opening through open_ledger() would perform schema
                        # writes and can race the cycle's own transactions.
                        # Keep this connection lease-only and bound lock waits
                        # so one busy database cannot strand shutdown.
                        lock_wait = min(5.0, max(0.1, self.interval_seconds))
                        connection = sqlite3.connect(self.db_path, timeout=lock_wait)
                        connection.execute(f"PRAGMA busy_timeout={int(lock_wait * 1000)}")
                    except Exception as exc:
                        self.last_error = f"{type(exc).__name__}: {exc}"
                        connection = None
                        if self._stop.wait(self.interval_seconds):
                            return
                        continue
                if self._stop.wait(self.interval_seconds):
                    return
                try:
                    if not renew_cycle_lease(
                        connection,
                        self.token,
                        ttl_seconds=self.ttl_seconds,
                    ):
                        self.lost = True
                        return
                    self.last_error = None
                except Exception as exc:
                    self.last_error = f"{type(exc).__name__}: {exc}"
        finally:
            if connection is not None:
                connection.close()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def decision_matches_current_evidence_receipt(
    decision: dict[str, Any],
    *,
    event_id: str,
    evidence_agent: EvidenceAgent,
) -> bool:
    """Return true only when a persisted decision still describes this event.

    Legacy records intentionally fail closed: before the receipt contract was
    introduced, an agent decision had no durable way to prove it saw the latest
    event version and evidence state.
    """

    output = decision.get("output")
    if not isinstance(output, dict):
        return False
    if str(decision.get("prompt_version") or output.get("prompt_version") or "") != EVIDENCE_AGENT_CONTRACT_VERSION:
        return False
    try:
        recorded_version = int(output.get("event_version"))
    except (TypeError, ValueError):
        return False
    recorded_fingerprint = str(output.get("evidence_receipt_fingerprint") or "")
    if not recorded_fingerprint:
        return False

    detail = evidence_agent.ledger.event_detail(event_id)
    if detail is None:
        return False
    current_version = int((detail.get("event") or {}).get("current_version") or 0)
    if current_version <= 0 or recorded_version != current_version:
        return False
    current_evidence = evidence_agent.ledger.event_evidence(event_id)
    return recorded_fingerprint == evidence_receipt_fingerprint(
        current_version,
        current_evidence,
    )


def deferred_legacy_adjudications(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Report legacy config rows without granting a continuous worker write authority."""

    return {
        "requested": len(rows),
        "applied": 0,
        "already_applied": 0,
        "status": "LEGACY_REVIEW_CONFIG_UNPROVEN_PROVENANCE",
        "continuous_worker_write_authority": False,
        "formal_mutation_attempted": False,
        "next_action": "explicit_operator_provenance_audit",
    }


def run_pending_evidence_agents(
    connection: Any,
    evidence_agent: EvidenceAgent,
    operations: OperationsRepository,
    *,
    limit: int = 4,
) -> dict[str, Any]:
    """Run bounded internal evidence analysis after primary evidence enrichment."""
    rows = connection.execute(
        """SELECT e.event_id,e.current_version,j.job_id,j.job_type,j.priority
           FROM canonical_events e
           JOIN pipeline_jobs j ON j.event_id=e.event_id
           WHERE (
                 (j.job_type='live_primary_evidence_review' AND e.status='candidate')
                 OR j.job_type='light_verification_followup'
             )
             AND j.status='PENDING_EVIDENCE_REVIEW'
             AND EXISTS (
                 SELECT 1 FROM event_evidence ev WHERE ev.event_id=e.event_id
             )
           ORDER BY j.priority DESC,e.last_updated_at DESC
           LIMIT ?""",
        (max(1, limit),),
    ).fetchall()
    result: dict[str, Any] = {
        "selected": len(rows),
        "run": 0,
        "already_run": 0,
        "stale_or_legacy_rerun": 0,
        "errors": [],
        "by_job_type": {},
        "no_trading": True,
    }
    def advance_fact_state(
        *,
        event_id: str,
        event_version: int,
        job_id: str,
        job_type: str,
        decision_status: str,
        evidence_fingerprint: str | None,
    ) -> None:
        if decision_status == "INSUFFICIENT":
            job_status = "COMPLETED_NEEDS_EVIDENCE"
            workflow_state = "NEEDS_EVIDENCE"
            reasons = ["EVIDENCE_AGENT_INSUFFICIENT"]
        else:
            job_status = "PENDING_HUMAN_REVIEW"
            workflow_state = "NEEDS_HUMAN"
            reasons = [f"EVIDENCE_AGENT_{decision_status}"]
        connection.execute(
            """UPDATE pipeline_jobs
               SET status=?,last_error=NULL,updated_at=?
               WHERE job_id=? AND job_type=? AND status='PENDING_EVIDENCE_REVIEW'""",
            (job_status, utc_now(), job_id, job_type),
        )
        connection.execute(
            """INSERT INTO event_fact_workflow(
                   event_id,event_version,workflow_state,reason_codes_json,
                   evidence_fingerprint,contract_version,updated_at
               ) VALUES (?,?,?,?,?,'event-admission-v1',?)
               ON CONFLICT(event_id,event_version) DO UPDATE SET
                   workflow_state=excluded.workflow_state,
                   reason_codes_json=excluded.reason_codes_json,
                   evidence_fingerprint=excluded.evidence_fingerprint,
                   contract_version=excluded.contract_version,
                   updated_at=excluded.updated_at""",
            (
                event_id,
                event_version,
                workflow_state,
                stable_json(reasons),
                evidence_fingerprint,
                utc_now(),
            ),
        )

    for row in rows:
        event_id = str(row["event_id"])
        event_version = int(row["current_version"])
        job_id = str(row["job_id"])
        job_type = str(row["job_type"])
        result["by_job_type"][job_type] = result["by_job_type"].get(job_type, 0) + 1
        existing = operations.agent_decisions(event_id, limit=1)
        if existing:
            if decision_matches_current_evidence_receipt(
                existing[0],
                event_id=event_id,
                evidence_agent=evidence_agent,
            ):
                prior_output = existing[0].get("output") or {}
                advance_fact_state(
                    event_id=event_id,
                    event_version=event_version,
                    job_id=job_id,
                    job_type=job_type,
                    decision_status=str(existing[0].get("status") or "INSUFFICIENT"),
                    evidence_fingerprint=prior_output.get("evidence_receipt_fingerprint"),
                )
                result["already_run"] += 1
                continue
            result["stale_or_legacy_rerun"] += 1
        try:
            decision = evidence_agent.run(event_id)
            advance_fact_state(
                event_id=event_id,
                event_version=event_version,
                job_id=job_id,
                job_type=job_type,
                decision_status=str(decision["status"]),
                evidence_fingerprint=decision.get("evidence_receipt_fingerprint"),
            )
            result["run"] += 1
            result.setdefault("statuses", {}).setdefault(decision["status"], 0)
            result["statuses"][decision["status"]] += 1
        except Exception as exc:
            connection.execute(
                """UPDATE pipeline_jobs
                   SET attempts=attempts+1,last_error=?,updated_at=?
                   WHERE job_id=? AND job_type=?""",
                (f"{type(exc).__name__}: {str(exc)[:500]}", utc_now(), job_id, job_type),
            )
            result["errors"].append(f"{event_id}:{type(exc).__name__}:{str(exc)[:240]}")
    connection.commit()
    return result


def run_cycle(
    connection: Any,
    *,
    send: bool,
    timeout: float,
    operations: OperationsRepository | None = None,
    evidence_object_store: EvidenceObjectStore | None = None,
    ledger_repository: LedgerRepository | None = None,
    risk_router: RiskRouter | None = None,
    evidence_agent: EvidenceAgent | None = None,
) -> dict[str, Any]:
    started_at = utc_now()
    result: dict[str, Any] = {"started_at": started_at, "errors": []}
    sec_user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    official = collect_official_sources(
        connection,
        sec_user_agent=sec_user_agent or None,
        timeout=timeout,
    )
    result["official_sources"] = official["sources"]
    result["errors"].extend(official["errors"])
    upsert_source(
        connection,
        source_id="opennews_free",
        name="OpenNews Free hot feed",
        source_type="aggregated_discovery",
        authority_tier="P2_experimental",
    )
    collection = {"items": 0, "new_revisions": 0, "jobs": 0, "categories": 0}
    for category in ("macro", "ai", "web3"):
        try:
            counts = collect_category(connection, category=category, timeout=timeout)
        except (RuntimeError, ValueError) as exc:
            result["errors"].append(f"opennews:{category}:{exc}")
            continue
        collection["categories"] += 1
        for key in ("items", "new_revisions", "jobs"):
            collection[key] += counts[key]
    result["opennews"] = collection
    record_source_poll(
        connection,
        source_id="opennews_free",
        cursor_type="aggregate_hot_feed",
        cursor_value=stable_json(
            {"categories": collection["categories"], "items": collection["items"]}
        ),
        status="SUCCESS" if collection["categories"] else "FAILED",
        error=None if collection["categories"] else "all OpenNews categories failed",
    )
    connection.commit()

    candidates = process_pending(connection, limit=500)
    result["candidate_extraction"] = candidates
    write_candidate_report(
        ROOT / "reports" / "live_candidate_extraction_latest.md", candidates, connection
    )

    reopened_sec_events = reopen_inconclusive_sec_events(connection)
    if sec_user_agent:
        result["sec_filing_enrichment"] = enrich_sec_filings(
            connection,
            SecFilingClient(sec_user_agent, timeout=timeout),
            limit=8,
        )
    else:
        result["sec_filing_enrichment"] = {
            "requested": 0,
            "parsed": 0,
            "errors": 0,
            "skipped_missing_user_agent": 1,
        }
    result["sec_filing_enrichment"]["inconclusive_events_reopened"] = reopened_sec_events
    result["sec_filing_enrichment"]["negated_match_repairs"] = (
        repair_negated_enrichment_matches(connection)
    )
    result["sec_filing_enrichment"]["semantic_reclassifications"] = (
        reclassify_parsed_enrichments(connection)
    )
    result["sec_filing_enrichment"]["evidence_materialization"] = (
        materialize_parsed_enrichment_evidence(connection)
    )

    if sec_user_agent:
        result["official_primary_page_enrichment"] = enrich_official_primary_pages(
            connection,
            cache_dir=ROOT / "data" / "cache" / "official_primary_pages",
            user_agent=sec_user_agent,
            limit=20,
            timeout=timeout,
            max_chars=1200,
        )
        result["errors"].extend(
            f"official_primary_page:{error}"
            for error in result["official_primary_page_enrichment"]["errors"]
        )
    else:
        result["official_primary_page_enrichment"] = {
            "selected": 0,
            "inserted": 0,
            "passages": 0,
            "link_only": 0,
            "errors": [],
            "by_type": {},
            "skipped_missing_user_agent": 1,
        }

    triage_rows = build_review_triage(connection)
    write_triage_outputs(
        triage_rows,
        ROOT / "data" / "research" / "live_review_triage.csv",
        ROOT / "reports" / "live_review_triage_latest.md",
    )
    result["review_triage"] = {
        "pending_events": len(triage_rows),
        "top_score": triage_rows[0]["review_score"] if triage_rows else None,
    }

    evidence_config = load_json(ROOT / "config" / "live_evidence_routes.json")
    evidence_rows = build_rows(connection, evidence_config)
    write_outputs(
        evidence_rows,
        ROOT / "data" / "research" / "live_evidence_review_queue.csv",
        ROOT / "reports" / "live_evidence_review_latest.md",
    )
    result["evidence_review"] = {
        "pending_events": len({row["event_id"] for row in evidence_rows}),
        "routes": len(evidence_rows),
    }

    adjudication_rows = load_json(
        ROOT / "config" / "live_primary_adjudications.json"
    )["adjudications"]
    result["adjudications"] = deferred_legacy_adjudications(adjudication_rows)

    if sec_user_agent and operations is not None and evidence_object_store is not None:
        snapshots = archive_evidence_sources(
            connection,
            operations,
            evidence_object_store,
            user_agent=sec_user_agent,
            limit=8,
            timeout=timeout,
        )
        write_snapshot_report(
            ROOT / "reports" / "evidence_source_snapshots_latest.json",
            ROOT / "reports" / "evidence_source_snapshots_latest.md",
            snapshots,
        )
        result["evidence_source_snapshots"] = snapshots
    else:
        result["evidence_source_snapshots"] = {
            "status": "SKIPPED",
            "reason": (
                "missing_SEC_USER_AGENT"
                if not sec_user_agent
                else "operations_or_object_store_not_configured"
            ),
            "archived": 0,
            "errors": [],
        }

    if operations is not None and ledger_repository is not None and risk_router is not None:
        result["shadow_routing"] = run_shadow_batch(
            ledger_repository,
            operations,
            risk_router,
            scan_limit=200,
            run_limit=100,
        )
    else:
        result["shadow_routing"] = {
            "status": "SKIPPED",
            "reason": "operations_ledger_or_router_not_configured",
            "recorded": 0,
        }

    if evidence_agent is not None and operations is not None:
        result["evidence_agent"] = run_pending_evidence_agents(
            connection,
            evidence_agent,
            operations,
            limit=4,
        )
    else:
        result["evidence_agent"] = {
            "status": "SKIPPED",
            "reason": "evidence_agent_or_operations_not_configured",
            "run": 0,
        }

    asset_events = load_json(ROOT / "config" / "live_asset_relations.json")["events"]
    result["asset_relations"] = apply_relations(connection, asset_events)
    if sec_user_agent:
        result["sec_issuer_assets"] = link_sec_issuer_assets(
            connection,
            cache_dir=ROOT / "data" / "cache" / "sec_company_tickers",
            user_agent=sec_user_agent,
            timeout=timeout,
        )
    else:
        result["sec_issuer_assets"] = {
            "selected": 0,
            "mapped": 0,
            "market_enabled": 0,
            "errors": ["missing_SEC_USER_AGENT"],
        }

    today = dt.datetime.now(dt.timezone.utc).date()
    scheduled = schedule_jobs(connection, freshness_days=14, today=today)
    followups_before = schedule_followup_jobs(connection)
    api_key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
    # Binance public crypto observations require no credentials and must still
    # run when the optional Twelve Data key is absent.
    market = run_pending(connection, api_key=api_key, timeout=timeout)
    market["scheduled"] = scheduled
    market["followups_scheduled"] = followups_before + schedule_followup_jobs(connection)
    result["market"] = market

    result["outbox_expired_stale"] = expire_stale_pending(
        connection, max_age_hours=24
    )
    result["outbox_inserted"] = enqueue_verified_alerts(
        connection, freshness_days=3, today=today
    )
    if send:
        token, chat_id = require_bot_config()
        sent, errors = deliver_pending(connection, TelegramBotClient(token), chat_id)
        result["telegram"] = {"sent": sent, "errors": errors}
    else:
        result["telegram"] = {"sent": 0, "errors": 0, "mode": "dry_run"}
    result["finished_at"] = utc_now()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--send", action="store_true", help="deliver new eligible Telegram outbox rows")
    args = parser.parse_args()
    load_dotenv(args.env_file)
    settings = Settings.from_env()
    connection = open_ledger(args.db)
    lease_ttl = cycle_lease_ttl_seconds(args.timeout)
    lease = acquire_cycle_lease(connection, ttl_seconds=lease_ttl)
    if lease is None:
        connection.close()
        print("live_cycle=skipped reason=lease_held")
        return 3
    heartbeat = CycleLeaseHeartbeat(args.db, lease, ttl_seconds=lease_ttl)
    heartbeat.start()
    try:
        operations = OperationsRepository(settings.operations_db)
        evidence_object_store = EvidenceObjectStore(settings.evidence_object_dir)
        ledger_repository = LedgerRepository(args.db)
        risk_router = RiskRouter(settings.model_artifact, settings.model_card)
        evidence_model_provider = (
            LocalEvidenceModelProvider(
                settings.evidence_llm_url,
                settings.evidence_llm_model,
                timeout_seconds=settings.evidence_llm_timeout_seconds,
                max_tokens=settings.evidence_llm_max_tokens,
            )
            if settings.evidence_llm_url
            else None
        )
        evidence_agent = EvidenceAgent(
            ledger_repository,
            operations,
            evidence_object_store,
            evidence_model_provider,
        )
        result = run_cycle(
            connection,
            send=args.send,
            timeout=args.timeout,
            operations=operations,
            evidence_object_store=evidence_object_store,
            ledger_repository=ledger_repository,
            risk_router=risk_router,
            evidence_agent=evidence_agent,
        )
    finally:
        heartbeat.stop()
        release_cycle_lease(connection, lease)
        connection.close()
    if heartbeat.lost:
        result.setdefault("errors", []).append("live_cycle_lease_lost")
    elif heartbeat.last_error:
        result["lease_heartbeat_warning"] = heartbeat.last_error
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(stable_json(result))
    print(f"REPORT={args.report}")
    return 1 if result["errors"] or result["market"].get("errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
