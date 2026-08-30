"""Independent fair queue for persisted Qwen semantic risk assessments."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol

from app.storage import LedgerRepository, OperationsRepository


QWEN_RISK_FAIR_CURSOR_STATE_KEY = "qwen_risk_fair_cursor_v2"


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
) -> tuple[list[tuple[str, dict[str, Any]]], dict[str, Any]]:
    recent_limit = max(1, scan_limit // 2)
    fair_limit = max(0, scan_limit - recent_limit)
    recent = ledger.shadow_batch(
        limit=recent_limit,
        semantic_events_only=True,
    )
    state = operations.get_state(QWEN_RISK_FAIR_CURSOR_STATE_KEY, {})
    fair_after_event_id = str((state or {}).get("after_event_id") or "").strip() or None
    fair = (
        ledger.shadow_batch(
            limit=fair_limit,
            order="event_id",
            after_event_id=fair_after_event_id,
            semantic_events_only=True,
        )
        if fair_limit
        else []
    )
    if fair_limit and not fair and fair_after_event_id:
        fair_after_event_id = None
        fair = ledger.shadow_batch(
            limit=fair_limit,
            order="event_id",
            semantic_events_only=True,
        )
    selected: list[tuple[str, dict[str, Any]]] = []
    for index in range(max(len(recent), len(fair))):
        if index < len(recent):
            selected.append(("recent", recent[index]))
        if index < len(fair):
            selected.append(("fair", fair[index]))
    return selected, {
        "recent_loaded": len(recent),
        "fair_loaded": len(fair),
        "fair_after_event_id": fair_after_event_id,
        "fair_limit": fair_limit,
    }


def _assess_candidate(
    provider: QwenProvider,
    candidate: tuple[str, dict[str, Any], list[dict[str, Any]]],
) -> tuple[str, dict[str, Any] | None, Exception | None]:
    event_id, detail, evidence = candidate
    try:
        return event_id, provider.assess(detail, evidence), None
    except Exception as exc:  # isolated per immutable event input
        return event_id, None, exc


def run_qwen_risk_batch(
    ledger: LedgerRepository,
    operations: OperationsRepository,
    provider: QwenProvider,
    *,
    scan_limit: int = 100,
    run_limit: int = 20,
    concurrency: int = 1,
) -> dict[str, Any]:
    """Assess a bounded recent/fair window without blocking collection or UI."""

    scan_limit = max(2, min(int(scan_limit), 200))
    run_limit = max(1, min(int(run_limit), 100))
    concurrency = max(1, min(int(concurrency), 4))
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
    last_fair_event_id: str | None = None
    candidates: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    for lane, item in selected:
        if counters["attempted"] >= run_limit:
            break
        event = _event(item)
        event_id = str(event.get("event_id") or "")
        if lane == "fair":
            fair_examined += 1
            if event_id:
                last_fair_event_id = event_id
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
            candidates.append((event_id, detail, evidence))
        except Exception as exc:
            counters["errors"] += 1
            errors.append(f"{event_id}:{type(exc).__name__}:{str(exc)[:240]}")

    if concurrency == 1:
        outcomes = [_assess_candidate(provider, candidate) for candidate in candidates]
    else:
        with ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="qwen-risk",
        ) as executor:
            outcomes = list(
                executor.map(
                    lambda candidate: _assess_candidate(provider, candidate),
                    candidates,
                )
            )
    for event_id, result, error in outcomes:
        if error is not None or result is None:
            exc = error or RuntimeError("QWEN_RISK_EMPTY_RESULT")
            counters["errors"] += 1
            errors.append(f"{event_id}:{type(exc).__name__}:{str(exc)[:240]}")
            continue
        try:
            _run_id, created = operations.record_model_run_once(event_id, result)
            counters["recorded" if created else "already_current"] += 1
            counters[f"priority:{result.get('semantic_priority')}"] += int(created)
            counters[f"scope:{result.get('assessment_scope')}"] += int(created)
        except Exception as exc:
            counters["errors"] += 1
            errors.append(f"{event_id}:{type(exc).__name__}:{str(exc)[:240]}")

    fair_loaded = selection["fair_loaded"]
    if fair_examined >= fair_loaded and fair_loaded < selection["fair_limit"]:
        next_after_event_id = None
    else:
        next_after_event_id = (
            last_fair_event_id or selection["fair_after_event_id"]
        )
    if selection["fair_limit"]:
        operations.set_state(
            QWEN_RISK_FAIR_CURSOR_STATE_KEY,
            {
                "after_event_id": next_after_event_id,
                "last_window_after_event_id": selection["fair_after_event_id"],
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
        "selection": {
            **selection,
            "fair_examined": fair_examined,
            "next_after_event_id": next_after_event_id,
        },
        "concurrency": concurrency,
        "independent_from_collection": True,
        "persisted_before_display": True,
        "no_trading": True,
    }
