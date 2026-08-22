from __future__ import annotations

from collections import Counter
from typing import Any

from app.models import RiskRouter, derive_evidence_context
from app.storage import LedgerRepository, OperationsRepository


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
    if callable(fast_loader):
        batch = fast_loader(limit=bounded_scan)
    else:
        # Compatibility path for small adapters.  The production repository
        # always exposes shadow_batch() and avoids this public-reader/N+1 path.
        page = ledger.list_events(limit=bounded_scan)
        batch = []
        for row in page["items"]:
            event_id = str(row["event_id"])
            detail = ledger.event_detail(event_id)
            if detail is not None:
                batch.append(
                    {"detail": detail, "evidence": ledger.event_evidence(event_id)}
                )
    counters: Counter[str] = Counter()
    errors: list[str] = []
    for item in batch:
        if counters["recorded"] >= max(1, run_limit):
            break
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
    return {
        "scanned": len(batch),
        "recorded": counters["recorded"],
        "already_current": counters["already_current"],
        "missing": counters["missing"],
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
        "input_loader": "bounded_bulk_v2" if callable(fast_loader) else "compatibility_v1",
    }
