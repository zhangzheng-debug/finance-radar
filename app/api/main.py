from __future__ import annotations

import ipaddress
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import stat
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
from app.models.qwen_risk_contract import (
    QWEN_RISK_CONTRACT_VERSION,
    QWEN_RISK_PROMPT_VERSION,
)
from app.source_url_policy import public_source_url
from app.services import (
    CAPTURE_INTERPRETATION_CONTRACT,
    CAPTURE_INTERPRETATION_PROMPT_SHA256,
    DEEPSEEK_CHEAP_TEXT_MODEL,
    AdjudicationService,
    EvidenceAgent,
    LocalEvidenceModelProvider,
    ReplayService,
    capture_source_text,
    build_qwen_risk_input_contract,
    deterministic_interpretation,
    evidence_receipt_fingerprint,
    knowledge_context,
    normalized_capture_input,
    validate_interpretation_result,
)
from app.services.capture_interpretation import (
    CAPTURE_INTERPRETATION_PROMPT_VERSION,
    LEGACY_CAPTURE_INTERPRETATION_PROMPT_SHA256,
    LEGACY_CAPTURE_INTERPRETATION_PROMPT_VERSION,
)
from app.services.public_event_semantics import (
    derive_public_display_headline,
    derive_public_event_semantics,
    project_public_qwen_semantics,
    project_public_risk_assessment,
)
from app.services.replay import ReplayCaseNotFound
from app.storage import EvidenceObjectStore, LedgerRepository, OperationsRepository


API_SCHEMA_VERSION = "1.3"
LOGGER = logging.getLogger(__name__)
BACKUP_SNAPSHOT_MAX_AGE_SECONDS = 36 * 60 * 60
BACKUP_HEALTH_RECEIPT_FORMAT = "finance-radar-backup-health-receipt-v1"
BACKUP_HEALTH_RECEIPT_MAX_BYTES = 64 * 1024
OVERVIEW_SNAPSHOT_REFRESH_SECONDS = 30.0
OVERVIEW_PUBLISHED_SNAPSHOT_REFRESH_SECONDS = 5 * 60.0
PUBLIC_MARKET_REACTION_WINDOWS = (
    ("t_plus_5m", "T+5m"),
    ("t_plus_30m", "T+30m"),
    ("t_plus_2h", "T+2h"),
    ("next_close", "下个收盘"),
    ("t_plus_1d", "T+1d"),
    ("t_plus_5d", "T+5d"),
)


def _public_mapping_rank(value: Any) -> int:
    """Return a bounded display rank without trusting persisted legacy values."""

    try:
        rank = int(value)
    except (TypeError, ValueError, OverflowError):
        return 99
    return rank if 1 <= rank <= 3 else 99


GENERIC_REVIEW_REASONS = frozenset(
    {
        "已逐条核对精确引文",
        "verified the exact primary-source passage",
        "reviewed",
        "n/a",
    }
)


def _public_market_reaction(value: dict[str, Any]) -> dict[str, Any] | None:
    """Project completed, audit-only reaction returns for public reading.

    Pending jobs, missed windows, raw snapshots and provider failures remain
    operational data. The public surface receives only numeric metrics whose
    database isolation flags prove they cannot feed discovery or model input.
    """

    rows = value.get("market_metrics")
    if not isinstance(rows, list):
        return None
    raw_assets = value.get("assets")
    raw_assets = raw_assets if isinstance(raw_assets, list) else []
    asset_context: dict[str, dict[str, Any]] = {}
    role_labels = {
        "DIRECT_SECURITY": "直接证券",
        "DIRECT_ASSET": "直接资产",
        "US_LISTED_PROXY": "美股代理",
        "MARKET_BENCHMARK": "市场基准",
        "SECTOR_PROXY": "行业代理",
        "THEMATIC_PROXY": "观察代理",
    }
    fallback_roles = {
        "PRIMARY": "DIRECT_SECURITY",
        "SECTOR": "SECTOR_PROXY",
        "MACRO_PROXY": "THEMATIC_PROXY",
        "ECOSYSTEM_PROXY": "THEMATIC_PROXY",
    }
    for asset in raw_assets:
        if not isinstance(asset, dict):
            continue
        asset_id = str(asset.get("asset_id") or "").strip()
        try:
            active = int(asset.get("market_observation_allowed") or 0) == 1
            no_trading = int(asset.get("no_trading") or 0) == 1
        except (TypeError, ValueError):
            continue
        if not asset_id or not active or not no_trading:
            continue
        role = str(asset.get("display_role") or "").strip().upper()
        if role not in role_labels:
            role = fallback_roles.get(str(asset.get("relation_type") or "").upper(), "")
        symbol = str(asset.get("symbol") or "").strip()
        provider_symbol = str(asset.get("provider_symbol") or "").strip()
        canonical_symbol = symbol or provider_symbol
        if not canonical_symbol:
            continue
        asset_context[asset_id] = {
            "role": role,
            "role_label": role_labels.get(role, ""),
            "proxy_label": str(asset.get("proxy_label") or "").strip()[:120],
            "_mapping_rank": _public_mapping_rank(asset.get("mapping_rank")),
            "_symbol": canonical_symbol[:32],
            "_provider_symbol": provider_symbol[:64],
        }
    window_order = {
        window: index
        for index, (window, _label) in enumerate(PUBLIC_MARKET_REACTION_WINDOWS)
    }
    labels = dict(PUBLIC_MARKET_REACTION_WINDOWS)
    projected: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("metric_scope") or "") != "post_event_audit_only":
            continue
        if str(row.get("metric_value_type") or "") != "decimal_percent":
            continue
        context = asset_context.get(str(row.get("stable_id") or ""))
        if context is None:
            continue
        try:
            if int(row.get("allowed_for_discovery_rank") or 0) != 0:
                continue
            if int(row.get("allowed_as_model_feature") or 0) != 0:
                continue
            value_percent = float(str(row.get("metric_value") or ""))
        except (TypeError, ValueError):
            continue
        if value_percent != value_percent or value_percent in {
            float("inf"),
            float("-inf"),
        }:
            continue
        metric_name = str(row.get("metric_name") or "")
        window = next(
            (
                candidate
                for candidate, _label in PUBLIC_MARKET_REACTION_WINDOWS
                if metric_name.startswith(f"reaction_return_{candidate}_pct__")
            ),
            "",
        )
        if not window:
            continue
        suffix = metric_name.split(
            f"reaction_return_{window}_pct__",
            1,
        )[-1].strip()
        metric_symbol = str(row.get("ticker_at_event") or "").strip()
        accepted_symbols = {
            candidate.upper()
            for candidate in (
                str(context.get("_symbol") or "").strip(),
                str(context.get("_provider_symbol") or "").strip(),
            )
            if candidate
        }
        supplied_symbols = {
            candidate.upper() for candidate in (metric_symbol, suffix) if candidate
        }
        if not accepted_symbols or not supplied_symbols.issubset(accepted_symbols):
            continue
        symbol = str(context["_symbol"])
        timestamp_precision = str(row.get("timestamp_precision") or "")
        label = labels[window]
        if timestamp_precision == "DATE_ONLY":
            label = {
                "next_close": "首个完整交易日收盘",
                "t_plus_1d": "下一交易日收盘",
                "t_plus_5d": "5个交易日后",
            }.get(window, label)
        item = {
            "window": window,
            "label": label,
            "symbol": symbol[:32],
            "return_pct": round(value_percent, 6),
            "provider": str(row.get("provider") or "")[:64],
            "event_trade_date": row.get("event_trade_date"),
            "benchmark_ticker": str(row.get("benchmark_ticker") or "")[:32] or None,
            "scope": "post_event_audit_only",
            "role": context["role"],
            "role_label": context["role_label"],
            "proxy_label": context["proxy_label"],
            "_mapping_rank": context["_mapping_rank"],
            "_updated_at": row.get("updated_at"),
        }
        if timestamp_precision:
            item["timestamp_precision"] = timestamp_precision
        key = (str(row.get("stable_id") or ""), window)
        previous = projected.get(key)
        if previous is None or str(item.get("_updated_at") or "") >= str(
            previous.get("_updated_at") or ""
        ):
            projected[key] = item
    items = list(projected.values())
    items.sort(
        key=lambda item: (
            int(item.get("_mapping_rank") or 99),
            window_order[str(item["window"])],
            str(item["symbol"]),
        )
    )
    for item in items:
        item.pop("_updated_at", None)
        item.pop("_mapping_rank", None)
    if not items:
        return None
    return {
        "items": items,
        "scope": "post_event_audit_only",
        "uses_event_truth": False,
        "used_as_model_feature": False,
        "used_for_discovery_rank": False,
    }


def _public_market_context(value: dict[str, Any]) -> dict[str, Any] | None:
    """Project completed read-only price observations for public reading.

    These are event-relative price snapshots, not live quotes. The projection
    excludes pending jobs, provider errors, raw payloads and snapshots attached
    to obsolete event versions.
    """

    raw_assets = value.get("assets")
    raw_assets = raw_assets if isinstance(raw_assets, list) else []
    role_labels = {
        "DIRECT_SECURITY": "直接证券",
        "DIRECT_ASSET": "直接资产",
        "US_LISTED_PROXY": "美股代理",
        "MARKET_BENCHMARK": "市场基准",
        "SECTOR_PROXY": "行业代理",
        "THEMATIC_PROXY": "观察代理",
    }
    fallback_roles = {
        "PRIMARY": "DIRECT_SECURITY",
        "SECTOR": "SECTOR_PROXY",
        "MACRO_PROXY": "THEMATIC_PROXY",
        "ECOSYSTEM_PROXY": "THEMATIC_PROXY",
    }
    asset_context: dict[str, dict[str, Any]] = {}
    for asset in raw_assets:
        if not isinstance(asset, dict):
            continue
        asset_id = str(asset.get("asset_id") or "").strip()
        try:
            allowed = int(asset.get("market_observation_allowed") or 0) == 1
            no_trading = int(asset.get("no_trading") or 0) == 1
        except (TypeError, ValueError):
            continue
        if not asset_id or not allowed or not no_trading:
            continue
        symbol = str(asset.get("symbol") or "").strip()
        provider_symbol = str(asset.get("provider_symbol") or "").strip()
        canonical_symbol = symbol or provider_symbol
        if not canonical_symbol:
            continue
        role = str(asset.get("display_role") or "").strip().upper()
        if role not in role_labels:
            role = fallback_roles.get(str(asset.get("relation_type") or "").upper(), "")
        asset_context[asset_id] = {
            "symbol": canonical_symbol[:32],
            "provider_symbol": provider_symbol[:64],
            "role": role,
            "role_label": role_labels.get(role, ""),
            "proxy_label": str(asset.get("proxy_label") or "").strip()[:120],
            "_mapping_rank": _public_mapping_rank(asset.get("mapping_rank")),
        }

    rows = value.get("market_snapshots")
    if not isinstance(rows, list):
        return None
    projected: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        context = asset_context.get(str(row.get("asset_id") or ""))
        if context is None:
            continue
        try:
            if int(row.get("read_only") or 0) != 1:
                continue
            if int(row.get("no_trading") or 0) != 1:
                continue
            price = float(str(row.get("price") or ""))
        except (TypeError, ValueError):
            continue
        if price <= 0 or price != price or price in {float("inf"), float("-inf")}:
            continue
        if str(row.get("market_job_status") or "").upper() != "COMPLETED":
            continue
        provider = str(row.get("provider") or "").strip().lower()
        if provider not in {"twelve_data", "binance_public"}:
            continue
        provider_symbol = str(row.get("provider_symbol") or "").strip()
        accepted_symbols = {
            candidate.upper()
            for candidate in (context["symbol"], context["provider_symbol"])
            if candidate
        }
        if not provider_symbol or provider_symbol.upper() not in accepted_symbols:
            continue
        observed_at = str(row.get("provider_as_of") or row.get("captured_at") or "").strip()
        if not observed_at:
            continue
        currency = str(row.get("currency") or "").strip().upper()
        if currency and (not currency.replace("_", "").isalnum() or len(currency) > 12):
            continue
        item = {
            "symbol": context["symbol"],
            "price": round(price, 8),
            "currency": currency or None,
            "observed_at": observed_at[:40],
            "provider": provider,
            "observation_window": str(row.get("observation_window") or "")[:32],
            "role": context["role"],
            "role_label": context["role_label"],
            "proxy_label": context["proxy_label"],
            "_mapping_rank": context["_mapping_rank"],
        }
        timestamp_precision = str(row.get("timestamp_precision") or "")
        if timestamp_precision:
            item["timestamp_precision"] = timestamp_precision
        asset_id = str(row.get("asset_id") or "")
        previous = projected.get(asset_id)
        if previous is None or str(item["observed_at"]) >= str(previous["observed_at"]):
            projected[asset_id] = item
    items = sorted(
        projected.values(),
        key=lambda item: (
            int(item.get("_mapping_rank") or 99),
            str(item["symbol"]),
        ),
    )
    for item in items:
        item.pop("_mapping_rank", None)
    if not items:
        return None
    return {
        "items": items,
        "scope": "event_relative_price_observation",
        "is_live_quote": False,
        "uses_event_truth": False,
        "used_as_model_feature": False,
        "used_for_discovery_rank": False,
    }


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


def _stable_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _unsafe_backup_health_receipt_permissions(
    parent_stat: os.stat_result,
    receipt_stat: os.stat_result,
    *,
    platform_name: str | None = None,
) -> str | None:
    """Return the POSIX ownership/mode violation for a public root receipt."""

    if (platform_name or os.name) != "posix":
        return None
    if parent_stat.st_uid != 0 or receipt_stat.st_uid != 0:
        return "non_root_owner"
    if parent_stat.st_mode & 0o022 or receipt_stat.st_mode & 0o022:
        return "writable_by_non_root"
    return None


def _read_backup_health_receipt(
    receipt_path: Path | None,
    latest_backup: dict[str, Any],
) -> tuple[dict[str, Any] | None, str]:
    """Validate a bounded root receipt without opening the backup tree."""

    if receipt_path is None:
        return None, "not_configured"
    try:
        parent_stat = receipt_path.parent.stat(follow_symlinks=False)
        receipt_stat = receipt_path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None, "missing"
    except PermissionError:
        return None, "unreadable"
    except OSError:
        return None, "unavailable"
    if receipt_path.is_symlink() or not stat.S_ISREG(receipt_stat.st_mode):
        return None, "unsafe_file_type"
    if not stat.S_ISDIR(parent_stat.st_mode):
        return None, "unsafe_parent_type"
    if receipt_stat.st_size <= 0 or receipt_stat.st_size > BACKUP_HEALTH_RECEIPT_MAX_BYTES:
        return None, "invalid_size"
    permission_violation = _unsafe_backup_health_receipt_permissions(
        parent_stat,
        receipt_stat,
    )
    if permission_violation is not None:
        return None, permission_violation
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(receipt_path, flags)
        try:
            opened_stat = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or opened_stat.st_size != receipt_stat.st_size
                or opened_stat.st_size > BACKUP_HEALTH_RECEIPT_MAX_BYTES
            ):
                return None, "changed_while_opening"
            chunks: list[bytes] = []
            remaining = BACKUP_HEALTH_RECEIPT_MAX_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
        if len(raw) != receipt_stat.st_size:
            return None, "changed_while_reading"
        receipt = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid_json"
    if not isinstance(receipt, dict) or receipt.get("format") != BACKUP_HEALTH_RECEIPT_FORMAT:
        return None, "invalid_contract"
    digest = str(receipt.get("payload_sha256") or "")
    unsigned = dict(receipt)
    unsigned.pop("payload_sha256", None)
    if len(digest) != 64 or not secrets.compare_digest(
        digest,
        hashlib.sha256(_stable_json_bytes(unsigned)).hexdigest(),
    ):
        return None, "invalid_digest"
    expected = {
        "backup_id": str(latest_backup.get("backup_id") or ""),
        "verified_at": str(latest_backup.get("verified_at") or ""),
        "quick_check": str(latest_backup.get("quick_check") or ""),
        "snapshot_kind": str(latest_backup.get("snapshot_kind") or ""),
        "source_bytes": int(latest_backup.get("source_bytes") or 0),
        "backup_bytes": int(latest_backup.get("backup_bytes") or 0),
        "manifest_name": Path(str(latest_backup.get("manifest_path") or "")).name,
    }
    for key, value in expected.items():
        if receipt.get(key) != value:
            return None, f"record_mismatch:{key}"
    if receipt.get("status") != "VERIFIED" or receipt.get("quick_check") != "ok":
        return None, "not_verified"
    return receipt, "validated"


class HumanOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


def validate_human_override_rationale(payload: HumanOverrideRequest) -> str:
    """Reject placeholder rationale before it becomes an immutable audit row.

    Reviewer identity is deliberately not accepted from the request body.  The
    endpoint binds attribution to the personal credential resolved by
    ``require_bound_reviewer_principal``.
    """

    reason = _normalized_audit_text(payload.reason)
    if len(reason) < 20 or reason.casefold() in GENERIC_REVIEW_REASONS:
        raise HTTPException(
            422,
            {
                "code": "SPECIFIC_REVIEW_RATIONALE_REQUIRED",
                "message": "human-review audit records require an event-specific rationale",
            },
        )
    return reason


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
            health_receipt, health_receipt_status = _read_backup_health_receipt(
                settings.backup_health_receipt_path,
                latest_backup,
            )
            # A PermissionError proves only that this identity cannot inspect
            # the protected path.  It cannot distinguish an existing bundle
            # from a bundle deleted behind an untraversable parent directory,
            # so a protected record must never be promoted to FRESH.
            fresh = (
                backup_quick_check == "ok"
                and age_seconds is not None
                and age_seconds <= BACKUP_SNAPSHOT_MAX_AGE_SECONDS
                and (
                    artifact_visibility == "visible"
                    or health_receipt_status == "validated"
                )
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
                "health_receipt_status": health_receipt_status,
                "artifact_verification_source": (
                    "live_path_stat_and_latest_verified_backup_record"
                    if artifact_visibility == "visible"
                    else "root_verified_recovery_receipt"
                    if health_receipt_status == "validated"
                    else "unprivileged_path_probe_inconclusive"
                    if artifact_visibility == "protected"
                    else "latest_verified_backup_record"
                ),
            }
            if health_receipt is not None:
                backup_snapshot["health_receipt_issued_at"] = health_receipt.get("issued_at")
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
        require_public_semantics: bool = False,
        allow_excluded_capture_archive: bool = False,
    ) -> dict[str, Any]:
        event = ledger.event_detail(
            event_id,
            semantic_events_only=require_public_semantics,
        )
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
        "displayable_source_count",
        "citable_evidence_count",
        "primary_source_url_count",
        "public_source_url_count",
        "captured_text_count",
        "source_problem_count",
        "public_fact_summary",
        "claim_subject",
        "claim_action",
        "claim_stage",
        "known_at",
        "reader_ready",
        "no_trading",
        "unverified_capture_excerpt",
        "summary_basis",
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

    def public_event_item(
        value: dict[str, Any],
        *,
        captured_source: dict[str, Any] | None = None,
        risk_run: dict[str, Any] | None = None,
        qwen_run: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the public event fields without promoting private review prose.

        ``fact_summary`` and ``evidence_summary`` are historical reviewer fields;
        neither is a public fact contract.  When the explicit
        ``public_fact_summary`` is absent, the public surface may show only a
        bounded title/summary captured from the source, clearly labelled as an
        unverified excerpt.
        """

        result = {key: value.get(key) for key in public_event_fields}
        result.update(derive_public_event_semantics(value, captured_source))
        result.update(derive_public_display_headline(value, captured_source))
        result["risk_assessment"] = project_public_risk_assessment(
            risk_run,
            current_version=int(value.get("current_version") or 0),
        )
        result["semantic_assessment"] = project_public_qwen_semantics(
            qwen_run,
            current_version=int(value.get("current_version") or 0),
        )
        # A structured claim is public only when the current event version has
        # crossed the citation gate.  Historical ledgers may already contain
        # claim slots even though the evidence relationship for that version
        # is missing or no longer reader-eligible.  Keeping every canonical
        # event visible must never turn those dormant slots into a public fact.
        if result["citation_ready"] and str(
            result.get("public_fact_summary") or ""
        ).strip():
            result["unverified_capture_excerpt"] = None
            result["summary_basis"] = "CITATION_READY_FACT"
            return result

        source = captured_source if isinstance(captured_source, dict) else value
        raw_excerpt = (
            source.get("source_summary")
            or source.get("summary")
            or source.get("source_title")
            or source.get("title")
        )
        printable = "".join(
            character if character.isprintable() else " "
            for character in str(raw_excerpt or "")
        )
        excerpt = " ".join(printable.split())
        if len(excerpt) > 360:
            excerpt = excerpt[:359].rstrip() + "…"
        for field in (
            "public_fact_summary",
            "claim_subject",
            "claim_action",
            "claim_stage",
            "known_at",
        ):
            result[field] = None
        result["unverified_capture_excerpt"] = excerpt or None
        result["summary_basis"] = (
            "UNVERIFIED_CAPTURE_EXCERPT" if excerpt else "NO_PUBLIC_SUMMARY"
        )
        return result

    def current_risk_runs(
        events: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        event_versions: dict[str, int] = {}
        for event in events:
            event_id = str(event.get("event_id") or "").strip()
            try:
                current_version = int(event.get("current_version") or 0)
            except (TypeError, ValueError):
                continue
            if event_id and current_version > 0:
                event_versions[event_id] = current_version
        try:
            return operations.latest_model_runs_for_versions(event_versions)
        except (OSError, sqlite3.Error) as exc:
            # Risk routing is an optional reader axis, not an event-admission
            # dependency.  If the operations store is locked or unavailable,
            # keep every canonical event visible and project a null assessment.
            LOGGER.warning("public risk assessment unavailable: %s", exc)
            return {}

    def current_qwen_runs(
        events: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        event_versions: dict[str, int] = {}
        for event in events:
            event_id = str(event.get("event_id") or "").strip()
            try:
                current_version = int(event.get("current_version") or 0)
            except (TypeError, ValueError):
                continue
            if event_id and current_version > 0:
                event_versions[event_id] = current_version
        try:
            publication = operations.qwen_risk_publication()
            if publication.get("public_approved") is not True:
                return {}
            candidates = operations.latest_qwen_risk_runs_for_versions(event_versions)
            inputs = ledger.shadow_batch(
                limit=max(1, len(event_versions)),
                order="event_id",
                event_ids=list(event_versions),
            )
            input_by_event = {
                str((item.get("detail") or {}).get("event", {}).get("event_id") or ""): item
                for item in inputs
                if isinstance(item, dict)
            }
            selected: dict[str, dict[str, Any]] = {}
            for event_id, run in candidates.items():
                output = run.get("output") if isinstance(run, dict) else None
                item = input_by_event.get(event_id)
                if not isinstance(output, dict) or not isinstance(item, dict):
                    continue
                if int(run.get("shadow") or 0) != 1:
                    continue
                if any(
                    str(output.get(key) or "") != str(publication.get(key) or "")
                    for key in (
                        "model_version",
                        "adapter_sha256",
                        "contract_version",
                        "prompt_version",
                    )
                ):
                    continue
                contract = build_qwen_risk_input_contract(
                    item.get("detail") or {},
                    item.get("evidence") or [],
                    model_version=str(publication["model_version"]),
                )
                if contract.get("input_sufficient") is not True:
                    continue
                if any(
                    str(output.get(key) or run.get(key) or "")
                    != str(contract.get(key) or "")
                    for key in (
                        "input_sha256",
                        "source_identity_sha256",
                        "evidence_identity_sha256",
                        "evidence_context_sha256",
                    )
                ):
                    continue
                selected[event_id] = {
                    **run,
                    "publication_state": "PUBLIC_APPROVED",
                    "current_input": True,
                }
            return selected
        except (OSError, sqlite3.Error) as exc:
            LOGGER.warning("public Qwen semantic assessment unavailable: %s", exc)
            return {}

    def public_evidence_item(value: dict[str, Any]) -> dict[str, Any]:
        result = {key: value.get(key) for key in public_evidence_fields}
        result["evidence_url"] = public_source_url(value.get("evidence_url"))
        return result

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

        source_url = public_source_url(value.get("canonical_url"))
        excerpt_raw = normalized_text(value.get("summary"))
        return {
            "source_name": value.get("source_name"),
            "source_type": value.get("source_type"),
            "authority_tier": value.get("authority_tier"),
            "source_title": bounded_text(value.get("title"), 500),
            "source_excerpt": bounded_text(value.get("summary"), 1200),
            "source_excerpt_original_length": len(excerpt_raw),
            "source_excerpt_truncated": len(excerpt_raw) > 1200,
            "source_url": source_url,
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
        "source_shape",
        "one_line_zh",
        "what_source_says",
        "what_source_does_not_prove_zh",
        "actors",
        "affected_assets",
        "modality",
        "why_current_state_zh",
        "missing_to_change_state_zh",
        "boundary_zh",
        "prompt_injection_suspected",
        "persisted",
        "external_generation_state",
        "safety",
    )

    def public_capture_interpretation(value: dict[str, Any]) -> dict[str, Any]:
        projected = {
            key: value.get(key) for key in public_capture_interpretation_fields
        }
        claims = value.get("what_source_says")
        claims = claims if isinstance(claims, list) else []
        projected["what_source_says"] = [
            claim
            for claim in claims
            if isinstance(claim, dict)
            and str(claim.get("text_zh") or "").strip()
            not in {"来源表达了所引用的内容。", "来源标题表达了这一主张；尚未完成中文语义复核。"}
        ][:2]
        projected["boundary_zh"] = (
            str(value.get("boundary_zh") or "").strip()
            or "AI仅解释来源文本，不参与事件评级或价格判断。"
        )
        return projected

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
        risk_run: dict[str, Any] | None = None,
        qwen_run: dict[str, Any] | None = None,
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
        public_event = public_event_item(
            raw_event,
            captured_source=(
                value.get("preferred_source")
                if isinstance(value.get("preferred_source"), dict)
                else None
            ),
            risk_run=risk_run,
            qwen_run=qwen_run,
        )
        public_facts = (
            {
                key: facts.get(key)
                for key in public_fact_fields
                if key in facts
            }
            if public_event.get("citation_ready") is True
            else {}
        )
        preferred_source = (
            value.get("preferred_source")
            if isinstance(value.get("preferred_source"), dict)
            else {}
        )
        source_link = (
            value.get("source_link")
            if isinstance(value.get("source_link"), dict)
            else {}
        )
        result: dict[str, Any] = {
            "event": public_event,
            "current_version": {
                "version": version.get("version"),
                "facts": public_facts,
            },
            "preferred_source": (
                public_captured_source(preferred_source)
                if preferred_source
                else {}
            ),
            "source_link": (
                public_captured_source(source_link)
                if source_link
                else {}
            ),
            "evidence_count": len(evidence),
            "no_trading_banner": value.get("no_trading_banner"),
        }
        market_reaction = _public_market_reaction(value)
        if market_reaction is not None:
            result["market_reaction"] = market_reaction
        market_context = _public_market_context(value)
        if market_context is not None:
            result["market_context"] = market_context
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

    def public_source_interpretation_items(
        event: dict[str, Any],
        captures: list[dict[str, Any]],
        *,
        eligibility: dict[str, Any] | None = None,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        """Return only current, persisted external explanations.

        Deterministic previews are an internal debugging aid.  They must never
        be dressed as external AI on the public surface, and an event with any
        evidence row is categorically outside this feature's boundary.
        """

        eligibility = eligibility or ledger.capture_interpretation_eligibility(
            str(event.get("event_id") or "")
        )
        if not eligibility.get("eligible"):
            return []

        eligible_observation_ids = set(
            eligibility.get("eligible_observation_ids") or []
        )
        visible_captures = [
            capture
            for capture in captures
            if (include_deleted or capture.get("observation_status") != "deleted")
            and str(capture.get("observation_id") or "")
            in eligible_observation_ids
        ]
        receipts = [
            str(capture.get("capture_receipt_sha256") or "")
            for capture in visible_captures
        ]
        persisted_runs = operations.latest_capture_interpretations(
            str(event.get("event_id") or ""),
            receipts,
            generation_priority=(
                (
                    CAPTURE_INTERPRETATION_CONTRACT,
                    CAPTURE_INTERPRETATION_PROMPT_VERSION,
                    CAPTURE_INTERPRETATION_PROMPT_SHA256,
                    DEEPSEEK_CHEAP_TEXT_MODEL,
                ),
                (
                    CAPTURE_INTERPRETATION_CONTRACT,
                    LEGACY_CAPTURE_INTERPRETATION_PROMPT_VERSION,
                    LEGACY_CAPTURE_INTERPRETATION_PROMPT_SHA256,
                    DEEPSEEK_CHEAP_TEXT_MODEL,
                ),
            ),
        )
        items: list[dict[str, Any]] = []
        for capture in visible_captures:
            receipt = str(capture.get("capture_receipt_sha256") or "")
            run = persisted_runs.get(receipt)
            output = dict((run or {}).get("output") or {})
            try:
                if not output:
                    continue
                validate_interpretation_result(
                    output,
                    capture_source_text(capture),
                    allow_legacy_prompt=True,
                )
                if str((run or {}).get("status") or "") != "COMPLETED":
                    continue
                if int((run or {}).get("external_call") or 0) != 1:
                    continue
                if str((run or {}).get("provider") or "") != "deepseek":
                    continue
                if str((run or {}).get("contract_version") or "") != (
                    CAPTURE_INTERPRETATION_CONTRACT
                ):
                    continue
                run_prompt_identity = (
                    str((run or {}).get("prompt_version") or ""),
                    str((run or {}).get("prompt_sha256") or ""),
                )
                if run_prompt_identity not in {
                    (
                        CAPTURE_INTERPRETATION_PROMPT_VERSION,
                        CAPTURE_INTERPRETATION_PROMPT_SHA256,
                    ),
                    (
                        LEGACY_CAPTURE_INTERPRETATION_PROMPT_VERSION,
                        LEGACY_CAPTURE_INTERPRETATION_PROMPT_SHA256,
                    ),
                }:
                    continue
                if str((run or {}).get("model_snapshot") or "") != (
                    DEEPSEEK_CHEAP_TEXT_MODEL
                ):
                    continue
                if output.get("status") != "READY" or output.get("mode") != "LLM_ASSISTED":
                    continue
                if output.get("persisted") is not True:
                    continue
                if output.get("external_generation_state") != "COMPLETED":
                    continue
                if str(output.get("event_id") or "") != str(event.get("event_id") or ""):
                    continue
                if int(output.get("bound_event_version") or 0) != int(
                    eligibility.get("current_event_version") or 0
                ):
                    continue
                if str(output.get("capture_receipt_sha256") or "") != receipt:
                    continue
                if int(output.get("source_revision_no") or 0) != int(
                    capture.get("latest_revision_no") or 0
                ):
                    continue
                if str(output.get("bound_content_sha256") or "") != str(
                    capture.get("semantic_content_sha256") or ""
                ):
                    continue
                if str(output.get("input_sha256") or "") != str(
                    (run or {}).get("input_sha256") or ""
                ):
                    continue
                if (
                    str(output.get("prompt_version") or ""),
                    str(output.get("prompt_sha256") or ""),
                ) != run_prompt_identity:
                    continue
            except Exception:
                continue
            items.append(public_capture_interpretation(output))
        return items

    def public_capture_explanation(
        event_id: str,
        *,
        event_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Project the cache-only public state of zero-evidence AI explanation."""

        event_data = event_data or event_or_404(
            event_id,
            require_reader_ready=False,
            require_public_semantics=True,
        )
        event = dict(event_data.get("event") or {})
        eligibility = ledger.capture_interpretation_eligibility(event_id)
        result: dict[str, Any] = {
            "display": bool(eligibility.get("display")),
            "reason_code": eligibility.get("reason_code"),
            "state": "NOT_APPLICABLE",
            "generation_path": "BACKGROUND_CACHE_ONLY",
            "item": None,
            "source": None,
            "attempts": 0,
            "queued_at": None,
            "updated_at": None,
            "next_retry_at": None,
            "boundary": {
                "enabled_only_when_event_has_zero_evidence": True,
                "captured_text_is_not_evidence": True,
                "does_not_confirm_event_truth": True,
                "canonical_mutation_allowed": False,
                "used_as_model_feature": False,
                "changes_materiality_or_polarity": False,
                "no_trading": True,
            },
        }
        if not eligibility.get("eligible"):
            result["state"] = {
                "REFETCH_PRIMARY_SOURCE": "REFETCH_PRIMARY_SOURCE",
                "NO_CAPTURE_TEXT": "NO_CAPTURE_TEXT",
                "CAPTURE_NOT_FOUND": "NO_CAPTURE_TEXT",
                "EVENT_NOT_FOUND": "NOT_APPLICABLE",
                "EVIDENCE_PRESENT": "NOT_APPLICABLE",
            }.get(str(eligibility.get("reason_code") or ""), "NOT_APPLICABLE")
            return result

        eligible_ids = set(eligibility.get("eligible_observation_ids") or [])
        captures = [
            item
            for item in ledger.captured_sources(event_id)
            if item.get("observation_status") != "deleted"
            and str(item.get("observation_id") or "") in eligible_ids
        ]
        items = public_source_interpretation_items(
            event,
            captures,
            eligibility=eligibility,
        )
        if items:
            ready_receipt = str(items[0].get("capture_receipt_sha256") or "")
            ready_capture = next(
                (
                    capture
                    for capture in captures
                    if str(capture.get("capture_receipt_sha256") or "")
                    == ready_receipt
                ),
                None,
            )
            if ready_capture is not None:
                result["source"] = public_captured_source(ready_capture)
            result["state"] = "READY"
            result["item"] = items[0]
            return result

        receipts = {
            str(item.get("capture_receipt_sha256") or "")
            for item in captures
            if str(item.get("capture_receipt_sha256") or "")
        }
        latest_run = next(
            (
                run
                for run in operations.capture_interpretation_runs(event_id, limit=200)
                if str(run.get("capture_receipt_sha256") or "") in receipts
                and str(run.get("provider") or "") == "deepseek"
                and str(run.get("contract_version") or "")
                == CAPTURE_INTERPRETATION_CONTRACT
                and str(run.get("prompt_version") or "")
                == CAPTURE_INTERPRETATION_PROMPT_VERSION
                and str(run.get("prompt_sha256") or "")
                == CAPTURE_INTERPRETATION_PROMPT_SHA256
                and str(run.get("model_snapshot") or "")
                == DEEPSEEK_CHEAP_TEXT_MODEL
            ),
            None,
        )
        selected_receipt = str((latest_run or {}).get("capture_receipt_sha256") or "")
        selected_capture = next(
            (
                capture
                for capture in captures
                if str(capture.get("capture_receipt_sha256") or "")
                == selected_receipt
            ),
            captures[0] if captures else None,
        )
        if selected_capture is not None:
            result["source"] = public_captured_source(selected_capture)
        if latest_run is None:
            result["state"] = "ELIGIBLE_NOT_QUEUED"
            return result

        result["attempts"] = max(0, int(latest_run.get("attempts") or 0))
        result["queued_at"] = latest_run.get("created_at")
        result["updated_at"] = latest_run.get("updated_at")
        run_status = str((latest_run or {}).get("status") or "")
        if run_status == "RUNNING":
            result["state"] = "RUNNING"
        elif run_status == "FAILED":
            result["state"] = "FAILED_TERMINAL"
        elif run_status in {"BUDGET_BLOCKED"} or (
            run_status == "PENDING" and result["attempts"] > 0
        ):
            result["state"] = "RETRY_WAIT"
            result["next_retry_at"] = latest_run.get("available_at")
        elif run_status == "PENDING":
            result["state"] = "QUEUED"
        elif run_status == "COMPLETED":
            # A completed row that failed the current receipt/output contract is
            # intentionally not rendered as ready.
            result["state"] = "SUPERSEDED"
        else:
            result["state"] = "ELIGIBLE_NOT_QUEUED"
        return result

    def public_qwen_semantic_status(event_id: str) -> dict[str, Any]:
        """Return a cache-only public lifecycle for the current Qwen assessment.

        Public readers need to distinguish a current result from work that can
        still be completed, but must not receive worker errors, model hashes or
        unpublished shadow output.  This read never invokes or enqueues a model.
        """

        processing = {
            "state": "PROCESSING",
            "assessment": None,
            "cache_only": True,
        }
        try:
            event_data = event_or_404(
                event_id,
                require_reader_ready=False,
                require_public_semantics=True,
            )
            event = dict(event_data.get("event") or {})
            current = current_qwen_runs([event])
            assessment = project_public_qwen_semantics(
                current.get(str(event.get("event_id") or "")),
                current_version=int(event.get("current_version") or 0),
            )
            if assessment is not None:
                return {
                    "state": "READY",
                    "assessment": assessment,
                    "cache_only": True,
                }

            publication = operations.qwen_risk_publication()
            if publication.get("public_approved") is not True:
                return processing
            inputs = ledger.shadow_batch(
                limit=1,
                order="event_id",
                event_ids=[event_id],
                semantic_events_only=True,
            )
            if not inputs:
                return {
                    "state": "NOT_APPLICABLE",
                    "assessment": None,
                    "cache_only": True,
                }
            candidate = inputs[0] if isinstance(inputs[0], dict) else {}
            contract = build_qwen_risk_input_contract(
                candidate.get("detail") or {},
                candidate.get("evidence") or [],
                model_version=str(publication.get("model_version") or ""),
            )
            if contract.get("input_sufficient") is not True:
                return {
                    "state": "NOT_APPLICABLE",
                    "assessment": None,
                    "cache_only": True,
                }
            return processing
        except HTTPException:
            raise
        except Exception as exc:
            LOGGER.warning("public Qwen semantic status unavailable: %s", exc)
            return processing

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
            snapshot_data, health_snapshot_status = overview_snapshot.read()
            latest_backup = snapshot_data["latest_verified_backup"]
            ledger_health = public_health_paths(
                health_from_latest_verified_backup(
                    snapshot_data["ledger_health_base"],
                    latest_backup,
                )
            )
            ops_health = public_health_paths(snapshot_data["operations_health_base"])
            model_health = router.status()
            if not internal_reader:
                model_health = public_model_status(model_health)
            status = "ok" if ledger_health["status"] == ops_health["status"] == "ok" else "degraded"
            return envelope(
                request,
                {
                    "status": status,
                    "service_version": __version__,
                    "demo_mode": snapshot_data["demo_mode"],
                    "ledger": ledger_health,
                    "operations": ops_health,
                    "model": model_health,
                    "health_snapshot": health_snapshot_status,
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
        except SnapshotUnavailable as exc:
            raise HTTPException(
                503,
                {
                    "code": "HEALTH_SNAPSHOT_UNAVAILABLE",
                    "message": "health snapshot has not completed successfully yet",
                },
            ) from exc
        except FileNotFoundError as exc:
            raise HTTPException(503, {"code": "LEDGER_UNAVAILABLE", "message": str(exc)}) from exc

    @application.get("/api/v1/health/deep", dependencies=[Depends(require_admin)])
    def deep_health(request: Request):
        """Run protected on-demand database diagnostics outside public probes."""

        latest_backup = operations.latest_verified_backup()
        ledger_health = public_health_paths(
            health_from_latest_verified_backup(
                ledger.health(run_integrity_check=False),
                latest_backup,
            )
        )
        ops_health = public_health_paths(operations.health(run_integrity_check=False))
        return envelope(
            request,
            {
                "status": (
                    "ok"
                    if ledger_health["status"] == ops_health["status"] == "ok"
                    else "degraded"
                ),
                "service_version": __version__,
                "ledger": ledger_health,
                "operations": ops_health,
                "model": router.status(),
                "request_path": "protected_on_demand_deep_check",
                "no_trading": True,
            },
        )

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
            recent_events = list(data.get("recent_events", []))
            risk_runs = current_risk_runs(recent_events)
            qwen_runs = current_qwen_runs(recent_events)
            data["recent_events"] = [
                public_event_item(
                    item,
                    risk_run=risk_runs.get(str(item.get("event_id") or "")),
                    qwen_run=qwen_runs.get(str(item.get("event_id") or "")),
                )
                for item in recent_events
            ]
            data["source_health"] = [
                public_source_health(item) for item in data.get("source_health", [])
            ]
        data["demo_mode"] = snapshot_data["demo_mode"]
        latest_worker = snapshot_data["latest_worker_cycle"]
        latest_successful_worker = snapshot_data["latest_successful_worker_cycle"]
        data["latest_worker_cycle"] = (
            operations.latest_worker_cycle()
            if internal_reader
            else public_worker_cycle(latest_worker)
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
        # Public visibility and evidentiary readiness are different concepts.
        # A source capture can be useful without passing the citation gate, but
        # a SEC form/accession/size directory row is not itself an event.  Keep
        # such rows in the internal ledger while the public feed admits only SEC
        # records with a concrete structured fact.  ``reader_ready`` remains a
        # separate authenticated review filter.
        effective_reader_ready = reader_ready if internal_reader else None

        def read_events() -> dict[str, Any]:
            return ledger.list_events(
                status=status,
                public_state=public_state,
                family=family,
                source=source,
                query=q,
                date_from=date_from.isoformat() if date_from else None,
                date_to=date_to.isoformat() if date_to else None,
                reader_ready=effective_reader_ready,
                captured_source_required=False,
                exclude_nonfinancial_retractions=not internal_reader,
                semantic_events_only=not internal_reader,
                # The public card renders at most 360 characters.  Avoid
                # pulling multi-megabyte provider summaries from historical
                # observations only to discard them in ``public_event_item``.
                # Authenticated readers retain the unbounded repository view.
                source_excerpt_chars=512 if not internal_reader else None,
                sort=sort,
                limit=limit,
                offset=offset,
            )

        if internal_reader:
            data = read_events()
        else:
            cache_key = "public-event-feed-v5:" + repr(
                (
                    status,
                    public_state,
                    family,
                    source,
                    q,
                    date_from.isoformat() if date_from else None,
                    date_to.isoformat() if date_to else None,
                    sort,
                    limit,
                    offset,
                )
            )
            data = cached_read(cache_key, 20.0, read_events)
        if not internal_reader:
            risk_runs = current_risk_runs(data["items"])
            qwen_runs = current_qwen_runs(data["items"])
            data["items"] = [
                public_event_item(
                    item,
                    risk_run=risk_runs.get(str(item.get("event_id") or "")),
                    qwen_run=qwen_runs.get(str(item.get("event_id") or "")),
                )
                for item in data["items"]
            ]
        return envelope(request, data)

    @application.get("/api/v1/events/facets")
    def event_facets(
        request: Request,
        reader_ready: bool | None = None,
        internal_reader: bool = Depends(internal_reader_access),
    ):
        effective_reader_ready = reader_ready if internal_reader else None
        cache_key = f"event-facets-v4:{effective_reader_ready!r}:{not internal_reader!r}"
        data = cached_read(
            cache_key,
            60.0,
            lambda: ledger.event_facets(
                reader_ready=effective_reader_ready,
                exclude_nonfinancial_retractions=not internal_reader,
                semantic_events_only=not internal_reader,
            ),
        )
        return envelope(request, data)

    @application.get("/api/v1/events/{event_id}/dossier")
    def public_event_dossier(request: Request, event_id: str):
        """Return the public core dossier without optional capture explanation.

        The previous page made five sequential loopback requests and reopened
        the same event/evidence/source rows repeatedly.  A later revision then
        bundled optional source and AI cache reads back into this critical
        path.  Keep the evidence-backed core bounded here; capture explanation
        is a separate cache-only endpoint and can never delay the event body.
        """

        def read_dossier() -> dict[str, Any]:
            data = event_or_404(
                event_id,
                require_reader_ready=False,
                require_public_semantics=True,
            )
            evidence = reader_scoped_evidence(event_id, internal_reader=False)
            facts = (
                data.get("current_version", {}).get("facts", {})
                if data.get("current_version")
                else {}
            )
            verification_method = public_verification_method(facts, evidence)
            if verification_method is not None:
                data["verification_method"] = verification_method
            data["no_trading_banner"] = (
                "Intelligence and review only. No execution capability is present."
            )
            event = dict(data.get("event") or {})
            risk_runs = current_risk_runs([event])
            qwen_runs = current_qwen_runs([event])
            explanation_eligibility = ledger.capture_interpretation_eligibility(
                event_id
            )
            return {
                "detail": public_event_detail(
                    data,
                    evidence=evidence,
                    risk_run=risk_runs.get(str(event.get("event_id") or "")),
                    qwen_run=qwen_runs.get(str(event.get("event_id") or "")),
                ),
                "evidence": {"items": evidence},
                "capture_explanation": {
                    "display": bool(explanation_eligibility.get("display")),
                    "reason_code": explanation_eligibility.get("reason_code"),
                    "state": (
                        "CHECKING"
                        if explanation_eligibility.get("eligible")
                        else "NOT_APPLICABLE"
                    ),
                    "generation_path": "BACKGROUND_CACHE_ONLY",
                },
                "contract": {
                    "public_projection": True,
                    "consistency_scope": "bounded_multi_read_best_effort",
                    "no_trading": True,
                },
            }

        return envelope(
            request,
            cached_read(f"public-event-dossier-v4:{event_id}", 20.0, read_dossier),
        )

    @application.get("/api/v1/events/{event_id}")
    def event_detail(
        request: Request,
        event_id: str,
        internal_reader: bool = Depends(internal_reader_access),
    ):
        data = event_or_404(
            event_id,
            require_reader_ready=False,
            require_public_semantics=not internal_reader,
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
            event = dict(data.get("event") or {})
            risk_runs = current_risk_runs([event])
            qwen_runs = current_qwen_runs([event])
            data = public_event_detail(
                data,
                evidence=evidence,
                risk_run=risk_runs.get(str(event.get("event_id") or "")),
                qwen_run=qwen_runs.get(str(event.get("event_id") or "")),
            )
        return envelope(request, data)

    @application.get("/api/v1/events/{event_id}/knowledge")
    def event_knowledge(event_id: str, request: Request):
        data = event_or_404(
            event_id,
            require_reader_ready=False,
            require_public_semantics=True,
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
            require_reader_ready=False,
            require_public_semantics=not internal_reader,
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
            require_reader_ready=False,
            require_public_semantics=not internal_reader,
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
            require_reader_ready=False,
            require_public_semantics=not internal_reader,
        )
        event = dict(event_data.get("event") or {})
        captures = ledger.captured_sources(event_id)
        eligibility = ledger.capture_interpretation_eligibility(event_id)
        items = public_source_interpretation_items(
            event,
            captures,
            eligibility=eligibility,
            include_deleted=internal_reader,
        )
        return envelope(
            request,
            {
                "items": items,
                "contract": {
                    "version": CAPTURE_INTERPRETATION_CONTRACT,
                    "advisory_only": True,
                    "canonical_mutation_allowed": False,
                    "used_as_model_feature": False,
                    "public_requests_are_cache_only": True,
                    "enabled_only_when_event_has_zero_evidence": True,
                    "no_trading": True,
                },
            },
        )

    @application.get("/api/v1/events/{event_id}/capture-explanation")
    def event_capture_explanation(request: Request, event_id: str):
        return envelope(request, public_capture_explanation(event_id))

    @application.get("/api/v1/events/{event_id}/semantic-assessment")
    def event_semantic_assessment(request: Request, event_id: str):
        return envelope(request, public_qwen_semantic_status(event_id))

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
        eligibility = ledger.capture_interpretation_eligibility(
            event_id,
            observation_id=observation_id,
        )
        if not eligibility.get("eligible"):
            raise HTTPException(
                422,
                {
                    "code": "CAPTURE_INTERPRETATION_NOT_ELIGIBLE",
                    "message": str(eligibility.get("reason_code") or "UNKNOWN"),
                },
            )
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

    @application.post("/api/v1/events/{event_id}/human-override")
    def record_human_override(
        request: Request,
        event_id: str,
        payload: HumanOverrideRequest,
        principal: dict[str, str] = Depends(require_bound_reviewer_principal),
    ):
        event_or_404(event_id)
        reason = validate_human_override_rationale(payload)
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
            actor=principal["principal_alias"],
            reason=reason,
            before={"review_status": decision["status"], "trace_id": decision["trace_id"]},
            after={
                "review_status": payload.review_status,
                "reviewer_attestation": payload.reviewer_attestation,
                "reviewer_principal_hash": principal["principal_hash"],
                "reviewer_role": principal["role"],
                "credential_bound": True,
            },
        )
        return envelope(
            request,
            {
                "override_id": override_id,
                "event_id": event_id,
                "decision_id": decision["decision_id"],
                "review_status": payload.review_status,
                "reviewer_principal": principal["principal_alias"],
                "credential_bound": True,
                "no_trading": True,
            },
        )

    @application.get("/api/v1/model/status", dependencies=[Depends(require_operator)])
    def model_status(request: Request):
        data = router.status()
        data["recent_runs"] = operations.model_runs(limit=20)
        try:
            capture_health = operations.capture_interpretation_queue_health(
                "deepseek",
                contract_version=CAPTURE_INTERPRETATION_CONTRACT,
                prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
                prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
                model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
            )
            capture_health.update(
                {
                    "enabled": bool(settings.capture_llm_enabled),
                    "configured_provider": settings.capture_llm_provider,
                    "candidate_count": ledger.capture_interpretation_candidate_count(),
                    "inventory": operations.get_state(
                        "capture_interpretation_inventory_v3", {}
                    ),
                    "runtime": operations.get_state(
                        "capture_interpretation_runtime_v1", {}
                    ),
                }
            )
        except (OSError, sqlite3.Error) as exc:
            LOGGER.warning("capture interpretation status unavailable: %s", exc)
            capture_health = {
                "enabled": bool(settings.capture_llm_enabled),
                "status": "UNAVAILABLE",
            }
        data["capture_interpretation"] = capture_health
        data["qwen_risk"] = {
            "enabled": bool(settings.qwen_risk_enabled),
            "runtime_state": (
                "ENABLED_SHADOW"
                if settings.qwen_risk_enabled
                else "DISABLED_NO_MODEL_CALLS"
            ),
            "publication": operations.qwen_risk_publication(),
            "runs": operations.qwen_risk_run_health(),
            "worker": operations.get_state("qwen_risk_worker_runtime_v1", {}),
            "public_projection_requires_approval": True,
            "input_identity_revalidated_before_display": True,
            "no_trading": True,
        }
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
