from __future__ import annotations

from collections.abc import Callable
from typing import Any


READ_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("health", "/api/v1/health"),
    ("overview", "/api/v1/overview"),
    ("sources", "/api/v1/sources/health"),
    ("evidence", "/api/v1/evidence/archive"),
    ("model", "/api/v1/model/status"),
)


def fetch_admin_read_snapshot(
    request: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    """Fetch independent read models so one unavailable endpoint cannot blank Admin."""

    data: dict[str, dict[str, Any]] = {}
    unavailable: list[str] = []
    for key, path in READ_ENDPOINTS:
        try:
            payload = request(path)
        except Exception:
            unavailable.append(key)
            continue
        if isinstance(payload, dict):
            data[key] = payload
        else:
            unavailable.append(key)
    return {"data": data, "unavailable": unavailable}


def _as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summarize_admin_read_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project existing API fields into a small, honest owner-facing summary."""

    payloads = _as_mapping(snapshot.get("data"))
    health = _as_mapping(payloads.get("health"))
    overview = _as_mapping(payloads.get("overview"))
    source_payload = _as_mapping(payloads.get("sources"))
    evidence = _as_mapping(payloads.get("evidence"))
    model = _as_mapping(payloads.get("model"))

    operations = _as_mapping(health.get("operations"))
    operation_counts = _as_mapping(operations.get("counts"))
    timing = _as_mapping(overview.get("timing"))
    latest_worker = _as_mapping(
        overview.get("latest_worker_cycle") or operations.get("latest_worker_cycle")
    )
    sources = [item for item in _as_list(source_payload.get("items")) if isinstance(item, dict)]
    source_failures = [
        item
        for item in sources
        if item.get("last_error")
        or str(item.get("cursor_status") or "").upper() in {"ERROR", "FAILED"}
    ]

    reader_quality = _as_mapping(overview.get("reader_quality"))
    total_events = _optional_int(
        reader_quality.get("total")
        if reader_quality.get("total") is not None
        else _as_mapping(overview.get("counts")).get("canonical_events")
    )
    citation_ready = _optional_int(
        reader_quality.get("citation_ready")
        if reader_quality.get("citation_ready") is not None
        else reader_quality.get("reader_ready")
    )
    archive_coverage = _as_mapping(evidence.get("coverage"))

    ledger = _as_mapping(health.get("ledger"))
    backup_snapshot = _as_mapping(ledger.get("backup_snapshot"))
    latest_verified_backup = _as_mapping(
        operations.get("latest_verified_backup") or operations.get("latest_backup")
    )
    external_blind = _as_mapping(model.get("external_blind"))
    capture_interpretation = _as_mapping(model.get("capture_interpretation"))
    capture_by_status = _as_mapping(capture_interpretation.get("by_status"))
    qwen_risk = _as_mapping(model.get("qwen_risk"))
    qwen_publication = _as_mapping(qwen_risk.get("publication"))
    audit_reconciliation = _as_mapping(operations.get("audit_reconciliation"))
    ledger_audit = _as_mapping(ledger.get("audit"))
    audit_violations = sum(
        number
        for value in ledger_audit.values()
        if (number := _optional_int(value)) is not None
    )

    return {
        "release": {
            "service_version": health.get("service_version"),
            # The current read API intentionally does not publish the immutable
            # release ID. Do not guess it from the semantic version.
            "release_id": None,
        },
        "worker": {
            "status": latest_worker.get("status"),
            "last_success_age_seconds": _optional_float(
                timing.get("latest_worker_success_age_seconds")
            ),
            "last_success_at": timing.get("latest_worker_success_at"),
        },
        "sources": {
            "total": len(sources) if "sources" in payloads else None,
            "failures": len(source_failures) if "sources" in payloads else None,
            "failure_names": [
                str(item.get("name") or item.get("source_id") or "unknown source")
                for item in source_failures[:8]
            ],
        },
        "interpretation": {
            "recorded_runs": _optional_int(operation_counts.get("capture_interpretation_runs")),
            "pending_backlog": (
                sum(
                    int(capture_by_status.get(key) or 0)
                    for key in ("PENDING", "BUDGET_BLOCKED", "RUNNING")
                )
                if capture_by_status
                else None
            ),
            "limitation": (
                None
                if capture_by_status
                else "当前只读 API 未提供解读队列的状态分组"
            ),
        },
        "evidence": {
            "total_events": total_events,
            "citation_ready": citation_ready,
            "archive_coverage_pct": _optional_float(archive_coverage.get("coverage_pct")),
            "missing_archive_links": _optional_int(archive_coverage.get("missing_links")),
        },
        "backup": {
            # This is the only current backup-health signal: it combines the
            # durable record with age and a live path-visibility probe.
            "status": backup_snapshot.get("status"),
            "fresh": backup_snapshot.get("fresh") if "fresh" in backup_snapshot else None,
            "age_seconds": _optional_float(backup_snapshot.get("age_seconds")),
            "verified_at": backup_snapshot.get("verified_at"),
            "quick_check": backup_snapshot.get("quick_check"),
            "path_available": (
                backup_snapshot.get("path_available")
                if "path_available" in backup_snapshot
                else None
            ),
            "artifact_visibility": backup_snapshot.get("artifact_visibility"),
            # Retain the latest successful row only as history. Its VERIFIED
            # status must never override a stale or missing current snapshot.
            "last_verified_record_status": latest_verified_backup.get("status"),
            "last_verified_record_at": latest_verified_backup.get("verified_at"),
        },
        "model": {
            "status": model.get("status"),
            "recent_runs": len(_as_list(model.get("recent_runs"))) if "model" in payloads else None,
            "external_blind_rows": _optional_int(external_blind.get("rows")),
            "external_blind_gate_pass": (
                external_blind.get("gate_pass")
                if isinstance(external_blind.get("gate_pass"), bool)
                else None
            ),
            "promotion_decision": external_blind.get("promotion_decision"),
            "evaluation_type": external_blind.get("evaluation_type"),
            **(
                {
                    "qwen_runtime_state": qwen_risk.get("runtime_state"),
                    "qwen_publication_state": qwen_publication.get("state"),
                    "qwen_public_approved": qwen_publication.get("public_approved"),
                }
                if qwen_risk
                else {}
            ),
        },
        "audit": {
            "status": audit_reconciliation.get("status"),
            "pending_reconciliation": _optional_int(
                audit_reconciliation.get("pending_reconciliation")
            ),
            "recovery_conflicts": _optional_int(audit_reconciliation.get("recovery_conflicts")),
            "boundary_violations": audit_violations if ledger_audit else None,
            # The API intentionally exposes reconciliation state, not recent
            # audit row payloads or their timestamps.
            "latest_at": None,
            "limitation": "当前只读 API 未提供最近审计记录时间",
        },
        "unavailable": [str(value) for value in _as_list(snapshot.get("unavailable"))],
    }
