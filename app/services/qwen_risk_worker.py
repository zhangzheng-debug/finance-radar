"""Independent fair queue for persisted Qwen semantic risk assessments."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Protocol

from app.models.qwen_risk_contract import (
    QWEN_RISK_CONTRACT_VERSION,
    QWEN_RISK_PROMPT_VERSION,
)
from app.storage import LedgerRepository, OperationsRepository


QWEN_RISK_FAIR_CURSOR_STATE_KEY = "qwen_risk_fair_cursor_v2"
QwenCandidate = tuple[
    str,
    int,
    str,
    str,
    dict[str, Any],
    list[dict[str, Any]],
]
QwenIdentity = tuple[str, int, str, str]
QwenOutcome = tuple[QwenIdentity, dict[str, Any] | None, Exception | None]


class QwenProvider(Protocol):
    model_version: str

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
    operations: OperationsRepository,
    candidate: QwenCandidate,
) -> QwenOutcome:
    event_id, event_version, input_sha256, model_version, detail, evidence = candidate
    identity = (event_id, event_version, input_sha256, model_version)
    try:
        operations.set_qwen_risk_activity(*identity, "RUNNING")
        result = provider.assess(detail, evidence)
        if (
            str(result.get("input_sha256") or "") != input_sha256
            or str(result.get("model_version") or "") != model_version
            or int(result.get("event_version") or 0) != event_version
        ):
            raise RuntimeError("QWEN_RISK_RESULT_IDENTITY_MISMATCH")
        return identity, result, None
    except Exception as exc:  # isolated per immutable event input
        try:
            operations.schedule_qwen_risk_retry(
                *identity,
                error_code=_error_code(exc),
            )
        except Exception:
            pass
        return identity, None, exc


def _error_code(exc: Exception) -> str:
    message = str(exc or "").strip()
    if message.startswith("QWEN_RISK_"):
        return message.split(":", 1)[0][:120]
    return type(exc).__name__[:120]


def _utc_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _persist_outcome(
    operations: OperationsRepository,
    counters: Counter[str],
    errors: list[str],
    outcome: QwenOutcome,
) -> None:
    identity, result, error = outcome
    event_id = identity[0]
    if error is not None or result is None:
        exc = error or RuntimeError("QWEN_RISK_EMPTY_RESULT")
        counters["errors"] += 1
        errors.append(f"{event_id}:{_error_code(exc)}")
        return
    try:
        _run_id, created = operations.record_model_run_once(event_id, result)
        operations.set_qwen_risk_activity(*identity, "READY")
        counters["recorded" if created else "already_current"] += 1
        counters[f"priority:{result.get('semantic_priority')}"] += int(created)
        counters[f"scope:{result.get('assessment_scope')}"] += int(created)
    except Exception as exc:
        try:
            operations.schedule_qwen_risk_retry(
                *identity,
                error_code=_error_code(exc),
            )
        except Exception:
            pass
        counters["errors"] += 1
        errors.append(f"{event_id}:{_error_code(exc)}")


def _prepare_priority_candidate(
    ledger: LedgerRepository,
    operations: OperationsRepository,
    provider: QwenProvider,
    claim: dict[str, Any],
) -> tuple[QwenCandidate | None, str | None]:
    identity: QwenIdentity = (
        str(claim.get("event_id") or ""),
        int(claim.get("event_version") or 0),
        str(claim.get("input_sha256") or ""),
        str(claim.get("model_version") or ""),
    )
    event_id, event_version, input_sha256, model_version = identity
    try:
        items = ledger.shadow_batch(
            limit=1,
            order="event_id",
            event_ids=[event_id],
            semantic_events_only=True,
        )
    except Exception as exc:
        error_code = _error_code(exc)
        operations.schedule_qwen_risk_retry(*identity, error_code=error_code)
        return None, error_code
    if not items:
        operations.set_qwen_risk_activity(
            *identity,
            "FAILED",
            error_code="PRIORITY_EVENT_UNAVAILABLE",
            retryable=False,
        )
        return None, "PRIORITY_EVENT_UNAVAILABLE"

    item = items[0] if isinstance(items[0], dict) else {}
    event = _event(item)
    detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
    evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
    try:
        current_version = int(event.get("current_version") or 0)
        contract = provider.input_contract(detail, evidence)
    except Exception as exc:
        error_code = _error_code(exc)
        operations.schedule_qwen_risk_retry(*identity, error_code=error_code)
        return None, error_code
    if contract.get("input_sufficient") is False:
        operations.set_qwen_risk_activity(
            *identity,
            "FAILED",
            error_code="QWEN_RISK_INPUT_INSUFFICIENT",
            retryable=False,
        )
        return None, "QWEN_RISK_INPUT_INSUFFICIENT"
    if (
        current_version != event_version
        or str(contract.get("input_sha256") or "") != input_sha256
        or str(contract.get("model_version") or "") != model_version
    ):
        operations.set_qwen_risk_activity(
            *identity,
            "FAILED",
            error_code="PRIORITY_INPUT_STALE",
            retryable=False,
        )
        return None, "PRIORITY_INPUT_STALE"

    previous = operations.latest_qwen_risk_runs_for_versions(
        {event_id: event_version},
        model_version=model_version,
        contract_version=QWEN_RISK_CONTRACT_VERSION,
        prompt_version=QWEN_RISK_PROMPT_VERSION,
    ).get(event_id)
    previous_output = previous.get("output") if isinstance(previous, dict) else {}
    if (
        isinstance(previous_output, dict)
        and previous_output.get("input_sha256") == input_sha256
        and previous_output.get("model_version") == model_version
    ):
        operations.set_qwen_risk_activity(*identity, "READY")
        return None, "ALREADY_CURRENT"
    return (*identity, detail, evidence), None


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
    current = operations.latest_qwen_risk_runs_for_versions(
        versions,
        model_version=provider.model_version,
        contract_version=QWEN_RISK_CONTRACT_VERSION,
        prompt_version=QWEN_RISK_PROMPT_VERSION,
    )

    counters: Counter[str] = Counter()
    errors: list[str] = []
    seen: set[str] = set()
    fair_examined = 0
    last_fair_event_id: str | None = None
    background_candidates: list[tuple[str, QwenCandidate]] = []
    for lane, item in selected:
        if len(background_candidates) >= run_limit:
            break
        event = _event(item)
        event_id = str(event.get("event_id") or "")
        try:
            version = int(event.get("current_version") or 0)
        except (TypeError, ValueError):
            version = 0
        if lane == "fair":
            fair_examined += 1
            if event_id:
                last_fair_event_id = event_id
        if not event_id or version <= 0:
            counters["missing"] += 1
            continue
        if event_id in seen:
            counters["deduplicated"] += 1
            continue
        seen.add(event_id)
        detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
        evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
        try:
            contract = provider.input_contract(detail, evidence)
            if contract.get("input_sufficient") is False:
                counters["input_insufficient"] += 1
                continue
            input_sha256 = str(contract.get("input_sha256") or "")
            model_version = str(contract.get("model_version") or "")
            identity = (event_id, version, input_sha256, model_version)
            activity = operations.qwen_risk_activity(*identity)
            activity_state = str((activity or {}).get("state") or "").upper()
            previous = current.get(event_id)
            previous_output = previous.get("output") if isinstance(previous, dict) else {}
            if (
                isinstance(previous_output, dict)
                and previous_output.get("input_sha256") == contract.get("input_sha256")
                and previous_output.get("model_version") == contract.get("model_version")
            ):
                counters["already_current"] += 1
                if activity_state != "READY":
                    operations.set_qwen_risk_activity(*identity, "READY")
                continue
            if activity_state == "QUEUED" and lane != "priority":
                counters["priority_waiting"] += 1
                continue
            if activity_state == "RUNNING" and lane != "priority":
                counters["already_running"] += 1
                continue
            retry_after = _utc_datetime((activity or {}).get("retry_after"))
            if (
                activity_state == "FAILED"
                and retry_after is not None
                and retry_after > datetime.now(timezone.utc)
            ):
                counters["retry_deferred"] += 1
                continue
            background_candidates.append(
                (lane, (*identity, detail, evidence))
            )
        except Exception as exc:
            counters["errors"] += 1
            errors.append(f"{event_id}:{_error_code(exc)}")

    background_used: set[int] = set()
    scheduled_identities: set[QwenIdentity] = set()
    fair_scheduled = 0
    priority_lane_available = True

    def take_background(*, fair_only: bool = False) -> QwenCandidate | None:
        nonlocal fair_scheduled
        for index, (lane, candidate) in enumerate(background_candidates):
            if index in background_used or (fair_only and lane != "fair"):
                continue
            background_used.add(index)
            identity = candidate[:4]
            if identity in scheduled_identities:
                counters["deduplicated"] += 1
                continue
            activity = operations.qwen_risk_activity(*identity)
            state = str((activity or {}).get("state") or "").upper()
            if state == "QUEUED":
                counters["priority_waiting"] += 1
                continue
            if state == "RUNNING":
                counters["already_running"] += 1
                continue
            retry_after = _utc_datetime((activity or {}).get("retry_after"))
            if (
                state == "FAILED"
                and retry_after is not None
                and retry_after > datetime.now(timezone.utc)
            ):
                counters["retry_deferred"] += 1
                continue
            fair_scheduled += int(lane == "fair")
            return candidate
        return None

    def next_candidate(*, reserve_fair: bool = False) -> QwenCandidate | None:
        nonlocal priority_lane_available
        if reserve_fair:
            fair_candidate = take_background(fair_only=True)
            if fair_candidate is not None:
                return fair_candidate
        while priority_lane_available:
            try:
                claim = operations.claim_qwen_risk_priority(provider.model_version)
            except Exception as exc:
                priority_lane_available = False
                counters["errors"] += 1
                errors.append(f"priority:{_error_code(exc)}")
                break
            if claim is None:
                break
            counters["priority_claimed"] += 1
            try:
                priority_candidate, priority_status = _prepare_priority_candidate(
                    ledger, operations, provider, claim
                )
            except Exception as exc:
                priority_candidate = None
                priority_status = _error_code(exc)
            if priority_candidate is not None:
                if priority_candidate[:4] in scheduled_identities:
                    counters["deduplicated"] += 1
                    continue
                return priority_candidate
            event_id = str(claim.get("event_id") or "")
            if priority_status == "ALREADY_CURRENT":
                counters["already_current"] += 1
            elif priority_status == "QWEN_RISK_INPUT_INSUFFICIENT":
                counters["input_insufficient"] += 1
            elif priority_status:
                counters["errors"] += 1
                errors.append(f"{event_id}:{priority_status}")
        return take_background()

    pending: dict[Future[QwenOutcome], QwenIdentity] = {}

    # Start one item synchronously.  A claimed public request therefore reaches
    # the provider before speculative fair work, while later slots still retain
    # bounded parallelism and are replenished completion-by-completion.
    first_candidate = next_candidate()
    if first_candidate is not None:
        first_identity = first_candidate[:4]
        scheduled_identities.add(first_identity)
        counters["attempted"] += 1
        _persist_outcome(
            operations,
            counters,
            errors,
            _assess_candidate(provider, operations, first_candidate),
        )

    with ThreadPoolExecutor(
        max_workers=concurrency,
        thread_name_prefix="qwen-risk",
    ) as executor:

        def submit_next() -> bool:
            if counters["attempted"] >= run_limit:
                return False
            reserve_fair = (
                run_limit > 1
                and fair_scheduled == 0
                and counters["attempted"] == run_limit - 1
            )
            candidate = next_candidate(reserve_fair=reserve_fair)
            if candidate is None:
                return False
            identity = candidate[:4]
            scheduled_identities.add(identity)
            counters["attempted"] += 1
            pending[executor.submit(
                _assess_candidate, provider, operations, candidate
            )] = identity
            return True

        while len(pending) < concurrency and submit_next():
            pass
        while pending:
            done, _not_done = wait(tuple(pending), return_when=FIRST_COMPLETED)
            for future in done:
                identity = pending.pop(future)
                try:
                    outcome = future.result()
                except Exception as exc:
                    outcome = (identity, None, exc)
                _persist_outcome(operations, counters, errors, outcome)
                # The replacement decision is made after each completion, not
                # after the whole fair batch, so a public priority request can
                # never sit behind the remaining slow model calls.
                submit_next()

    fair_loaded = selection["fair_loaded"]
    unscheduled_background = len(background_used) < len(background_candidates)
    cursor_fair_examined = 0 if unscheduled_background else fair_examined
    cursor_last_fair_event_id = (
        None if unscheduled_background else last_fair_event_id
    )
    if cursor_fair_examined >= fair_loaded and fair_loaded < selection["fair_limit"]:
        next_after_event_id = None
    else:
        next_after_event_id = (
            cursor_last_fair_event_id or selection["fair_after_event_id"]
        )
    if selection["fair_limit"]:
        operations.set_state(
            QWEN_RISK_FAIR_CURSOR_STATE_KEY,
            {
                "after_event_id": next_after_event_id,
                "last_window_after_event_id": selection["fair_after_event_id"],
                "last_loaded": fair_loaded,
                "last_examined": cursor_fair_examined,
            },
        )
    return {
        "scanned": len(seen),
        "attempted": counters["attempted"],
        "recorded": counters["recorded"],
        "already_current": counters["already_current"],
        "input_insufficient": counters["input_insufficient"],
        "priority_claimed": counters["priority_claimed"],
        "priority_waiting": counters["priority_waiting"],
        "already_running": counters["already_running"],
        "retry_deferred": counters["retry_deferred"],
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
            "fair_examined": cursor_fair_examined,
            "next_after_event_id": next_after_event_id,
        },
        "concurrency": concurrency,
        "independent_from_collection": True,
        "persisted_before_display": True,
        "no_trading": True,
    }
