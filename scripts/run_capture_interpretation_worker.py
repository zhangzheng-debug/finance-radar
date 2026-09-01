#!/usr/bin/env python3
"""Bounded scheduler for advisory interpretations of retained API captures.

This worker only considers P2/raw-only records without current reader-eligible
evidence. It never changes the canonical ledger, never treats an interpretation
as evidence, and delegates every external call to the receipt-bound, atomically
budgeted single-job runner.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.services.capture_interpretation import (  # noqa: E402
    CAPTURE_INTERPRETATION_CONTRACT,
    CAPTURE_INTERPRETATION_PROMPT_SHA256,
    CAPTURE_INTERPRETATION_PROMPT_VERSION,
    normalized_capture_input,
)
from app.services.deepseek_capture_interpretation import (  # noqa: E402
    DEEPSEEK_CHEAP_TEXT_MODEL,
)
from app.storage import LedgerRepository, OperationsRepository  # noqa: E402
from scripts.run_capture_interpretation_deepseek import (  # noqa: E402
    MAX_ATTEMPTS,
    RUN_CACHED,
    RUN_COMPLETED,
    load_local_env,
    run as run_single,
)


PRIORITY = {"NO_URL_RAW_ONLY": 0, "P2_CAPTURE_ONLY": 1}
INVENTORY_STATE_KEY = "capture_interpretation_inventory_v3"
RUNTIME_STATE_KEY = "capture_interpretation_runtime_v1"
PERSISTED_PENDING_SCAN_LIMIT = 500
INVENTORY_MIN_INTERVAL_SECONDS = 60


def is_current_terminal(run: dict[str, Any] | None) -> bool:
    if not run or str(run.get("status") or "") not in {"COMPLETED", "FAILED"}:
        return False
    return (
        str(run.get("contract_version") or "") == CAPTURE_INTERPRETATION_CONTRACT
        and str(run.get("prompt_version") or "")
        == CAPTURE_INTERPRETATION_PROMPT_VERSION
        and str(run.get("prompt_sha256") or "")
        == CAPTURE_INTERPRETATION_PROMPT_SHA256
        and str(run.get("provider") or "") == "deepseek"
        and str(run.get("model_snapshot") or "") == DEEPSEEK_CHEAP_TEXT_MODEL
    )


def _safe_json(**values: Any) -> None:
    print(json.dumps(values, ensure_ascii=False, sort_keys=True))


def classify_run_code(code: int) -> str:
    if code == RUN_COMPLETED:
        return "COMPLETED"
    if code == RUN_CACHED:
        return "CACHED"
    return "FAILED"


def candidates(plan: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    records = sorted(
        plan.get("records") or [],
        key=lambda item: (
            PRIORITY.get(str(item.get("bucket")), 99),
            str((item.get("event") or {}).get("event_id") or ""),
        ),
    )
    for record in records:
        bucket = str(record.get("bucket") or "")
        event_id = str((record.get("event") or {}).get("event_id") or "")
        if bucket not in PRIORITY or not event_id:
            continue
        for capture in record.get("captures") or []:
            if str(capture.get("observation_status") or "") == "deleted":
                continue
            if not str(capture.get("title") or capture.get("summary") or "").strip():
                continue
            observation_id = str(capture.get("observation_id") or "")
            receipt = str(capture.get("capture_receipt_sha256") or "")
            key = (event_id, receipt)
            if observation_id and receipt and key not in seen:
                seen.add(key)
                result.append(
                    {
                        "event_id": event_id,
                        "observation_id": observation_id,
                        "capture_receipt_sha256": receipt,
                        "bucket": bucket,
                    }
                )
    return result


def process_pending_item(item: dict[str, str], env_file: Path) -> str:
    """Run one independently claimed receipt without leaking provider details."""

    try:
        code = run_single(
            SimpleNamespace(
                event_id=item["event_id"],
                observation_id=item["observation_id"],
                env_file=env_file,
                public_request=(
                    str(item.get("scheduler_lane") or "") == "public_priority"
                ),
            )
        )
        return classify_run_code(code)
    except RuntimeError as exc:
        code = str(exc)
        if "DAILY_REQUEST_CAP_REACHED" in code or "DAILY_CNY_CAP_REACHED" in code:
            return "BUDGET_DEFERRED"
        if "NO_ELIGIBLE_JOB" in code:
            return "DEFERRED"
        return "FAILED"
    except Exception:
        # The single-job runner already stores a redacted failure class and
        # usage accounting.  Do not echo source text or provider bodies.
        return "FAILED"


def process_pending_items(
    items: list[dict[str, str]],
    env_file: Path,
    *,
    workers: int,
) -> list[str]:
    """Process a bounded receipt set with independent atomic claims.

    SQLite claims and provider usage reservations remain transactional.  The
    small thread pool only overlaps network waits; it does not share a ledger
    connection or permit a canonical mutation.
    """

    if not items:
        return []
    if workers <= 1:
        return [process_pending_item(item, env_file) for item in items]
    with ThreadPoolExecutor(
        max_workers=min(workers, len(items)),
        thread_name_prefix="capture-interpretation",
    ) as executor:
        return list(
            executor.map(
                lambda item: process_pending_item(item, env_file),
                items,
            )
        )


def current_persisted_pending_requests(
    operations: OperationsRepository,
    *,
    limit: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return current-generation jobs already persisted by an API request.

    ``enqueue_capture_interpretation`` is the shared idempotent write path. The
    worker deliberately reads that durable queue before discovering new
    inventory, so a page request is real work rather than a presentation-only
    loading state. Terminal failures are not selected or retried.
    """

    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    bounded_limit = max(1, min(int(limit), PERSISTED_PENDING_SCAN_LIMIT))
    return operations.capture_interpretation_pending_runs(
        provider="deepseek",
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
        available_before=observed_at.isoformat(),
        max_attempts=MAX_ATTEMPTS,
        limit=bounded_limit,
    )


def current_public_priority_requests(
    operations: OperationsRepository,
    *,
    limit: int = 1,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    observed_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return operations.capture_interpretation_priority_runs(
        provider="deepseek",
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
        available_before=observed_at.isoformat(),
        max_attempts=MAX_ATTEMPTS,
        limit=max(1, int(limit)),
    )


def prepare_persisted_pending_requests(
    ledger: LedgerRepository,
    operations: OperationsRepository,
    rows: list[dict[str, Any]],
    *,
    limit: int,
    public_priority: bool = False,
) -> tuple[list[dict[str, str]], int]:
    """Bind queued rows to current ledger inputs and reject stale work once."""

    prepared: list[dict[str, str]] = []
    stale_rejected = 0
    for row in rows:
        if len(prepared) >= max(1, int(limit)):
            break
        interpretation_id = str(row.get("interpretation_id") or "")
        event_id = str(row.get("event_id") or "")
        observation_id = str(row.get("observation_id") or "")
        eligibility = ledger.capture_interpretation_eligibility(
            event_id,
            observation_id=observation_id,
        )
        reason = str(eligibility.get("reason_code") or "UNKNOWN")
        context = (
            ledger.capture_interpretation_context(event_id, observation_id)
            if eligibility.get("eligible")
            else None
        )
        if context is None:
            operations.fail_capture_interpretation(
                interpretation_id,
                "CAPTURE_INTERPRETATION_NOT_ELIGIBLE:" + reason,
            )
            stale_rejected += 1
            continue
        normalized = normalized_capture_input(
            dict(context.get("event") or {}),
            dict(context.get("capture") or {}),
        )
        if (
            str(normalized.get("capture_receipt_sha256") or "")
            != str(row.get("capture_receipt_sha256") or "")
            or str(normalized.get("input_sha256") or "")
            != str(row.get("input_sha256") or "")
        ):
            replacement: dict[str, Any] = {}
            if public_priority:
                replacement = operations.replace_stale_capture_interpretation_priority(
                    interpretation_id,
                    event_id=event_id,
                    observation_id=observation_id,
                    input_payload=normalized,
                    contract_version=CAPTURE_INTERPRETATION_CONTRACT,
                    prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
                    prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
                    provider="deepseek",
                    model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
                    max_attempts=MAX_ATTEMPTS,
                )
            if not replacement.get("replaced"):
                operations.fail_capture_interpretation(
                    interpretation_id,
                    "CAPTURE_INTERPRETATION_STALE_INPUT",
                )
            stale_rejected += 1
            if replacement.get("queued"):
                prepared.append(
                    {
                        "interpretation_id": str(
                            replacement.get("interpretation_id") or ""
                        ),
                        "event_id": event_id,
                        "observation_id": observation_id,
                        "capture_receipt_sha256": str(
                            normalized.get("capture_receipt_sha256") or ""
                        ),
                        "bucket": str(eligibility.get("bucket") or ""),
                        "scheduler_lane": "public_priority",
                    }
                )
            continue
        prepared.append(
            {
                "interpretation_id": interpretation_id,
                "event_id": event_id,
                "observation_id": observation_id,
                "capture_receipt_sha256": str(
                    row.get("capture_receipt_sha256") or ""
                ),
                "bucket": str(eligibility.get("bucket") or ""),
                "scheduler_lane": (
                    "public_priority" if public_priority else "persisted_pending"
                ),
            }
        )
    return prepared, stale_rejected


def run(args: argparse.Namespace) -> int:
    load_local_env(args.env_file.resolve())
    settings = Settings.from_env()
    ledger = LedgerRepository(settings.ledger_db)
    operations = OperationsRepository(settings.operations_db)
    generation = ledger.capture_source_generation()
    prior_inventory = operations.get_state(INVENTORY_STATE_KEY, {})

    # A public request is durable and rechecked after every item.  Keep one
    # slot for fair inventory whenever the configured batch has room.
    priority_capacity = max(1, int(args.limit) - 1) if int(args.limit) > 1 else 1
    priority_work: list[dict[str, str]] = []
    priority_outcomes: list[str] = []
    seen_priority: set[str] = set()
    priority_stale_rejected = 0
    while len(priority_outcomes) < priority_capacity:
        priority_rows = current_public_priority_requests(operations, limit=1)
        if not priority_rows:
            break
        interpretation_id = str(priority_rows[0].get("interpretation_id") or "")
        if not interpretation_id or interpretation_id in seen_priority:
            break
        seen_priority.add(interpretation_id)
        prepared, rejected = prepare_persisted_pending_requests(
            ledger,
            operations,
            priority_rows,
            limit=1,
            public_priority=True,
        )
        priority_stale_rejected += rejected
        if not prepared:
            continue
        priority_work.extend(prepared)
        priority_outcomes.extend(
            process_pending_items(prepared, args.env_file, workers=1)
        )

    observed_at = datetime.now(timezone.utc)
    last_scan = None
    if isinstance(prior_inventory, dict):
        try:
            last_scan = datetime.fromisoformat(
                str(prior_inventory.get("last_inventory_scan_at") or "").replace(
                    "Z", "+00:00"
                )
            )
            if last_scan.tzinfo is None:
                last_scan = last_scan.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            last_scan = None
    inventory_scan_due = (
        last_scan is None
        or (observed_at - last_scan.astimezone(timezone.utc)).total_seconds()
        >= INVENTORY_MIN_INTERVAL_SECONDS
    )

    background_capacity = max(
        0,
        int(args.limit) - len(priority_outcomes) - (1 if int(args.limit) > 1 else 0),
    )
    persisted_rows = (
        current_persisted_pending_requests(
            operations,
            limit=max(args.limit, min(args.limit * 8, PERSISTED_PENDING_SCAN_LIMIT)),
        )
        if inventory_scan_due and background_capacity > 0
        else []
    )
    persisted_work, stale_rejected = prepare_persisted_pending_requests(
        ledger,
        operations,
        persisted_rows,
        limit=max(1, background_capacity),
    )
    persisted_outcomes: list[str] = []
    background_index = 0
    execution_budget = background_capacity
    wave_size = max(1, int(args.workers))
    while background_index < len(persisted_work) and execution_budget > 0:
        wave = persisted_work[
            background_index : background_index + min(wave_size, execution_budget)
        ]
        persisted_outcomes.extend(
            process_pending_items(wave, args.env_file, workers=args.workers)
        )
        background_index += len(wave)
        execution_budget -= len(wave)

        # A request can arrive while one background wave is waiting on the
        # provider. Recheck durable public priority before submitting any more
        # old work, so the next free wave is not monopolized by backlog.
        if execution_budget <= 0:
            break
        priority_rows = current_public_priority_requests(operations, limit=1)
        if not priority_rows:
            continue
        interpretation_id = str(priority_rows[0].get("interpretation_id") or "")
        if not interpretation_id or interpretation_id in seen_priority:
            continue
        seen_priority.add(interpretation_id)
        prepared, rejected = prepare_persisted_pending_requests(
            ledger,
            operations,
            priority_rows,
            limit=1,
            public_priority=True,
        )
        priority_stale_rejected += rejected
        if not prepared:
            continue
        priority_work.extend(prepared)
        priority_outcomes.extend(
            process_pending_items(prepared, args.env_file, workers=1)
        )
        execution_budget -= 1
    all_persisted_outcomes = priority_outcomes + persisted_outcomes
    completed = sum(outcome == "COMPLETED" for outcome in all_persisted_outcomes)
    skipped_terminal = sum(outcome == "CACHED" for outcome in all_persisted_outcomes)
    deferred = sum(
        outcome in {"DEFERRED", "BUDGET_DEFERRED"}
        for outcome in all_persisted_outcomes
    )
    failed = sum(
        outcome not in {"COMPLETED", "CACHED", "DEFERRED", "BUDGET_DEFERRED"}
        for outcome in all_persisted_outcomes
    )
    examined = len(all_persisted_outcomes)
    remaining_limit = max(0, int(args.limit) - len(all_persisted_outcomes))
    persisted_keys = {
        (
            str(item.get("event_id") or ""),
            str(item.get("capture_receipt_sha256") or ""),
        )
        for item in persisted_work
    }
    active_keys = operations.capture_interpretation_active_keys(
        provider="deepseek",
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
    )
    inventory_reserved_keys = persisted_keys | active_keys

    candidate_count = ledger.capture_interpretation_candidate_count()
    window_limit = max(args.limit, min(args.scan_limit, 1_000))
    recent_limit = max(1, min(window_limit // 3, max(args.limit, 50)))
    fair_limit = max(1, window_limit - recent_limit)

    after: tuple[int, str, str] | None = None
    if isinstance(prior_inventory, dict):
        cursor = prior_inventory.get("fair_cursor")
        if isinstance(cursor, dict):
            try:
                after = (
                    int(cursor.get("bucket_priority") or 0),
                    str(cursor.get("event_id") or ""),
                    str(cursor.get("observation_id") or ""),
                )
            except (TypeError, ValueError):
                after = None

    recent = (
        ledger.capture_interpretation_candidates(limit=recent_limit, order="recent")
        if inventory_scan_due
        else []
    )
    fair = (
        ledger.capture_interpretation_candidates(
            limit=fair_limit,
            order="fair",
            after=after,
        )
        if inventory_scan_due
        else []
    )
    wrapped = False
    if inventory_scan_due and not fair and after is not None:
        wrapped = True
        after = None
        fair = ledger.capture_interpretation_candidates(
            limit=fair_limit,
            order="fair",
        )

    inventory: list[dict[str, Any]] = []
    seen_inventory: set[tuple[str, str, int]] = set()
    for index in range(max(len(recent), len(fair))):
        for lane, values in (("recent", recent), ("fair", fair)):
            if index >= len(values):
                continue
            item = dict(values[index])
            key = (
                str(item.get("event_id") or ""),
                str(item.get("capture_receipt_sha256") or ""),
                int(item.get("event_version") or 0),
            )
            if key in seen_inventory:
                continue
            seen_inventory.add(key)
            item["scheduler_lane"] = lane
            inventory.append(item)
    terminal_before = operations.capture_interpretation_terminal_keys(
        provider="deepseek",
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
    )

    def is_terminal(
        item: dict[str, Any],
        terminal: dict[tuple[str, str, int], str],
    ) -> bool:
        event_id = str(item["event_id"])
        receipt = str(item["capture_receipt_sha256"])
        version = int(item.get("event_version") or 0)
        return (event_id, receipt, version) in terminal or (
            event_id,
            receipt,
            -1,
        ) in terminal or (event_id, receipt) in terminal

    nonterminal_inventory = [
        item
        for item in inventory
        if not is_terminal(item, terminal_before)
    ]
    pending = [
        item
        for item in nonterminal_inventory
        if (
            str(item.get("event_id") or ""),
            str(item.get("capture_receipt_sha256") or ""),
        )
        not in inventory_reserved_keys
    ]
    skipped_terminal += len(inventory) - len(nonterminal_inventory)

    work_items = pending[:remaining_limit]
    outcomes = process_pending_items(
        work_items,
        args.env_file,
        workers=args.workers,
    )
    examined += len(outcomes)
    for outcome in outcomes:
        if outcome == "COMPLETED":
            completed += 1
        elif outcome == "CACHED":
            skipped_terminal += 1
        elif outcome in {"DEFERRED", "BUDGET_DEFERRED"}:
            deferred += 1
        else:
            failed += 1

    terminal_after = operations.capture_interpretation_terminal_keys(
        provider="deepseek",
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
    )
    fair_advance = 0
    fair_cursor = after
    for item in fair:
        if not is_terminal(item, terminal_after):
            break
        fair_advance += 1
        fair_cursor = (
            int(item.get("bucket_priority") or 0),
            str(item.get("event_id") or ""),
            str(item.get("observation_id") or ""),
        )
    reached_end = (
        inventory_scan_due
        and len(fair) < fair_limit
        and fair_advance == len(fair)
    )
    if reached_end:
        fair_cursor = None

    health = operations.capture_interpretation_queue_health(
        "deepseek",
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
    )
    status = "COMPLETED" if failed == 0 else "PARTIAL"
    runtime = {
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "examined": examined,
        "persisted_pending_loaded": len(persisted_rows),
        "persisted_pending_examined": len(persisted_outcomes),
        "persisted_pending_stale_rejected": stale_rejected + priority_stale_rejected,
        "public_priority_examined": len(priority_outcomes),
        "inventory_scan_due": inventory_scan_due,
        "last_inventory_scan_at": (
            observed_at.isoformat()
            if inventory_scan_due
            else (prior_inventory.get("last_inventory_scan_at") if isinstance(prior_inventory, dict) else None)
        ),
        "candidates": candidate_count,
        "window_candidates": len(inventory),
        "recent_loaded": len(recent),
        "fair_loaded": len(fair),
        "fair_advanced": fair_advance,
        "fair_wrapped": wrapped,
        "fair_reached_end": reached_end,
        "completed": completed,
        "skipped_terminal": skipped_terminal,
        "deferred": deferred,
        "failed": failed,
        "queue": health.get("by_status") or {},
        "daily": health.get("daily") or {},
        "source_generation": generation,
        "inventory_loader": "persisted_pending_then_recent_plus_durable_keyset_v4",
        "canonical_state_unchanged": True,
        "no_trading": True,
    }
    inventory_state = {
        **runtime,
        "fair_cursor": (
            {
                "bucket_priority": fair_cursor[0],
                "event_id": fair_cursor[1],
                "observation_id": fair_cursor[2],
            }
            if fair_cursor is not None
            else None
        ),
        "contract_version": CAPTURE_INTERPRETATION_CONTRACT,
        "prompt_version": CAPTURE_INTERPRETATION_PROMPT_VERSION,
        "prompt_sha256": CAPTURE_INTERPRETATION_PROMPT_SHA256,
        "provider": "deepseek",
        "model_snapshot": DEEPSEEK_CHEAP_TEXT_MODEL,
    }
    operations.set_state(INVENTORY_STATE_KEY, inventory_state)
    operations.set_state(RUNTIME_STATE_KEY, runtime)
    _safe_json(**runtime)
    return 0 if failed == 0 else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--limit", type=int, default=3)
    result.add_argument("--scan-limit", type=int, default=100_000)
    result.add_argument("--workers", type=int, default=1)
    result.add_argument("--env-file", type=Path, default=ROOT / ".env.local")
    return result


def main() -> int:
    args = parser().parse_args()
    args.limit = max(1, min(int(args.limit), 20))
    args.scan_limit = max(args.limit, min(int(args.scan_limit), 1_000))
    args.workers = max(1, min(int(args.workers), 4, args.limit))
    try:
        return run(args)
    except Exception as exc:
        _safe_json(status="FAILED", error_class=type(exc).__name__, no_trading=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
