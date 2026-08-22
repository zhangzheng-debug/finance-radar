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
from app.services.source_observation_recovery import (  # noqa: E402
    build_source_observation_recovery_plan,
)
from app.storage import LedgerRepository, OperationsRepository  # noqa: E402
from scripts.run_capture_interpretation_deepseek import (  # noqa: E402
    RUN_CACHED,
    RUN_COMPLETED,
    load_local_env,
    run as run_single,
)


PRIORITY = {"NO_URL_RAW_ONLY": 0, "P2_CAPTURE_ONLY": 1}
INVENTORY_STATE_KEY = "capture_interpretation_inventory_v1"


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


def run(args: argparse.Namespace) -> int:
    load_local_env(args.env_file.resolve())
    settings = Settings.from_env()
    ledger = LedgerRepository(settings.ledger_db)
    operations = OperationsRepository(settings.operations_db)
    generation = ledger.capture_source_generation()
    prior_inventory = operations.get_state(INVENTORY_STATE_KEY, {})
    health = operations.capture_interpretation_queue_health(
        "deepseek",
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
    )
    if (
        isinstance(prior_inventory, dict)
        and prior_inventory.get("backlog_complete") is True
        and prior_inventory.get("source_generation") == generation
    ):
        _safe_json(
            status="IDLE",
            reason="SOURCE_GENERATION_UNCHANGED",
            examined=0,
            completed=0,
            remaining=0,
            source_generation=generation,
            queue=health.get("by_status") or {},
            daily=health.get("daily") or {},
            only_new_or_changed=True,
            canonical_state_unchanged=True,
            no_trading=True,
        )
        return 0

    plan = build_source_observation_recovery_plan(Path(settings.ledger_db))
    inventory = candidates(plan)
    terminal_before = operations.capture_interpretation_terminal_keys(
        provider="deepseek",
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
    )
    pending = [
        item
        for item in inventory
        if (item["event_id"], item["capture_receipt_sha256"]) not in terminal_before
    ]
    completed = 0
    skipped_terminal = len(inventory) - len(pending)
    deferred = 0
    failed = 0
    examined = 0

    for item in pending[: args.scan_limit]:
        if examined >= args.limit:
            break
        examined += 1
        try:
            code = run_single(
                SimpleNamespace(
                    event_id=item["event_id"],
                    observation_id=item["observation_id"],
                    env_file=args.env_file,
                )
            )
            outcome = classify_run_code(code)
            if outcome == "COMPLETED":
                completed += 1
            elif outcome == "CACHED":
                skipped_terminal += 1
            else:
                failed += 1
        except RuntimeError as exc:
            code = str(exc)
            if "DAILY_REQUEST_CAP_REACHED" in code or "DAILY_CNY_CAP_REACHED" in code:
                deferred += 1
                break
            if "NO_ELIGIBLE_JOB" in code:
                deferred += 1
                continue
            failed += 1
        except Exception:
            # The single-job runner already stores a redacted failure class and
            # usage accounting.  Do not echo source text or provider bodies.
            failed += 1

    terminal_after = operations.capture_interpretation_terminal_keys(
        provider="deepseek",
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
    )
    remaining = sum(
        (item["event_id"], item["capture_receipt_sha256"]) not in terminal_after
        for item in inventory
    )
    operations.set_state(
        INVENTORY_STATE_KEY,
        {
            "source_generation": generation,
            "candidate_count": len(inventory),
            "remaining": remaining,
            "backlog_complete": remaining == 0,
            "contract_version": CAPTURE_INTERPRETATION_CONTRACT,
            "prompt_version": CAPTURE_INTERPRETATION_PROMPT_VERSION,
            "prompt_sha256": CAPTURE_INTERPRETATION_PROMPT_SHA256,
            "provider": "deepseek",
            "model_snapshot": DEEPSEEK_CHEAP_TEXT_MODEL,
            "only_new_or_changed": True,
        },
    )
    health = operations.capture_interpretation_queue_health(
        "deepseek",
        contract_version=CAPTURE_INTERPRETATION_CONTRACT,
        prompt_version=CAPTURE_INTERPRETATION_PROMPT_VERSION,
        prompt_sha256=CAPTURE_INTERPRETATION_PROMPT_SHA256,
        model_snapshot=DEEPSEEK_CHEAP_TEXT_MODEL,
    )
    _safe_json(
        status="COMPLETED" if failed == 0 else "PARTIAL",
        examined=examined,
        candidates=len(inventory),
        completed=completed,
        skipped_terminal=skipped_terminal,
        remaining=remaining,
        backlog_complete=remaining == 0,
        deferred=deferred,
        failed=failed,
        queue=health.get("by_status") or {},
        daily=health.get("daily") or {},
        source_generation=generation,
        only_new_or_changed=True,
        canonical_state_unchanged=True,
        no_trading=True,
    )
    return 0 if failed == 0 else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--limit", type=int, default=3)
    result.add_argument("--scan-limit", type=int, default=100_000)
    result.add_argument("--env-file", type=Path, default=ROOT / ".env.local")
    return result


def main() -> int:
    args = parser().parse_args()
    args.limit = max(1, min(int(args.limit), 20))
    args.scan_limit = max(args.limit, min(int(args.scan_limit), 250_000))
    try:
        return run(args)
    except Exception as exc:
        _safe_json(status="FAILED", error_class=type(exc).__name__, no_trading=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
