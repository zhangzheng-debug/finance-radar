from __future__ import annotations

from collections import Counter
from typing import Any

from app.models import RiskRouter, derive_evidence_context
from app.storage import LedgerRepository, OperationsRepository


SHADOW_FAIR_CURSOR_STATE_KEY = "shadow_router_fair_cursor_v1"


def event_model_text(detail: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    event = detail.get("event") or {}
    version = detail.get("current_version") or {}
    facts = version.get("facts") or {}
    preferred_source = detail.get("preferred_source") or {}
    return "\n".join(
        [
            str(event.get("company_name") or ""),
            str(preferred_source.get("title") or facts.get("source_title") or ""),
            str(preferred_source.get("summary") or facts.get("source_summary") or ""),
            str(facts.get("evidence_summary") or ""),
            *[str(item.get("evidence_passage") or "") for item in evidence[:5]],
        ]
    )


def execution_status(result: dict[str, Any]) -> str:
    if result.get("semantic_model_invoked") is True:
        return "MODEL_EXECUTED"
    if result.get("runtime") == "structured_evidence_gate":
        return "GATED_BEFORE_MODEL"
    if result.get("runtime") in {"semantic_policy_gate", "scope_guardrail"}:
        return "POLICY_GATE_DECISION"
    return "FALLBACK_DECISION"


def _event_id(item: dict[str, Any]) -> str:
    detail = item.get("detail") or {}
    event = detail.get("event") or {}
    return str(event.get("event_id") or "")


def _fair_shadow_batch(
    ledger: LedgerRepository,
    operations: OperationsRepository,
    *,
    scan_limit: int,
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, int]]:
    """Blend a recent lane with a durable round-robin ledger walk.

    The previous recent-only window could remain permanently full of already
    current events while an older event without a model run was never seen.
    Alternating lanes reserves throughput for both fresh changes and history.
    The cursor advances only across fair-lane rows actually examined, so a
    busy recent lane cannot silently skip the historical backlog.
    """

    recent_limit = max(1, scan_limit // 2)
    fair_limit = max(0, scan_limit - recent_limit)
    recent = ledger.shadow_batch(limit=recent_limit)
    if fair_limit == 0:
        return [("recent", item) for item in recent], {
            "recent_loaded": len(recent),
            "fair_loaded": 0,
            "fair_offset": 0,
            "fair_limit": 0,
        }

    state = operations.get_state(SHADOW_FAIR_CURSOR_STATE_KEY, {})
    try:
        fair_offset = max(0, int((state or {}).get("next_offset") or 0))
    except (AttributeError, TypeError, ValueError):
        fair_offset = 0
    fair = ledger.shadow_batch(
        limit=fair_limit,
        offset=fair_offset,
        order="event_id",
    )
    if not fair and fair_offset:
        fair_offset = 0
        fair = ledger.shadow_batch(limit=fair_limit, offset=0, order="event_id")

    selected: list[tuple[str, dict[str, Any]]] = []
    width = max(len(recent), len(fair))
    for index in range(width):
        if index < len(recent):
            selected.append(("recent", recent[index]))
        if index < len(fair):
            selected.append(("fair", fair[index]))
    return selected, {
        "recent_loaded": len(recent),
        "fair_loaded": len(fair),
        "fair_offset": fair_offset,
        "fair_limit": fair_limit,
    }


def run_shadow_batch(
    ledger: LedgerRepository,
    operations: OperationsRepository,
    router: RiskRouter,
    *,
    scan_limit: int = 200,
    run_limit: int = 100,
) -> dict[str, Any]:
    """Persist bounded, idempotent shadow outcomes for recent ledger events."""
    bounded_scan = max(1, min(scan_limit, 200))
    fast_loader = getattr(ledger, "shadow_batch", None)
    selection: dict[str, int] = {
        "recent_loaded": 0,
        "fair_loaded": 0,
        "fair_offset": 0,
        "fair_limit": 0,
    }
    if callable(fast_loader):
        try:
            selected_batch, selection = _fair_shadow_batch(
                ledger,
                operations,
                scan_limit=bounded_scan,
            )
        except TypeError:
            # Small third-party adapters may expose the original limit-only
            # method. Preserve that compatibility without weakening the
            # production repository's fair queue.
            selected_batch = [("recent", item) for item in fast_loader(limit=bounded_scan)]
            selection["recent_loaded"] = len(selected_batch)
    else:
        # Compatibility path for small adapters.  The production repository
        # always exposes shadow_batch() and avoids this public-reader/N+1 path.
        page = ledger.list_events(limit=bounded_scan)
        batch: list[dict[str, Any]] = []
        for row in page["items"]:
            event_id = str(row["event_id"])
            detail = ledger.event_detail(event_id)
            if detail is not None:
                batch.append(
                    {"detail": detail, "evidence": ledger.event_evidence(event_id)}
                )
        selected_batch = [("compatibility", item) for item in batch]
    counters: Counter[str] = Counter()
    errors: list[str] = []
    seen_event_ids: set[str] = set()
    fair_examined = 0
    fair_next_offset = 0
    for lane, item in selected_batch:
        if counters["recorded"] >= max(1, run_limit):
            break
        if lane == "fair":
            fair_examined += 1
        selected_event_id = _event_id(item)
        if selected_event_id and selected_event_id in seen_event_ids:
            counters["deduplicated"] += 1
            continue
        if selected_event_id:
            seen_event_ids.add(selected_event_id)
        detail = item.get("detail") or {}
        evidence = item.get("evidence") or []
        event = detail.get("event") or {}
        event_id = str(event.get("event_id") or "")
        if not event_id:
            counters["missing"] += 1
            continue
        try:
            text = event_model_text(detail, evidence)
            result = router.predict(text, evidence_context=derive_evidence_context(evidence))
            result.update(
                {
                    "event_version": int((detail.get("event") or {}).get("current_version") or 0),
                    "event_status": str((detail.get("event") or {}).get("status") or "unknown"),
                    "execution_status": execution_status(result),
                    "evidence_ids": [
                        str(item.get("evidence_id"))
                        for item in evidence[:5]
                        if item.get("evidence_id")
                    ],
                    "persisted_by": "live_worker_shadow_batch_v1",
                }
            )
            _run_id, created = operations.record_model_run_once(event_id, result)
            counters["recorded" if created else "already_current"] += 1
            counters[f"execution:{result['execution_status']}"] += int(created)
            counters[f"label:{result['label']}"] += int(created)
        except Exception as exc:  # one event must not stop the live worker
            counters["errors"] += 1
            errors.append(f"{event_id}:{type(exc).__name__}:{str(exc)[:240]}")

    if callable(fast_loader) and selection["fair_limit"]:
        fair_loaded = selection["fair_loaded"]
        if fair_examined >= fair_loaded and fair_loaded < selection["fair_limit"]:
            fair_next_offset = 0
        else:
            fair_next_offset = selection["fair_offset"] + fair_examined
        operations.set_state(
            SHADOW_FAIR_CURSOR_STATE_KEY,
            {
                "next_offset": fair_next_offset,
                "last_window_offset": selection["fair_offset"],
                "last_loaded": fair_loaded,
                "last_examined": fair_examined,
            },
        )
    return {
        "scanned": len(seen_event_ids),
        "recorded": counters["recorded"],
        "already_current": counters["already_current"],
        "missing": counters["missing"],
        "deduplicated": counters["deduplicated"],
        "errors": errors,
        "by_execution_status": {
            key.removeprefix("execution:"): value
            for key, value in counters.items()
            if key.startswith("execution:")
        },
        "by_label": {
            key.removeprefix("label:"): value
            for key, value in counters.items()
            if key.startswith("label:")
        },
        "shadow_only": True,
        "no_trading": True,
        "input_loader": "fair_recent_bulk_v3" if callable(fast_loader) else "compatibility_v1",
        "selection": {
            **selection,
            "fair_examined": fair_examined,
            "next_offset": fair_next_offset,
        },
    }
