from __future__ import annotations

import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Callable, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import __version__
from app.config import Settings
from app.models import RiskRouter
from app.services import AdjudicationService, EvidenceAgent, LocalEvidenceModelProvider, ReplayService
from app.services.replay import ReplayCaseNotFound
from app.storage import EvidenceObjectStore, LedgerRepository, OperationsRepository


API_SCHEMA_VERSION = "1.1"


class HumanOverrideRequest(BaseModel):
    actor: str = Field(min_length=2, max_length=80)
    reason: str = Field(min_length=8, max_length=1000)
    review_status: Literal["HUMAN_REVIEW", "INSUFFICIENT", "REVIEWED_NO_CHANGE"]


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


def generated_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def elapsed_seconds(start: str | None, end: str | None = None) -> float | None:
    if not start:
        return None
    try:
        start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_at = (
            datetime.fromisoformat(end.replace("Z", "+00:00"))
            if end
            else datetime.now(timezone.utc)
        )
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
        if settings.admin_token and x_admin_token != settings.admin_token:
            raise HTTPException(403, {"code": "ADMIN_TOKEN_REQUIRED", "message": "valid X-Admin-Token required"})

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
            ledger_health = ledger.health()
            ops_health = operations.health()
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
        data = ledger.overview()
        data["demo_mode"] = operations.demo_mode(settings.demo_mode)
        latest_worker = operations.latest_worker_cycle()
        data["latest_worker_cycle"] = latest_worker
        data["latest_backup"] = operations.latest_backup()
        data["timing"] = {
            "latest_event_age_seconds": elapsed_seconds(data.get("last_event_update")),
            "worker_cycle_duration_seconds": elapsed_seconds(
                latest_worker.get("started_at") if latest_worker else None,
                latest_worker.get("finished_at") if latest_worker else None,
            ),
            "latest_worker_finished_at": latest_worker.get("finished_at") if latest_worker else None,
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
        family: str | None = None,
        source: str | None = None,
        q: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        return envelope(
            request,
            ledger.list_events(
                status=status,
                family=family,
                source=source,
                query=q,
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
        text = "\n".join(
            [
                data["event"].get("company_name") or "",
                data["event"].get("event_family") or "",
                data["event"].get("event_type") or "",
                facts.get("evidence_summary") or "",
                *[(item.get("evidence_passage") or "") for item in evidence[:5]],
            ]
        )
        data["evidence_count"] = len(evidence)
        data["model_shadow_output"] = router.predict(text)
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
        data["evidence_objects"] = operations.evidence_objects(event_id)
        for item in data["evidence_objects"]:
            item["integrity_verified"] = evidence_object_store.verify(
                item["relative_path"], item["object_sha256"]
            )
        return envelope(request, data)

    @application.post("/api/v1/events/{event_id}/agent/run", dependencies=[Depends(require_admin)])
    def run_evidence_agent(request: Request, event_id: str):
        event_or_404(event_id)
        return envelope(request, evidence_agent.run(event_id))

    @application.post("/api/v1/events/{event_id}/human-override", dependencies=[Depends(require_admin)])
    def record_human_override(request: Request, event_id: str, payload: HumanOverrideRequest):
        event_or_404(event_id)
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
        override_id = operations.record_human_override(
            event_id,
            decision["decision_id"],
            actor=payload.actor,
            reason=payload.reason,
            before={"review_status": decision["status"], "trace_id": decision["trace_id"]},
            after={"review_status": payload.review_status},
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
