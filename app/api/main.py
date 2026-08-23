from __future__ import annotations

import ipaddress
import hashlib
import secrets
import time
import uuid
from copy import deepcopy
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from itertools import islice
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app import __version__
from app.api.overview_projection import build_overview_payload
from app.api.snapshot import PrecomputedSnapshot, PublishedSnapshot, SnapshotUnavailable
from app.config import Settings
from app.models import RiskRouter, derive_evidence_context
from app.services import (
    CAPTURE_INTERPRETATION_CONTRACT,
    CAPTURE_INTERPRETATION_PROMPT_SHA256,
    AdjudicationService,
    EvidenceAgent,
    LocalEvidenceModelProvider,
    ReplayService,
    capture_source_text,
    deterministic_interpretation,
    evidence_receipt_fingerprint,
    knowledge_context,
    normalized_capture_input,
    validate_interpretation_result,
)
from app.services.capture_interpretation import CAPTURE_INTERPRETATION_PROMPT_VERSION
from app.services.replay import ReplayCaseNotFound
from app.storage import EvidenceObjectStore, LedgerRepository, OperationsRepository


API_SCHEMA_VERSION = "1.1"
BACKUP_SNAPSHOT_MAX_AGE_SECONDS = 36 * 60 * 60
OVERVIEW_SNAPSHOT_REFRESH_SECONDS = 30.0
OVERVIEW_PUBLISHED_SNAPSHOT_REFRESH_SECONDS = 5 * 60.0
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


def _rate_limit_client_key(request: Request, trusted_proxy_hosts: tuple[str, ...]) -> str:
    """Return a bounded-rate key without trusting caller-controlled proxy headers.

    The production API listens on loopback.  Only a connection that actually
    arrives from a configured proxy host may supply ``X-Real-IP``; direct
    callers cannot manufacture new buckets with ``X-Forwarded-For``.
    """

    client_host = request.client.host if request.client else "unknown"
    if client_host not in trusted_proxy_hosts:
        return client_host
    real_ip = request.headers.get("X-Real-IP", "").strip()
    if not real_ip:
        return client_host
    try:
        return str(ipaddress.ip_address(real_ip))
    except ValueError:
        return client_host


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


class CaptureInterpretationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_write_confirmed: Literal[True]
    mode: Literal["DETERMINISTIC"] = "DETERMINISTIC"


class AdjudicationReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


def public_verification_method(
    facts: Any,
    evidence: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Expose a verification receipt without reviewer or authorization secrets.

    A dual-human label is public only while its selected evidence remains the
    current, reader-eligible receipt-bound relation. The public summary omits
    reviewer identities, submission hashes, rationales, and authorization data.
    """

    if not isinstance(facts, dict):
        return None
    dual_review = facts.get("dual_human_fact_review")
    if isinstance(dual_review, dict) and dual_review.get("target_status") == "verified":
        human_claim = facts.get("human_fact_claim")
        selected_evidence_id = str(dual_review.get("selected_evidence_id") or "")
        selected = next(
            (
                item
                for item in evidence
                if str(item.get("evidence_id") or "") == selected_evidence_id
            ),
            None,
        )
        reviewers = dual_review.get("reviewers")
        reviewer_values = (
            list(reviewers.values())
            if isinstance(reviewers, dict)
            else reviewers
            if isinstance(reviewers, list)
            else []
        )
        independent_reviews = (
            len(
                {
                    str(reviewer).strip()
                    for reviewer in reviewer_values
                    if str(reviewer).strip()
                }
            )
        )
        if (
            selected is not None
            and selected_evidence_id
            and dual_review.get("contract_version") == "event-fact-review-v2"
            and isinstance(human_claim, dict)
            and human_claim.get("contract_version") == "human-fact-claim-v1"
            and dual_review.get("canonical_claim_sha256")
            == human_claim.get("canonical_claim_sha256")
            and dual_review.get("public_fact_summary_sha256")
            == human_claim.get("public_fact_summary_sha256")
            and independent_reviews == 2
            and str(selected.get("evidence_status") or "")
            == "accepted_dual_human_primary_evidence"
            and str(selected.get("relation_status") or "") == "HUMAN_CONFIRMED"
            and int(selected.get("subject_match") or 0) == 1
            and int(selected.get("event_claim_supported") or 0) == 1
            and int(selected.get("date_coherent") or 0) == 1
            and int(selected.get("dual_human_receipt_consistent") or 0) == 1
            and int(selected.get("reader_eligible") or 0) == 1
        ):
            return {
                "kind": "dual_human_fact_review",
                "version": dual_review.get("contract_version"),
                "reviewed_at": dual_review.get("applied_at"),
                "evidence_ids": [selected_evidence_id],
                "independent_reviews": independent_reviews,
                "no_trading": True,
            }
    light_verification = facts.get("light_verification")
    if isinstance(light_verification, dict):
        evidence_ids = light_verification.get("evidence_ids")
        return {
            "kind": "light_verification",
            "version": light_verification.get("version"),
            "reviewed_at": light_verification.get("reviewed_at"),
            "evidence_ids": evidence_ids if isinstance(evidence_ids, list) else [],
            "score": light_verification.get("score"),
            "no_trading": True,
        }
    return None


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

    if settings.overview_snapshot_path is not None:
        # Production never performs the expensive ledger aggregation inside the
        # API interpreter.  A bounded systemd oneshot publishes this file and
        # the request process only loads complete atomic generations.
        overview_snapshot = PublishedSnapshot(
            settings.overview_snapshot_path,
            refresh_interval_seconds=OVERVIEW_PUBLISHED_SNAPSHOT_REFRESH_SECONDS,
            name="overview",
        )
    else:
        # Small local/test ledgers retain a zero-configuration fallback.
        overview_snapshot = PrecomputedSnapshot(
            lambda: build_overview_payload(
                settings,
                ledger=ledger,
                operations=operations,
            ),
            refresh_interval_seconds=OVERVIEW_SNAPSHOT_REFRESH_SECONDS,
            name="overview",
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        overview_snapshot.start()
        try:
            yield
        finally:
            overview_snapshot.stop()

    application = FastAPI(
        title="Finance Radar Read-Mostly API",
        version=__version__,
        description="Evidence-linked financial event intelligence. No trading endpoints exist.",
        lifespan=lifespan,
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
    application.state.overview_snapshot = overview_snapshot

    rate_buckets: OrderedDict[str, deque[float]] = OrderedDict()
    rate_lock = Lock()
    read_cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
    read_cache_lock = Lock()
    application.state.rate_buckets = rate_buckets
    application.state.rate_bucket_limit = settings.api_rate_limit_max_clients

    def cached_read(key: str, ttl_seconds: float, factory: Callable[[], Any]) -> Any:
        """Bound repeated public aggregate cost without persisting stale truth."""

        now = time.monotonic()
        with read_cache_lock:
            cached = read_cache.get(key)
            if cached and cached[0] > now:
                read_cache.move_to_end(key)
                return deepcopy(cached[1])
        value = factory()
        with read_cache_lock:
            read_cache[key] = (now + max(1.0, float(ttl_seconds)), deepcopy(value))
            read_cache.move_to_end(key)
            while len(read_cache) > 32:
                read_cache.popitem(last=False)
        return value

    @application.middleware("http")
    async def trace_middleware(request: Request, call_next: Callable[..., Any]):
        request.state.trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        limit = settings.api_rate_limit_per_minute
        rate_remaining: int | None = None
        if limit > 0 and request.url.path.startswith("/api/"):
            key = _rate_limit_client_key(request, settings.api_trusted_proxy_hosts)
            now = time.monotonic()
            with rate_lock:
                # Opportunistically retire the oldest expired keys.  The hard
                # cardinality cap below is the final memory-safety boundary.
                for stale_key in tuple(islice(rate_buckets, 64)):
                    stale_bucket = rate_buckets[stale_key]
                    while stale_bucket and now - stale_bucket[0] >= 60:
                        stale_bucket.popleft()
                    if not stale_bucket:
                        del rate_buckets[stale_key]
                bucket = rate_buckets.get(key)
                if bucket is None:
                    while len(rate_buckets) >= settings.api_rate_limit_max_clients:
                        rate_buckets.popitem(last=False)
                    bucket = deque()
                    rate_buckets[key] = bucket
                else:
                    rate_buckets.move_to_end(key)
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
        if not secrets.compare_digest(x_admin_token or "", settings.admin_token):
            raise HTTPException(403, {"code": "ADMIN_TOKEN_REQUIRED", "message": "valid X-Admin-Token required"})

    def require_reviewer(
        x_reviewer_token: str | None = Header(default=None),
        x_admin_token: str | None = Header(default=None),
    ) -> None:
        if settings.admin_token and secrets.compare_digest(x_admin_token or "", settings.admin_token):
            return
        if any(
            secrets.compare_digest(x_reviewer_token or "", token)
            for _principal_id, _role, token in settings.reviewer_principals
        ):
            return
        if not settings.reviewer_token:
            if settings.admin_token:
                raise HTTPException(
                    403,
                    {"code": "REVIEWER_TOKEN_REQUIRED", "message": "valid reviewer or admin token required"},
                )
            raise HTTPException(
                503,
                {
                    "code": "REVIEWER_MUTATIONS_DISABLED",
                    "message": "reviewer token is not configured; reviewer operations are disabled",
                },
            )
        if not secrets.compare_digest(x_reviewer_token or "", settings.reviewer_token):
            raise HTTPException(
                403,
                {"code": "REVIEWER_TOKEN_REQUIRED", "message": "valid X-Reviewer-Token required"},
            )

    def internal_reader_access(
        x_reviewer_token: str | None = Header(default=None),
        x_admin_token: str | None = Header(default=None),
    ) -> bool:
        """Grant unfiltered reads only to an existing reviewer/admin credential.

        These endpoints remain publicly callable, so an absent or invalid
        credential deliberately falls back to the public reader view instead
        of turning a safe read into an authentication oracle.  This helper
        never grants mutation authority.
        """

        if settings.admin_token and secrets.compare_digest(
            x_admin_token or "", settings.admin_token
        ):
            return True
        supplied = x_reviewer_token or ""
        if any(
            secrets.compare_digest(supplied, token)
            for _principal_id, _role, token in settings.reviewer_principals
        ):
            return True
        return bool(
            settings.reviewer_token
            and secrets.compare_digest(supplied, settings.reviewer_token)
        )

    def require_bound_reviewer_principal(
        x_reviewer_token: str | None = Header(default=None),
    ) -> dict[str, str]:
        """Resolve one immutable human principal and role from its credential.

        Admin and legacy shared reviewer tokens intentionally cannot submit
        human labels. They may inspect readiness, but human independence must
        be established by separate credentials configured for each person.
        """

        if not settings.reviewer_principals:
            raise HTTPException(
                503,
                {
                    "code": "BOUND_REVIEWER_PRINCIPALS_DISABLED",
                    "message": "credential-bound human reviewer principals are not configured",
                },
            )
        supplied = x_reviewer_token or ""
        for principal_id, role, token in settings.reviewer_principals:
            if secrets.compare_digest(supplied, token):
                principal_hash = hashlib.sha256(
                    f"finance-radar-reviewer-principal-v1:{principal_id.casefold()}".encode("utf-8")
                ).hexdigest()
                return {
                    "principal_hash": principal_hash,
                    "principal_alias": f"human-{principal_hash[:10]}",
                    "role": role,
                }
        raise HTTPException(
            403,
            {
                "code": "BOUND_REVIEWER_PRINCIPAL_REQUIRED",
                "message": "a valid credential-bound reviewer token is required",
            },
        )

    def require_operator(
        x_operator_token: str | None = Header(default=None),
        x_admin_token: str | None = Header(default=None),
    ) -> None:
        if settings.admin_token and secrets.compare_digest(x_admin_token or "", settings.admin_token):
            return
        if not settings.operator_token:
            if settings.admin_token:
                raise HTTPException(
                    403,
                    {"code": "OPERATOR_TOKEN_REQUIRED", "message": "valid operator or admin token required"},
                )
            raise HTTPException(
                503,
                {
                    "code": "OPERATOR_MUTATIONS_DISABLED",
                    "message": "operator token is not configured; operator operations are disabled",
                },
            )
        if not secrets.compare_digest(x_operator_token or "", settings.operator_token):
            raise HTTPException(
                403,
                {"code": "OPERATOR_TOKEN_REQUIRED", "message": "valid X-Operator-Token required"},
            )

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

    def event_or_404(
        event_id: str,
        *,
        require_reader_ready: bool = False,
        allow_excluded_capture_archive: bool = False,
    ) -> dict[str, Any]:
        event = ledger.event_detail(event_id)
        if event is None:
            raise HTTPException(404, {"code": "EVENT_NOT_FOUND", "message": f"event not found: {event_id}"})
        public_event = event.get("event") or {}
        if require_reader_ready and int(public_event.get("reader_ready") or 0) != 1:
            archive_allowed = (
                allow_excluded_capture_archive
                and str(public_event.get("public_state") or "") == "excluded"
                and int(public_event.get("captured_source_count") or 0) > 0
            )
            if not archive_allowed:
                raise HTTPException(
                    404,
                    {"code": "EVENT_NOT_FOUND", "message": f"event not found: {event_id}"},
                )
        return event

    public_event_fields = (
        "event_id",
        "current_version",
        "status",
        "public_state",
        "event_family",
        "event_type",
        "event_date",
        "first_seen_at",
        "last_updated_at",
        "ticker_at_event",
        "company_name",
        "discovery_source",
        "reviewed_at",
        "captured_source_count",
        "citable_evidence_count",
        "public_fact_summary",
        "claim_subject",
        "claim_action",
        "claim_stage",
        "known_at",
        "reader_ready",
        "no_trading",
    )
    public_evidence_fields = (
        "evidence_id",
        "evidence_url",
        "evidence_passage",
        "filing_date",
        "form",
        "source_name",
        "authority_tier",
        "source_type",
        "source_published_at",
        "local_received_at",
        "evidence_status",
        "relation_status",
        "subject_match",
        "event_claim_supported",
        "date_coherent",
        "dual_human_receipt_consistent",
        "reader_eligible",
    )

    def public_event_item(value: dict[str, Any]) -> dict[str, Any]:
        return {key: value.get(key) for key in public_event_fields}

    def public_evidence_item(value: dict[str, Any]) -> dict[str, Any]:
        return {key: value.get(key) for key in public_evidence_fields}

    def public_captured_source(value: dict[str, Any]) -> dict[str, Any]:
        """Expose a bounded discovery receipt without calling it evidence."""

        def normalized_text(raw: Any) -> str:
            normalized = " ".join(str(raw or "").split())
            return normalized

        def bounded_text(raw: Any, limit: int) -> str | None:
            normalized = normalized_text(raw)
            if not normalized:
                return None
            return (
                normalized
                if len(normalized) <= limit
                else normalized[: limit - 1].rstrip() + "…"
            )

        source_url = str(value.get("canonical_url") or "").strip()
        try:
            parsed_url = urlsplit(source_url)
        except ValueError:
            parsed_url = None
        if (
            parsed_url is None
            or parsed_url.scheme.lower() not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
        ):
            source_url = ""
        elif parsed_url.hostname.casefold() in {"localhost", "metadata.google.internal"}:
            source_url = ""
        else:
            try:
                source_address = ipaddress.ip_address(parsed_url.hostname)
            except ValueError:
                source_address = None
            if source_address is not None and not source_address.is_global:
                source_url = ""
        excerpt_raw = normalized_text(value.get("summary"))
        return {
            "source_name": value.get("source_name"),
            "source_type": value.get("source_type"),
            "authority_tier": value.get("authority_tier"),
            "source_title": bounded_text(value.get("title"), 500),
            "source_excerpt": bounded_text(value.get("summary"), 1200),
            "source_excerpt_original_length": len(excerpt_raw),
            "source_excerpt_truncated": len(excerpt_raw) > 1200,
            "source_url": source_url or None,
            "source_published_at": value.get("source_published_at"),
            "local_received_at": value.get("local_received_at"),
            "latest_revision_no": value.get("latest_revision_no"),
            "latest_revision_kind": value.get("latest_revision_kind"),
            "capture_receipt_sha256": value.get("capture_receipt_sha256"),
            "capture_status": (
                "FILTERED_DISCOVERY"
                if value.get("relation_type") == "filtered_aggregated_noise"
                else "CAPTURED_DISCOVERY"
            ),
            "is_citable_evidence": False,
            "formal_verification": False,
            "no_trading": True,
        }

    public_capture_interpretation_fields = (
        "contract_version",
        "event_id",
        "bound_event_version",
        "capture_receipt_sha256",
        "source_revision_no",
        "bound_content_sha256",
        "status",
        "mode",
        "generated_at",
        "source_language",
        "coverage",
        "one_line_zh",
        "what_source_says",
        "what_source_does_not_prove_zh",
        "actors",
        "affected_assets",
        "modality",
        "why_current_state_zh",
        "missing_to_change_state_zh",
        "prompt_injection_suspected",
        "persisted",
        "external_generation_state",
        "safety",
    )

    def public_capture_interpretation(value: dict[str, Any]) -> dict[str, Any]:
        return {key: value.get(key) for key in public_capture_interpretation_fields}

    def public_worker_cycle(value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Expose only the user-facing state and timing of one worker cycle."""

        if value is None:
            return None
        started_at = value.get("started_at")
        finished_at = value.get("finished_at")
        return {
            "status": value.get("status"),
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_seconds": elapsed_seconds(started_at, finished_at),
        }

    def public_model_status(value: dict[str, Any]) -> dict[str, Any]:
        """Return model availability and gate posture without diagnostics."""

        structured = value.get("structured_evidence_gate")
        semantic = value.get("semantic_policy_gate")
        operational = value.get("operational_scope_gate")
        structured = structured if isinstance(structured, dict) else {}
        semantic = semantic if isinstance(semantic, dict) else {}
        operational = operational if isinstance(operational, dict) else {}
        return {
            "status": value.get("status"),
            "model_version": value.get("model_version"),
            "architecture": value.get("architecture"),
            "structured_evidence_gate": {
                "version": structured.get("version"),
                "required_for_v4": structured.get("required_for_v4"),
            },
            "semantic_policy_gate": {
                "version": semantic.get("version"),
                "enforced_for_v4": semantic.get("enforced_for_v4"),
            },
            "operational_scope_gate": {
                "version": operational.get("version"),
                "enforced": operational.get("enforced"),
            },
            "shadow": bool(value.get("shadow", True)),
            "no_trading": bool(value.get("no_trading", True)),
        }

    def public_source_health(value: dict[str, Any]) -> dict[str, Any]:
        """Return the public source label and last successful collection state."""

        return {
            "name": value.get("name"),
            "source_type": value.get("source_type"),
            "authority_tier": value.get("authority_tier"),
            "status": value.get("cursor_status"),
            "last_success_at": value.get("last_success_at"),
        }

    def public_market_capabilities(value: dict[str, Any]) -> dict[str, Any]:
        """Expose provider availability without jobs, errors or routing details."""

        raw_providers = value.get("providers")
        raw_providers = raw_providers if isinstance(raw_providers, list) else []
        providers = []
        for raw_provider in raw_providers:
            if not isinstance(raw_provider, dict):
                continue
            providers.append(
                {
                    "provider_id": raw_provider.get("provider_id"),
                    "name": raw_provider.get("name"),
                    "status": raw_provider.get("status"),
                    "freshness_status": raw_provider.get("freshness_status"),
                    "last_snapshot_at": raw_provider.get("last_snapshot_at"),
                    "read_only": bool(raw_provider.get("read_only")),
                    "order_endpoints_present": bool(
                        raw_provider.get("order_endpoints_present")
                    ),
                }
            )
        raw_boundary = value.get("boundary")
        raw_boundary = raw_boundary if isinstance(raw_boundary, dict) else {}
        return {
            "providers": providers,
            "boundary": {
                "read_only": bool(raw_boundary.get("read_only")),
                "no_trading": bool(raw_boundary.get("no_trading")),
                "post_event_audit_only": bool(
                    raw_boundary.get("post_event_audit_only")
                ),
            },
        }

    public_replay_observation_fields = (
        "at_seconds",
        "source",
        "authority_tier",
        "title",
        "passage",
        "contradicts",
        "revision_kind",
    )

    def public_replay_case(value: dict[str, Any]) -> dict[str, Any]:
        """Keep only the frozen teaching material consumed by Replay Lab."""

        raw_observations = value.get("observations")
        raw_observations = (
            raw_observations if isinstance(raw_observations, list) else []
        )
        return {
            "case_id": value.get("case_id"),
            "display_name": value.get("title"),
            "display_description": value.get("description"),
            "observations": [
                {
                    key: observation.get(key)
                    for key in public_replay_observation_fields
                }
                for observation in raw_observations
                if isinstance(observation, dict)
            ],
        }

    def public_replay_run(
        value: dict[str, Any],
        *,
        display_names: dict[str, Any],
    ) -> dict[str, Any]:
        """Describe a replay outcome without its result, error or internal id."""

        case_id = str(value.get("case_id") or "")
        raw_status = str(value.get("status") or "").upper()
        summaries = {
            "RUNNING": "Replay is running.",
            "COMPLETED": "Replay completed successfully.",
            "FAILED": "Replay did not complete; details are available to reviewers.",
        }
        status = raw_status if raw_status in summaries else "UNKNOWN"
        return {
            "case_id": case_id,
            "display_name": display_names.get(case_id) or "Replay case",
            "status": status,
            "started_at": value.get("started_at"),
            "finished_at": value.get("finished_at"),
            "summary": summaries.get(status, "Replay status is unavailable."),
        }

    def reader_scoped_evidence(
        event_id: str,
        *,
        internal_reader: bool,
    ) -> list[dict[str, Any]]:
        items = ledger.event_evidence(event_id)
        if internal_reader:
            return items
        return [
            public_evidence_item(item)
            for item in items
            if int(item.get("reader_eligible") or 0) == 1
        ]

    def public_event_detail(
        value: dict[str, Any],
        *,
        evidence: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return only fields consumed by the public event dossier.

        The repository detail object also carries reviewer workflow, market
        job errors, receipts and internal model diagnostics.  Those remain
        available to an authenticated reviewer/admin but must not cross the
        public read boundary.
        """

        raw_event = value.get("event") if isinstance(value.get("event"), dict) else {}
        version = (
            value.get("current_version")
            if isinstance(value.get("current_version"), dict)
            else {}
        )
        facts = version.get("facts") if isinstance(version.get("facts"), dict) else {}
        public_fact_fields = (
            "public_fact_summary",
            "claim_subject",
            "claim_action",
            "claim_stage",
            "known_at",
        )
        result: dict[str, Any] = {
            "event": public_event_item(raw_event),
            "current_version": {
                "version": version.get("version"),
                "facts": {
                    key: facts.get(key)
                    for key in public_fact_fields
                    if key in facts
                },
            },
            "preferred_source": {
                "source_published_at": (
                    value.get("preferred_source") or {}
                ).get("source_published_at")
            },
            "evidence_count": len(evidence),
            "no_trading_banner": value.get("no_trading_banner"),
        }
        verification = value.get("verification_method")
        if isinstance(verification, dict):
            eligible_ids = {
                str(item.get("evidence_id") or "")
                for item in evidence
                if str(item.get("evidence_id") or "")
            }
            allowed_verification_fields = (
                "kind",
                "version",
                "reviewed_at",
                "score",
                "independent_reviews",
                "no_trading",
            )
            public_verification = {
                key: verification.get(key)
                for key in allowed_verification_fields
                if key in verification
            }
            public_verification["evidence_ids"] = [
                evidence_id
                for evidence_id in verification.get("evidence_ids", [])
                if str(evidence_id) in eligible_ids
            ]
            result["verification_method"] = public_verification
        return result

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

    @application.get("/api/v1/live")
    def live(request: Request):
        """Return process liveness without touching either production database."""
        return envelope(
            request,
            {
                "status": "ok",
                "service": "finance-radar-api",
                "service_version": __version__,
                "database_checks": "not_run",
                "boundary": "process-liveness-only",
            },
        )

    @application.get("/api/v1/health")
    def health(
        request: Request,
        internal_reader: bool = Depends(internal_reader_access),
    ):
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
            if not internal_reader:
                model_health = public_model_status(model_health)
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
    def overview(
        request: Request,
        internal_reader: bool = Depends(internal_reader_access),
    ):
        try:
            snapshot_data, overview_snapshot_status = overview_snapshot.read()
        except SnapshotUnavailable as exc:
            raise HTTPException(
                503,
                {
                    "code": "OVERVIEW_SNAPSHOT_UNAVAILABLE",
                    "message": "overview snapshot has not completed successfully yet",
                },
            ) from exc
        overview_base = snapshot_data["overview_base"]
        latest_backup = snapshot_data["latest_verified_backup"]
        data = health_from_latest_verified_backup(
            overview_base,
            latest_backup,
        )
        if not internal_reader:
            data["recent_events"] = [
                public_event_item(item) for item in data.get("recent_events", [])
            ]
            data["source_health"] = [
                public_source_health(item) for item in data.get("source_health", [])
            ]
        data["demo_mode"] = snapshot_data["demo_mode"]
        latest_worker = snapshot_data["latest_worker_cycle"]
        latest_successful_worker = snapshot_data["latest_successful_worker_cycle"]
        data["latest_worker_cycle"] = (
            latest_worker if internal_reader else public_worker_cycle(latest_worker)
        )
        data["latest_backup"] = public_backup_status(latest_backup)
        data["latest_backup_attempt"] = public_backup_status(
            snapshot_data["latest_backup_attempt"]
        )
        data["overview_snapshot"] = overview_snapshot_status
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

    @application.get("/api/v1/product/metrics")
    def product_metrics(request: Request, window_days: int = Query(30, ge=1, le=365)):
        return envelope(request, ledger.product_metrics(window_days=window_days))

    @application.get("/api/v1/sources/health")
    def sources_health(
        request: Request,
        internal_reader: bool = Depends(internal_reader_access),
    ):
        items = ledger.list_source_health()
        if not internal_reader:
            items = [public_source_health(item) for item in items]
        return envelope(request, {"items": items})

    @application.get("/api/v1/market/capabilities")
    def market_capabilities(
        request: Request,
        internal_reader: bool = Depends(internal_reader_access),
    ):
        data = ledger.market_capabilities()
        if not internal_reader:
            data = public_market_capabilities(data)
        return envelope(request, data)

    @application.get("/api/v1/evidence/archive", dependencies=[Depends(require_operator)])
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
        reader_ready: bool | None = None,
        sort: Literal["latest", "event_date", "subject"] = "event_date",
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        internal_reader: bool = Depends(internal_reader_access),
    ):
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                422,
                {
                    "code": "INVALID_DATE_RANGE",
                    "message": "date_from must not be after date_to",
                },
            )
        public_excluded_archive = not internal_reader and public_state == "excluded"
        effective_reader_ready = (
            reader_ready
            if internal_reader
            else None
            if public_excluded_archive
            else True
        )
        data = ledger.list_events(
            status=status,
            public_state=public_state,
            family=family,
            source=source,
            query=q,
            date_from=date_from.isoformat() if date_from else None,
            date_to=date_to.isoformat() if date_to else None,
            reader_ready=effective_reader_ready,
            captured_source_required=public_excluded_archive,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        if not internal_reader:
            data["items"] = [public_event_item(item) for item in data["items"]]
        return envelope(request, data)

    @application.get("/api/v1/events/facets")
    def event_facets(
        request: Request,
        reader_ready: bool | None = None,
        internal_reader: bool = Depends(internal_reader_access),
    ):
        effective_reader_ready = reader_ready if internal_reader else True
        cache_key = f"event-facets-v1:{effective_reader_ready!r}"
        data = cached_read(
            cache_key,
            60.0,
            lambda: ledger.event_facets(reader_ready=effective_reader_ready),
        )
        return envelope(request, data)

    @application.get("/api/v1/events/{event_id}")
    def event_detail(
        request: Request,
        event_id: str,
        internal_reader: bool = Depends(internal_reader_access),
    ):
        data = event_or_404(
            event_id,
            require_reader_ready=not internal_reader,
            allow_excluded_capture_archive=not internal_reader,
        )
        evidence = reader_scoped_evidence(event_id, internal_reader=internal_reader)
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
        verification_method = public_verification_method(facts, evidence)
        if verification_method is not None:
            data["verification_method"] = verification_method
        data["no_trading_banner"] = "Intelligence and review only. No execution capability is present."
        if internal_reader:
            evidence_context = derive_evidence_context(evidence)
            data["model_shadow_output"] = router.predict(
                text,
                evidence_context=evidence_context,
            )
            data["model_input_contract"] = {
                "uses_source_content": True,
                "uses_evidence_passages": True,
                "uses_structured_evidence_state": True,
                "excludes_event_taxonomy_shortcuts": True,
                "shadow_only": True,
            }
        else:
            data = public_event_detail(data, evidence=evidence)
        return envelope(request, data)

    @application.get("/api/v1/events/{event_id}/knowledge")
    def event_knowledge(event_id: str, request: Request):
        data = event_or_404(
            event_id,
            require_reader_ready=True,
            allow_excluded_capture_archive=True,
        )
        event = data.get("event") or {}
        return envelope(
            request,
            knowledge_context(
                str(event.get("event_family") or ""),
                str(event.get("event_type") or ""),
            ),
        )

    @application.get(
        "/api/v1/events/{event_id}/timeline",
        dependencies=[Depends(require_reviewer)],
    )
    def event_timeline(request: Request, event_id: str):
        event_or_404(event_id)
        return envelope(request, {"items": ledger.event_timeline(event_id)})

    @application.get("/api/v1/events/{event_id}/evidence")
    def event_evidence(
        request: Request,
        event_id: str,
        internal_reader: bool = Depends(internal_reader_access),
    ):
        event_or_404(
            event_id,
            require_reader_ready=not internal_reader,
            allow_excluded_capture_archive=not internal_reader,
        )
        return envelope(
            request,
            {"items": reader_scoped_evidence(event_id, internal_reader=internal_reader)},
        )

    @application.get("/api/v1/events/{event_id}/sources")
    def event_captured_sources(
        request: Request,
        event_id: str,
        internal_reader: bool = Depends(internal_reader_access),
    ):
        event_or_404(
            event_id,
            require_reader_ready=not internal_reader,
            allow_excluded_capture_archive=not internal_reader,
        )
        items = ledger.captured_sources(event_id)
        if not internal_reader:
            items = [
                public_captured_source(item)
                for item in items
                if item.get("observation_status") != "deleted"
            ]
        return envelope(
            request,
            {
                "items": items,
                "contract": {
                    "captured_source_is_not_evidence": True,
                    "canonical_state_unchanged": True,
                    "no_trading": True,
                },
            },
        )

    @application.get("/api/v1/events/{event_id}/source-interpretations")
    def event_source_interpretations(
        request: Request,
        event_id: str,
        internal_reader: bool = Depends(internal_reader_access),
    ):
        event_data = event_or_404(
            event_id,
            require_reader_ready=not internal_reader,
            allow_excluded_capture_archive=not internal_reader,
        )
        event = dict(event_data.get("event") or {})
        items: list[dict[str, Any]] = []
        for capture in ledger.captured_sources(event_id):
            if not internal_reader and capture.get("observation_status") == "deleted":
                continue
            receipt = str(capture.get("capture_receipt_sha256") or "")
            run = operations.latest_capture_interpretation(event_id, receipt) if receipt else None
            output = dict((run or {}).get("output") or {})
            try:
                if output:
                    validate_interpretation_result(output, capture_source_text(capture))
                else:
                    output = deterministic_interpretation(event, capture)
            except Exception:
                output = deterministic_interpretation(event, capture)
                output["status"] = "FAILED"
                output["one_line_zh"] = (
                    "缓存解读未通过当前合同；原始捕获仍可阅读，正式状态保持不变。"
                )
                output["persisted"] = False
                output["external_generation_state"] = "FAILED_VALIDATION"
            items.append(public_capture_interpretation(output))
        return envelope(
            request,
            {
                "items": items,
                "contract": {
                    "version": CAPTURE_INTERPRETATION_CONTRACT,
                    "advisory_only": True,
                    "canonical_mutation_allowed": False,
                    "used_as_model_feature": False,
                    "public_requests_are_cached_or_deterministic": True,
                    "external_provider_configured": False,
                    "no_trading": True,
                },
            },
        )

    @application.post(
        "/api/v1/events/{event_id}/sources/{observation_id}/interpret",
        dependencies=[Depends(require_operator)],
    )
    def run_source_interpretation(
        request: Request,
        event_id: str,
        observation_id: str,
        payload: CaptureInterpretationRunRequest,
    ):
        event_data = event_or_404(event_id)
        event = dict(event_data.get("event") or {})
        capture = next(
            (
                item
                for item in ledger.captured_sources(event_id)
                if str(item.get("observation_id") or "") == observation_id
            ),
            None,
        )
        if capture is None:
            raise HTTPException(
                404,
                {
                    "code": "CAPTURE_NOT_FOUND",
                    "message": "capture does not belong to this event",
                },
            )
        normalized = normalized_capture_input(event, capture)
        output = deterministic_interpretation(event, capture)
        interpretation_id, inserted = operations.enqueue_capture_interpretation(
            event_id,
            observation_id,
            normalized,
            contract_version=CAPTURE_INTERPRETATION_CONTRACT,
            prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
            prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
            provider="deterministic",
            model_snapshot="capture-rules-v1",
            external_call=False,
        )
        existing = operations.latest_capture_interpretation(
            event_id, str(capture.get("capture_receipt_sha256") or "")
        )
        if inserted or existing is None:
            operations.complete_capture_interpretation(
                interpretation_id,
                output,
                guardrails={
                    "source_text_untrusted": True,
                    "quote_substrings_validated": True,
                    "tools_allowed": False,
                    "canonical_mutation": False,
                    "used_as_model_feature": False,
                },
                usage={"input_tokens": 0, "output_tokens": 0, "estimated_usd": 0},
                latency_ms=0.0,
            )
        stored = operations.latest_capture_interpretation(
            event_id, str(capture.get("capture_receipt_sha256") or "")
        )
        return envelope(
            request,
            {
                "interpretation_id": interpretation_id,
                "created": inserted,
                "output": public_capture_interpretation(
                    dict((stored or {}).get("output") or output)
                ),
                "external_call": False,
                "estimated_usd": 0,
                "canonical_state_unchanged": True,
            },
        )

    @application.get("/api/v1/events/{event_id}/trace", dependencies=[Depends(require_reviewer)])
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

    @application.post("/api/v1/events/{event_id}/agent/run", dependencies=[Depends(require_operator)])
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

    @application.post("/api/v1/events/{event_id}/human-override", dependencies=[Depends(require_reviewer)])
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

    @application.get("/api/v1/model/status", dependencies=[Depends(require_operator)])
    def model_status(request: Request):
        data = router.status()
        data["recent_runs"] = operations.model_runs(limit=20)
        return envelope(request, data)

    @application.get("/api/v1/adjudication/status", dependencies=[Depends(require_reviewer)])
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
    )
    def adjudication_queue(
        request: Request,
        limit: int = Query(50, ge=1, le=200),
        principal: dict[str, str] = Depends(require_bound_reviewer_principal),
    ):
        try:
            return envelope(
                request,
                adjudication.queue(
                    principal["principal_hash"],
                    role=principal["role"],
                    limit=limit,
                    principal_alias=principal["principal_alias"],
                ),
            )
        except ValueError as exc:
            raise HTTPException(
                422,
                {"code": "ADJUDICATION_QUEUE_INVALID", "message": str(exc)},
            ) from exc

    @application.post(
        "/api/v1/adjudication/samples/{sample_id}/reviews",
    )
    def submit_adjudication_review(
        request: Request,
        sample_id: str,
        payload: AdjudicationReviewRequest,
        principal: dict[str, str] = Depends(require_bound_reviewer_principal),
    ):
        try:
            result = adjudication.submit_review(
                sample_id,
                reviewer_id=principal["principal_hash"],
                role=principal["role"],
                materiality=payload.materiality,
                polarity=payload.polarity,
                evidence_state=payload.evidence_state,
                rationale=payload.rationale,
            )
            result["reviewer_principal"] = principal["principal_alias"]
            result["credential_bound"] = True
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
    def replay_cases(
        request: Request,
        internal_reader: bool = Depends(internal_reader_access),
    ):
        items = replay.cases()
        recent_runs = operations.replay_runs()
        if internal_reader:
            return envelope(request, {"items": items, "recent_runs": recent_runs})
        display_names = {
            str(item.get("case_id") or ""): item.get("title")
            for item in items
            if isinstance(item, dict)
        }
        return envelope(
            request,
            {
                "items": [
                    public_replay_case(item)
                    for item in items
                    if isinstance(item, dict)
                ],
                "recent_runs": [
                    public_replay_run(item, display_names=display_names)
                    for item in recent_runs
                    if isinstance(item, dict)
                ],
            },
        )

    @application.post("/api/v1/replays/{case_id}/run", dependencies=[Depends(require_operator)])
    def replay_run(request: Request, case_id: str):
        try:
            operations.set_demo_mode("REPLAY")
            return envelope(request, replay.run(case_id))
        except ReplayCaseNotFound as exc:
            raise HTTPException(404, {"code": "REPLAY_CASE_NOT_FOUND", "message": f"replay case not found: {case_id}"}) from exc

    @application.post("/api/v1/replays/{case_id}/reset", dependencies=[Depends(require_operator)])
    def replay_reset(request: Request, case_id: str):
        try:
            return envelope(request, {"case_id": case_id, "deleted_runs": replay.reset(case_id)})
        except ReplayCaseNotFound as exc:
            raise HTTPException(404, {"code": "REPLAY_CASE_NOT_FOUND", "message": f"replay case not found: {case_id}"}) from exc

    @application.get("/api/v1/demo/mode")
    def get_demo_mode(request: Request):
        return envelope(request, {"mode": operations.demo_mode(settings.demo_mode)})

    @application.post("/api/v1/demo/mode/{mode}", dependencies=[Depends(require_operator)])
    def set_demo_mode(request: Request, mode: str):
        try:
            return envelope(request, {"mode": operations.set_demo_mode(mode)})
        except ValueError as exc:
            raise HTTPException(422, {"code": "INVALID_DEMO_MODE", "message": str(exc)}) from exc

    return application


app = create_app()
