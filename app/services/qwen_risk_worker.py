"""Independent fair queue for persisted Qwen semantic risk assessments."""

from __future__ import annotations

from collections import Counter
from typing import Any, Protocol

from app.storage import LedgerRepository, OperationsRepository


QWEN_RISK_FAIR_CURSOR_STATE_KEY = "qwen_risk_fair_cursor_v1"


class QwenProvider(Protocol):
    def input_contract(
        self, detail: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> dict[str, Any]: ...

    def assess(
        self, detail: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> dict[str, Any]: ...


def _event(item: dict[str, Any]) -> dict[str, Any]:
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    return detail.get("event") if isinstance(detail.get("event"), dict) else {}


def _fair_batch(
    ledger: LedgerRepository,
    operations: OperationsRepository,
    *,
    scan_limit: int,
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, int]]:
    recent_limit = max(1, scan_limit // 2)
    fair_limit = max(0, scan_limit - recent_limit)
    recent = ledger.shadow_batch(limit=recent_limit)
    state = operations.get_state(QWEN_RISK_FAIR_CURSOR_STATE_KEY, {})
    try:
        fair_offset = max(0, int((state or {}).get("next_offset") or 0))
    except (AttributeError, TypeError, ValueError):
        fair_offset = 0
    fair = (
        ledger.shadow_batch(limit=fair_limit, offset=fair_offset, order="event_id")
        if fair_limit
        else []
    )
    if fair_limit and not fair and fair_offset:
        fair_offset = 0
        fair = ledger.shadow_batch(limit=fair_limit, offset=0, order="event_id")
    selected: list[tuple[str, dict[str, Any]]] = []
    for index in range(max(len(recent), len(fair))):
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


def run_qwen_risk_batch(
    ledger: LedgerRepository,
    operations: OperationsRepository,
    provider: QwenProvider,
    *,
    scan_limit: int = 100,
    run_limit: int = 20,
) -> dict[str, Any]:
    """Assess a bounded recent/fair window without blocking collection or UI."""

    scan_limit = max(2, min(int(scan_limit), 200))
    run_limit = max(1, min(int(run_limit), 100))
    selected, selection = _fair_batch(ledger, operations, scan_limit=scan_limit)
    versions: dict[str, int] = {}
    for _lane, item in selected:
        event = _event(item)
        event_id = str(event.get("event_id") or "")
        try:
            version = int(event.get("current_version") or 0)
        except (TypeError, ValueError):
            version = 0
        if event_id and version > 0:
            versions[event_id] = version
    current = operations.latest_qwen_risk_runs_for_versions(versions)

    counters: Counter[str] = Counter()
    errors: list[str] = []
    seen: set[str] = set()
    fair_examined = 0
    for lane, item in selected:
        if counters["attempted"] >= run_limit:
            break
        if lane == "fair":
            fair_examined += 1
        event = _event(item)
        event_id = str(event.get("event_id") or "")
        if not event_id or event_id in seen:
            counters["deduplicated" if event_id else "missing"] += 1
            continue
        seen.add(event_id)
        detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
        evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
        try:
            contract = provider.input_contract(detail, evidence)
            if contract.get("input_sufficient") is False:
                counters["input_insufficient"] += 1
                continue
            previous = current.get(event_id)
            previous_output = previous.get("output") if isinstance(previous, dict) else {}
            if (
                isinstance(previous_output, dict)
                and previous_output.get("input_sha256") == contract.get("input_sha256")
                and previous_output.get("model_version") == contract.get("model_version")
            ):
                counters["already_current"] += 1
                continue
            counters["attempted"] += 1
            result = provider.assess(detail, evidence)
            _run_id, created = operations.record_model_run_once(event_id, result)
            counters["recorded" if created else "already_current"] += 1
            counters[f"priority:{result.get('semantic_priority')}"] += int(created)
            counters[f"scope:{result.get('assessment_scope')}"] += int(created)
        except Exception as exc:
            counters["errors"] += 1
            errors.append(f"{event_id}:{type(exc).__name__}:{str(exc)[:240]}")

    fair_loaded = selection["fair_loaded"]
    if fair_examined >= fair_loaded and fair_loaded < selection["fair_limit"]:
        next_offset = 0
    else:
        next_offset = selection["fair_offset"] + fair_examined
    if selection["fair_limit"]:
        operations.set_state(
            QWEN_RISK_FAIR_CURSOR_STATE_KEY,
            {
                "next_offset": next_offset,
                "last_window_offset": selection["fair_offset"],
                "last_loaded": fair_loaded,
                "last_examined": fair_examined,
            },
        )
    return {
        "scanned": len(seen),
        "attempted": counters["attempted"],
        "recorded": counters["recorded"],
        "already_current": counters["already_current"],
        "input_insufficient": counters["input_insufficient"],
        "errors": errors,
        "by_priority": {
            key.removeprefix("priority:"): value
            for key, value in counters.items()
            if key.startswith("priority:")
        },
        "by_scope": {
            key.removeprefix("scope:"): value
            for key, value in counters.items()
            if key.startswith("scope:")
        },
        "selection": {**selection, "fair_examined": fair_examined, "next_offset": next_offset},
        "independent_from_collection": True,
        "persisted_before_display": True,
        "no_trading": True,
    }
