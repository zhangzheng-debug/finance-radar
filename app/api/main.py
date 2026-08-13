from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import __version__
from app.config import Settings
from app.models import RiskRouter, derive_evidence_context
from app.services import (
    AdjudicationService,
    EvidenceAgent,
    LocalEvidenceModelProvider,
    ReplayService,
    evidence_receipt_fingerprint,
)
from app.services.replay import ReplayCaseNotFound
from app.storage import EvidenceObjectStore, LedgerRepository, OperationsRepository


API_SCHEMA_VERSION = "1.1"
BACKUP_SNAPSHOT_MAX_AGE_SECONDS = 36 * 60 * 60
GENERIC_REVIEWER_IDENTITIES = frozenset(
    {"reviewer", "defense-reviewer", "审核者", "审查员", "unknown", "test"}
)
GENERIC_REVIEW_REASONS = frozenset(
    {
        "已逐条核对精确引文",
        "verified the exact primary-source passage",
        "reviewed",
        "n/a",
    }
)


def _backup_artifact_visibility(path: Path) -> tuple[bool | None, str]:
    """Classify a backup path without treating least-privilege as data loss.

    Production recovery bundles are deliberately owned by root and mode 0700.
    The read-only API account therefore cannot stat their manifests even though
    the independently privileged backup workflow has just verified them.  Keep
    an actual missing path distinct from that intentional access boundary.
    """

    try:
        path.stat()
    except PermissionError:
        return None, "protected"
    except FileNotFoundError:
        return False, "missing"
    except OSError:
        return None, "unavailable"
    return True, "visible"


class HumanOverrideRequest(BaseModel):
    actor: str = Field(min_length=3, max_length=80)
    reason: str = Field(min_length=20, max_length=1000)
    review_status: Literal["HUMAN_REVIEW", "INSUFFICIENT", "REVIEWED_NO_CHANGE"]
    reviewer_attestation: Literal[True]


class EvidenceAgentRunRequest(BaseModel):
    audit_write_confirmed: Literal[True]
    evidence_change_confirmed: bool = False


class AdjudicationReviewRequest(BaseModel):
    reviewer_id: str = Field(min_length=2, max_length=80)
    role: Literal["REVIEWER", "ARBITER"] = "REVIEWER"
    materiality: Literal["MATERIAL_ADVERSE", "NOT_MATERIAL_ADVERSE", "UNCLEAR"]
    polarity: Literal["ADVERSE", "POSITIVE", "NEUTRAL", "MIXED", "UNCLEAR"]
    evidence_state: Literal[
        "PRIMARY_SUPPORTED",
        "MULTI_SOURCE_SUPPORTED",
        "DISCOVERY_ONLY",
        "CONFLICTED",
        "INSUFFICIENT",
    ]
    rationale: str = Field(min_length=20, max_length=3000)


def _normalized_audit_text(value: str) -> str:
    return " ".join(value.split()).strip()


def validate_human_override_attribution(payload: HumanOverrideRequest) -> tuple[str, str]:
    """Reject placeholder attribution before it becomes an immutable audit row."""

    actor = _normalized_audit_text(payload.actor)
    reason = _normalized_audit_text(payload.reason)
    if len(actor) < 3 or actor.casefold() in GENERIC_REVIEWER_IDENTITIES:
        raise HTTPException(
            422,
            {
                "code": "SPECIFIC_REVIEWER_ID_REQUIRED",
                "message": "human-review audit records require a specific reviewer identity",
            },
        )
    if len(reason) < 20 or reason.casefold() in GENERIC_REVIEW_REASONS:
        raise HTTPException(
            422,
            {
                "code": "SPECIFIC_REVIEW_RATIONALE_REQUIRED",
                "message": "human-review audit records require an event-specific rationale",
            },
        )
    return actor, reason


def generated_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def elapsed_seconds(start: str | None, end: str | None = None) -> float | None:
    if not start:
        return None
    try:
        start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=timezone.utc)
        end_at = (
            datetime.fromisoformat(end.replace("Z", "+00:00"))
            if end
            else datetime.now(timezone.utc)
        )
        if end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=timezone.utc)
        return max(0.0, round((end_at - start_at).total_seconds(), 3))
    except (TypeError, ValueError):
        return None


def envelope(request: Request, data: Any) -> dict[str, Any]:
    return {
        "schema_version": API_SCHEMA_VERSION,
        "trace_id": request.state.trace_id,
        "generated_at": generated_at(),
        "data": data,
    }


def error_envelope(request: Request, code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "schema_version": API_SCHEMA_VERSION,
        "trace_id": getattr(request.state, "trace_id", uuid.uuid4().hex),
        "generated_at": generated_at(),
        "error": {"code": code, "message": message, "details": details},
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    ledger = LedgerRepository(settings.ledger_db)
    operations = OperationsRepository(settings.operations_db)
    router = RiskRouter(settings.model_artifact, settings.model_card)
    replay = ReplayService(settings.replay_dir, router, operations)
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
    evidence_object_store = EvidenceObjectStore(settings.evidence_object_dir)
    evidence_agent = EvidenceAgent(
        ledger,
        operations,
        evidence_object_store,
        evidence_model_provider,
    )
    adjudication = AdjudicationService(ledger, operations)

    application = FastAPI(
        title="Finance Radar Read-Mostly API",
        version=__version__,
        description="Evidence-linked financial event intelligence. No trading endpoints exist.",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8501", "http://localhost:8501"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )
    application.state.settings = settings
    application.state.ledger = ledger
    application.state.operations = operations
    application.state.router = router
    application.state.replay = replay
    application.state.evidence_agent = evidence_agent
    application.state.evidence_object_store = evidence_object_store
    application.state.adjudication = adjudication
    rate_buckets: dict[str, deque[float]] = defaultdict(deque)
    rate_lock = Lock()

    @application.middleware("http")
    async def trace_middleware(request: Request, call_next: Callable[..., Any]):
        request.state.trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        limit = settings.api_rate_limit_per_minute
        rate_remaining: int | None = None
        if limit > 0 and request.url.path.startswith("/api/"):
            forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
            client_host = request.client.host if request.client else "unknown"
            key = forwarded or client_host
            now = time.monotonic()
            with rate_lock:
                bucket = rate_buckets[key]
                while bucket and now - bucket[0] >= 60:
                    bucket.popleft()
                if len(bucket) >= limit:
                    response = JSONResponse(
                        status_code=429,
                        content=error_envelope(
                            request,
                            "RATE_LIMITED",
                            f"API rate limit exceeded: {limit} requests per minute",
                        ),
                    )
                    response.headers["Retry-After"] = "60"
                    response.headers["X-RateLimit-Limit"] = str(limit)
                    response.headers["X-RateLimit-Remaining"] = "0"
                    response.headers["X-Trace-Id"] = request.state.trace_id
                    response.headers["X-Content-Type-Options"] = "nosniff"
                    response.headers["Cache-Control"] = "no-store"
                    return response
                bucket.append(now)
                rate_remaining = max(0, limit - len(bucket))
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        if limit > 0 and request.url.path.startswith("/api/"):
            response.headers["X-RateLimit-Limit"] = str(limit)
            response.headers["X-RateLimit-Remaining"] = str(
                rate_remaining if rate_remaining is not None else limit
            )
        return response

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return JSONResponse(
            status_code=exc.status_code,
            content=error_envelope(
                request,
                detail.get("code", "HTTP_ERROR"),
                detail.get("message", str(exc.detail)),
                detail.get("details"),
            ),
        )

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=error_envelope(request, "VALIDATION_ERROR", "request validation failed", exc.errors()),
        )

    def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
        if not settings.admin_token:
            raise HTTPException(
                503,
                {
                    "code": "ADMIN_MUTATIONS_DISABLED",
                    "message": "admin token is not configured; all mutation endpoints are disabled",
                },
            )
        if x_admin_token != settings.admin_token:
            raise HTTPException(403, {"code": "ADMIN_TOKEN_REQUIRED", "message": "valid X-Admin-Token required"})

    def public_backup_status(value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        result = dict(value)
        if result.get("backup_path"):
            result["backup_path"] = Path(str(result["backup_path"])).name
        if result.get("manifest_path"):
            result["manifest_path"] = Path(str(result["manifest_path"])).name
        # A recovery-bundle manifest may contain megabytes of per-file audit
        # inventory.  That belongs in the protected backup artifact, not in a
        # frequently polled liveness response.  Publish only a bounded summary.
        components = result.pop("components", None)
        if isinstance(components, dict):
            result["component_summary"] = {
                "count": len(components),
                "names": sorted(str(name) for name in components)[:32],
            }
        elif isinstance(components, list):
            result["component_summary"] = {"count": len(components)}
        return result

    def public_health_paths(value: dict[str, Any]) -> dict[str, Any]:
        result = dict(value)
        if result.get("database"):
            result["database"] = Path(str(result["database"])).name
        if "latest_backup" in result:
            result["latest_backup"] = public_backup_status(result.get("latest_backup"))
        if "latest_verified_backup" in result:
            result["latest_verified_backup"] = public_backup_status(result.get("latest_verified_backup"))
        return result

    def health_from_latest_verified_backup(
        value: dict[str, Any],
        latest_backup: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Expose live database liveness separately from backup snapshot freshness.

        A live ``PRAGMA quick_check`` scans the entire ledger and previously made
        every dashboard request take about as long as the web timeout.  The backup
        service performs that full check and an isolated restore drill.  It is
        useful integrity evidence, but it must never be presented as a current
        database scan.  Keep both facts explicit in the public health contract.
        """
        result = dict(value)
        live_quick_check = result.get("quick_check")
        live_status = result.get("status")
        result["current_db_liveness"] = {
            "status": live_status,
            "quick_check": live_quick_check,
            "integrity_check_source": result.get("integrity_check_source"),
        }
        backup_snapshot: dict[str, Any] = {
            "status": "MISSING",
            "fresh": False,
            "max_age_seconds": BACKUP_SNAPSHOT_MAX_AGE_SECONDS,
            "age_seconds": None,
            "quick_check": None,
            "verified_at": None,
        }
        if latest_backup and latest_backup.get("status") == "VERIFIED":
            verified_at = latest_backup.get("verified_at")
            age_seconds: float | None = None
            try:
                verified = datetime.fromisoformat(str(verified_at).replace("Z", "+00:00"))
                if verified.tzinfo is None:
                    verified = verified.replace(tzinfo=timezone.utc)
                age_seconds = max(
                    0.0,
                    (datetime.now(timezone.utc) - verified.astimezone(timezone.utc)).total_seconds(),
                )
            except (TypeError, ValueError):
                pass
            backup_quick_check = latest_backup.get("quick_check") or "unknown"
            backup_path = Path(str(latest_backup.get("backup_path") or ""))
            path_available, artifact_visibility = _backup_artifact_visibility(backup_path)
            # A PermissionError proves only that this identity cannot inspect
            # the protected path.  It cannot distinguish an existing bundle
            # from a bundle deleted behind an untraversable parent directory,
            # so a protected record must never be promoted to FRESH.
            fresh = (
                backup_quick_check == "ok"
                and age_seconds is not None
                and age_seconds <= BACKUP_SNAPSHOT_MAX_AGE_SECONDS
                and artifact_visibility == "visible"
            )
            if fresh:
                snapshot_status = "FRESH"
            elif artifact_visibility == "protected":
                snapshot_status = "UNVERIFIABLE_PROTECTED"
            elif age_seconds is not None and age_seconds > BACKUP_SNAPSHOT_MAX_AGE_SECONDS:
                snapshot_status = "STALE"
            elif artifact_visibility == "missing":
                snapshot_status = "MISSING_ARTIFACT"
            else:
                snapshot_status = "UNAVAILABLE"
            backup_snapshot = {
                "status": snapshot_status,
                "fresh": fresh,
                "max_age_seconds": BACKUP_SNAPSHOT_MAX_AGE_SECONDS,
                "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
                "quick_check": backup_quick_check,
                "verified_at": verified_at,
                "snapshot_kind": latest_backup.get("snapshot_kind"),
                "path_available": path_available,
                "artifact_visibility": artifact_visibility,
                "artifact_verification_source": (
                    "live_path_stat_and_latest_verified_backup_record"
                    if artifact_visibility == "visible"
                    else "unprivileged_path_probe_inconclusive"
                    if artifact_visibility == "protected"
                    else "latest_verified_backup_record"
                ),
            }
        result["backup_snapshot"] = backup_snapshot
        # These three legacy fields remain for the existing public dashboard,
        # while ``current_db_liveness`` / ``backup_snapshot`` make the scope
        # unambiguous for operators and automation.
        if backup_snapshot["fresh"]:
            result["quick_check"] = backup_snapshot["quick_check"]
            result["integrity_check_source"] = "latest_verified_backup"
            result["integrity_checked_at"] = backup_snapshot["verified_at"]
        else:
            result["quick_check"] = "unknown"
            result["integrity_check_source"] = (
                "unverifiable_protected_backup"
                if backup_snapshot["status"] == "UNVERIFIABLE_PROTECTED"
                else "stale_or_missing_verified_backup"
            )
            result["integrity_checked_at"] = None
        if live_status != "ok" or not backup_snapshot["fresh"]:
            result["status"] = "degraded"
        return result

    def event_or_404(event_id: str) -> dict[str, Any]:
        event = ledger.event_detail(event_id)
        if event is None:
            raise HTTPException(404, {"code": "EVENT_NOT_FOUND", "message": f"event not found: {event_id}"})
        return event

    @application.get("/")
    def root(request: Request):
        return envelope(
            request,
            {
                "service": "finance-radar-api",
                "docs": "/docs",
                "health": "/api/v1/health",
                "boundary": "read-mostly intelligence; no trading endpoints",
            },
        )

    @application.get("/api/v1/health")
    def health(request: Request):
        try:
            latest_backup = operations.latest_verified_backup()
            ledger_health = public_health_paths(
                health_from_latest_verified_backup(
                    ledger.health(run_integrity_check=False),
                    latest_backup,
                )
            )
            # A request-time PRAGMA quick_check scans the complete operations
            # database.  On production-sized review/evidence stores, repeated
            # probes can form an I/O thundering herd and make the liveness
            # endpoint itself unavailable.  Full restore-time integrity checks
            # remain mandatory in the independently verified backup workflow.
            ops_health = public_health_paths(operations.health(run_integrity_check=False))
            model_health = router.status()
            status = "ok" if ledger_health["status"] == ops_health["status"] == "ok" else "degraded"
            return envelope(
                request,
                {
                    "status": status,
                    "service_version": __version__,
                    "demo_mode": operations.demo_mode(settings.demo_mode),
                    "ledger": ledger_health,
                    "operations": ops_health,
                    "model": model_health,
                    "capabilities": [
                        "events",
                        "evidence",
                        "timeline",
                        "trace",
                        "replay",
                        "shadow_model",
                        "structured_evidence_agent",
                        "read_only_market_context",
                        "content_addressed_evidence",
                        "raw_official_source_snapshots",
                        "human_override_audit",
                        "dual_blind_adjudication",
                        "pre_freeze_label_contract",
                    ],
                    "forbidden_capabilities": ["orders", "positions", "balances", "trade_execution"],
                },
            )
        except FileNotFoundError as exc:
            raise HTTPException(503, {"code": "LEDGER_UNAVAILABLE", "message": str(exc)}) from exc

    @application.get("/api/v1/overview")
    def overview(request: Request):
        latest_backup = operations.latest_verified_backup()
        data = health_from_latest_verified_backup(
            ledger.overview(run_integrity_check=False),
            latest_backup,
        )
        data["demo_mode"] = operations.demo_mode(settings.demo_mode)
        latest_worker = operations.latest_worker_cycle()
        latest_successful_worker = operations.latest_successful_worker_cycle()
        data["latest_worker_cycle"] = latest_worker
        data["latest_backup"] = public_backup_status(latest_backup)
        data["latest_backup_attempt"] = public_backup_status(operations.latest_backup())
        # Keep the legacy alias and the explicit update clock exactly aligned.
        # Computing elapsed time twice makes an otherwise identical API fact
        # differ by milliseconds and turns deterministic contract tests flaky.
        event_update_age_seconds = elapsed_seconds(data.get("last_event_update"))
        data["timing"] = {
            # Legacy field: age of the most recent insert or revision.
            "latest_event_age_seconds": event_update_age_seconds,
            "worker_cycle_duration_seconds": elapsed_seconds(
                latest_worker.get("started_at") if latest_worker else None,
                latest_worker.get("finished_at") if latest_worker else None,
            ),
            "latest_worker_finished_at": latest_worker.get("finished_at") if latest_worker else None,
            "latest_worker_success_at": (
                latest_successful_worker.get("finished_at")
                if latest_successful_worker
                else None
            ),
            "latest_worker_success_age_seconds": elapsed_seconds(
                latest_successful_worker.get("finished_at")
                if latest_successful_worker
                else None
            ),
            "latest_new_event_at": data.get("last_new_event_at"),
            "latest_new_event_age_seconds": elapsed_seconds(data.get("last_new_event_at")),
            "latest_event_update_at": data.get("last_event_update"),
            "latest_event_update_age_seconds": event_update_age_seconds,
        }
        return envelope(request, data)

    @application.get("/api/v1/sources/health")
    def sources_health(request: Request):
        return envelope(request, {"items": ledger.list_source_health()})

    @application.get("/api/v1/market/capabilities")
    def market_capabilities(request: Request):
        return envelope(request, ledger.market_capabilities())

    @application.get("/api/v1/evidence/archive")
    def evidence_archive(request: Request):
        data = operations.evidence_archive_summary()
        eligibility = ledger.evidence_snapshot_eligibility()
        eligible_pairs = ledger.evidence_snapshot_eligible_pairs()
        archived_pairs = operations.source_snapshot_pairs()
        failure_pairs = operations.source_snapshot_failure_pairs()
        eligible = int(eligibility["eligible_links"])
        archived = len(eligible_pairs & archived_pairs)
        terminal_policy = len(eligible_pairs & failure_pairs["terminal_policy"])
        retry_pending = len(eligible_pairs & failure_pairs["retry_pending"])
        archiveable = max(0, eligible - terminal_policy)
        data["coverage"] = {
            **eligibility,
            "archived_links": archived,
            "archiveable_links": archiveable,
            "terminal_policy_exclusions": terminal_policy,
            "retry_pending_links": retry_pending,
            "missing_links": max(0, archiveable - archived),
            "coverage_pct": round(100.0 * archived / archiveable, 2) if archiveable else 100.0,
            "worker_batch_size": 8,
        }
        for item in data["recent_objects"]:
            item["integrity_verified"] = evidence_object_store.verify(
                item["relative_path"], item["object_sha256"]
            )
        data["integrity_failures_in_recent_sample"] = sum(
            not item["integrity_verified"] for item in data["recent_objects"]
        )
        return envelope(request, data)

    @application.get("/api/v1/events")
    def events(
        request: Request,
        status: str | None = None,
        public_state: Literal[
            "verified",
            "excluded",
            "insufficient",
            "pending_verification",
            "rough_reviewed",
        ]
        | None = None,
        family: str | None = None,
        source: str | None = None,
        q: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        sort: Literal["latest", "event_date", "subject"] = "event_date",
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                422,
                {
                    "code": "INVALID_DATE_RANGE",
                    "message": "date_from must not be after date_to",
                },
            )
        return envelope(
            request,
            ledger.list_events(
                status=status,
                public_state=public_state,
                family=family,
                source=source,
                query=q,
                date_from=date_from.isoformat() if date_from else None,
                date_to=date_to.isoformat() if date_to else None,
                sort=sort,
                limit=limit,
                offset=offset,
            ),
        )

    @application.get("/api/v1/events/facets")
    def event_facets(request: Request):
        return envelope(request, ledger.event_facets())

    @application.get("/api/v1/events/{event_id}")
    def event_detail(request: Request, event_id: str):
        data = event_or_404(event_id)
        evidence = ledger.event_evidence(event_id)
        facts = data.get("current_version", {}).get("facts", {}) if data.get("current_version") else {}
        preferred_source = data.get("preferred_source") or {}
        text = "\n".join(
            [
                data["event"].get("company_name") or "",
                preferred_source.get("title") or facts.get("source_title") or "",
                preferred_source.get("summary") or facts.get("source_summary") or "",
                facts.get("evidence_summary") or "",
                *[(item.get("evidence_passage") or "") for item in evidence[:5]],
            ]
        )
        data["evidence_count"] = len(evidence)
        light_verification = facts.get("light_verification") if isinstance(facts, dict) else None
        if isinstance(light_verification, dict):
            data["verification_method"] = {
                "kind": "light_verification",
                "version": light_verification.get("version"),
                "reviewed_at": light_verification.get("reviewed_at"),
                "evidence_ids": light_verification.get("evidence_ids", []),
                "score": light_verification.get("score"),
                "rationale": light_verification.get("rationale"),
                "no_trading": True,
            }
        evidence_context = derive_evidence_context(evidence)
        data["model_shadow_output"] = router.predict(text, evidence_context=evidence_context)
        data["model_input_contract"] = {
            "uses_source_content": True,
            "uses_evidence_passages": True,
            "uses_structured_evidence_state": True,
            "excludes_event_taxonomy_shortcuts": True,
            "shadow_only": True,
        }
        data["no_trading_banner"] = "Intelligence and review only. No execution capability is present."
        return envelope(request, data)

    @application.get("/api/v1/events/{event_id}/timeline")
    def event_timeline(request: Request, event_id: str):
        event_or_404(event_id)
        return envelope(request, {"items": ledger.event_timeline(event_id)})

    @application.get("/api/v1/events/{event_id}/evidence")
    def event_evidence(request: Request, event_id: str):
        event_or_404(event_id)
        return envelope(request, {"items": ledger.event_evidence(event_id)})

    @application.get("/api/v1/events/{event_id}/trace")
    def event_trace(request: Request, event_id: str):
        event_or_404(event_id)
        data = ledger.event_trace(event_id)
        data["model_runs"] = operations.model_runs(event_id)
        data["agent_decisions"] = operations.agent_decisions(event_id)
        data["human_overrides"] = operations.human_overrides(event_id)
        data["light_verifications"] = operations.light_verification_runs(event_id)
        data["evidence_objects"] = operations.evidence_objects(event_id)
        for item in data["evidence_objects"]:
            item["integrity_verified"] = evidence_object_store.verify(
                item["relative_path"], item["object_sha256"]
            )
        return envelope(request, data)

    @application.post("/api/v1/events/{event_id}/agent/run", dependencies=[Depends(require_admin)])
    def run_evidence_agent(
        request: Request,
        event_id: str,
        payload: EvidenceAgentRunRequest,
    ):
        event = event_or_404(event_id)
        workflow_status = str((event.get("event") or {}).get("status") or "").lower()
        if workflow_status in {"verified", "rejected"} and not payload.evidence_change_confirmed:
            raise HTTPException(
                422,
                {
                    "code": "EVIDENCE_CHANGE_CONFIRMATION_REQUIRED",
                    "message": "a closed event requires confirmation of new or revised evidence",
                },
            )
        return envelope(
            request,
            evidence_agent.run(
                event_id,
                audit_write_confirmation={
                    "confirmed": True,
                    "evidence_change_confirmed": payload.evidence_change_confirmed,
                },
            ),
        )

    @application.post("/api/v1/events/{event_id}/human-override", dependencies=[Depends(require_admin)])
    def record_human_override(request: Request, event_id: str, payload: HumanOverrideRequest):
        event_or_404(event_id)
        actor, reason = validate_human_override_attribution(payload)
        decisions = operations.agent_decisions(event_id, limit=1)
        if not decisions:
            raise HTTPException(
                409,
                {
                    "code": "AGENT_DECISION_REQUIRED",
                    "message": "run the structured Evidence Agent before recording a human override",
                },
            )
        decision = decisions[0]
        # Re-read the ledger immediately before the immutable override write.
        # An agent decision is scoped to both the canonical event version and
        # the exact evidence receipt it observed.  A reviewer must never attach
        # an override to a decision whose evidence has since changed.
        current_detail = event_or_404(event_id)
        current_event_version = int(
            (current_detail.get("event") or {}).get("current_version") or 0
        )
        current_evidence_fingerprint = evidence_receipt_fingerprint(
            current_event_version,
            ledger.event_evidence(event_id),
        )
        decision_output = decision.get("output")
        decision_output = decision_output if isinstance(decision_output, dict) else {}
        try:
            decision_event_version = int(decision_output.get("event_version"))
        except (TypeError, ValueError):
            decision_event_version = None
        decision_evidence_fingerprint = decision_output.get("evidence_receipt_fingerprint")
        if (
            decision_event_version != current_event_version
            or not isinstance(decision_evidence_fingerprint, str)
            or decision_evidence_fingerprint != current_evidence_fingerprint
        ):
            raise HTTPException(
                409,
                {
                    "code": "STALE_AGENT_DECISION",
                    "message": "event or evidence changed after the latest agent decision; rerun the Evidence Agent",
                    "details": {
                        "decision_id": decision["decision_id"],
                        "decision_event_version": decision_event_version,
                        "current_event_version": current_event_version,
                        "receipt_matches": False,
                    },
                },
            )
        override_id = operations.record_human_override(
            event_id,
            decision["decision_id"],
            actor=actor,
            reason=reason,
            before={"review_status": decision["status"], "trace_id": decision["trace_id"]},
            after={
                "review_status": payload.review_status,
                "reviewer_attestation": payload.reviewer_attestation,
            },
        )
        return envelope(
            request,
            {
                "override_id": override_id,
                "event_id": event_id,
                "decision_id": decision["decision_id"],
                "review_status": payload.review_status,
                "no_trading": True,
            },
        )

    @application.get("/api/v1/model/status")
    def model_status(request: Request):
        data = router.status()
        data["recent_runs"] = operations.model_runs(limit=20)
        return envelope(request, data)

    @application.get("/api/v1/adjudication/status")
    def adjudication_status(request: Request):
        report = adjudication.pre_freeze_report()
        report.pop("annotations", None)
        return envelope(request, report)

    @application.post(
        "/api/v1/adjudication/samples/from-event/{event_id}",
        dependencies=[Depends(require_admin)],
    )
    def create_adjudication_sample(request: Request, event_id: str):
        event_or_404(event_id)
        try:
            return envelope(request, adjudication.create_sample_from_event(event_id))
        except ValueError as exc:
            raise HTTPException(
                409,
                {"code": "ADJUDICATION_SAMPLE_REJECTED", "message": str(exc)},
            ) from exc

    @application.get(
        "/api/v1/adjudication/queue",
        dependencies=[Depends(require_admin)],
    )
    def adjudication_queue(
        request: Request,
        reviewer_id: str = Query(min_length=2, max_length=80),
        role: Literal["REVIEWER", "ARBITER"] = "REVIEWER",
        limit: int = Query(50, ge=1, le=200),
    ):
        try:
            return envelope(
                request,
                adjudication.queue(reviewer_id, role=role, limit=limit),
            )
        except ValueError as exc:
            raise HTTPException(
                422,
                {"code": "ADJUDICATION_QUEUE_INVALID", "message": str(exc)},
            ) from exc

    @application.post(
        "/api/v1/adjudication/samples/{sample_id}/reviews",
        dependencies=[Depends(require_admin)],
    )
    def submit_adjudication_review(
        request: Request,
        sample_id: str,
        payload: AdjudicationReviewRequest,
    ):
        try:
            result = adjudication.submit_review(
                sample_id,
                reviewer_id=payload.reviewer_id,
                role=payload.role,
                materiality=payload.materiality,
                polarity=payload.polarity,
                evidence_state=payload.evidence_state,
                rationale=payload.rationale,
            )
            return envelope(request, result)
        except KeyError as exc:
            raise HTTPException(
                404,
                {"code": "ADJUDICATION_SAMPLE_NOT_FOUND", "message": f"sample not found: {sample_id}"},
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                409,
                {"code": "ADJUDICATION_REVIEW_REJECTED", "message": str(exc)},
            ) from exc

    @application.get("/api/v1/replays")
    def replay_cases(request: Request):
        return envelope(request, {"items": replay.cases(), "recent_runs": operations.replay_runs()})

    @application.post("/api/v1/replays/{case_id}/run", dependencies=[Depends(require_admin)])
    def replay_run(request: Request, case_id: str):
        try:
            operations.set_demo_mode("REPLAY")
            return envelope(request, replay.run(case_id))
        except ReplayCaseNotFound as exc:
            raise HTTPException(404, {"code": "REPLAY_CASE_NOT_FOUND", "message": f"replay case not found: {case_id}"}) from exc

    @application.post("/api/v1/replays/{case_id}/reset", dependencies=[Depends(require_admin)])
    def replay_reset(request: Request, case_id: str):
        try:
            return envelope(request, {"case_id": case_id, "deleted_runs": replay.reset(case_id)})
        except ReplayCaseNotFound as exc:
            raise HTTPException(404, {"code": "REPLAY_CASE_NOT_FOUND", "message": f"replay case not found: {case_id}"}) from exc

    @application.get("/api/v1/demo/mode")
    def get_demo_mode(request: Request):
        return envelope(request, {"mode": operations.demo_mode(settings.demo_mode)})

    @application.post("/api/v1/demo/mode/{mode}", dependencies=[Depends(require_admin)])
    def set_demo_mode(request: Request, mode: str):
        try:
            return envelope(request, {"mode": operations.set_demo_mode(mode)})
        except ValueError as exc:
            raise HTTPException(422, {"code": "INVALID_DEMO_MODE", "message": str(exc)}) from exc

    return application


app = create_app()
