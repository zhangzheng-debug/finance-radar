#!/usr/bin/env python3
"""Bounded scheduler for advisory interpretations of retained API captures.

This worker only considers zero-evidence P2/raw-only records.  It never changes
the canonical ledger, never treats an interpretation as evidence, and delegates
every external call to the receipt-bound, atomically budgeted single-job runner.
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
)
from app.services.deepseek_capture_interpretation import (  # noqa: E402
    DEEPSEEK_CHEAP_TEXT_MODEL,
)
from app.storage import LedgerRepository, OperationsRepository  # noqa: E402
from scripts.run_capture_interpretation_deepseek import (  # noqa: E402
    RUN_CACHED,
    RUN_COMPLETED,
    load_local_env,
    run as run_single,
)


PRIORITY = {"NO_URL_RAW_ONLY": 0, "P2_CAPTURE_ONLY": 1}
INVENTORY_STATE_KEY = "capture_interpretation_inventory_v3"
RUNTIME_STATE_KEY = "capture_interpretation_runtime_v1"


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


def run(args: argparse.Namespace) -> int:
    load_local_env(args.env_file.resolve())
    settings = Settings.from_env()
    ledger = LedgerRepository(settings.ledger_db)
    operations = OperationsRepository(settings.operations_db)
    generation = ledger.capture_source_generation()
    prior_inventory = operations.get_state(INVENTORY_STATE_KEY, {})
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

    recent = ledger.capture_interpretation_candidates(
        limit=recent_limit,
        order="recent",
    )
    fair = ledger.capture_interpretation_candidates(
        limit=fair_limit,
        order="fair",
        after=after,
    )
    wrapped = False
    if not fair and after is not None:
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

    pending = [
        item
        for item in inventory
        if not is_terminal(item, terminal_before)
    ]
    completed = 0
    skipped_terminal = len(inventory) - len(pending)
    deferred = 0
    failed = 0
    examined = 0

    work_items = pending[: args.limit]
    outcomes = process_pending_items(
        work_items,
        args.env_file,
        workers=args.workers,
    )
    examined = len(outcomes)
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
    reached_end = len(fair) < fair_limit and fair_advance == len(fair)
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
        "inventory_loader": "recent_plus_durable_keyset_v3",
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
