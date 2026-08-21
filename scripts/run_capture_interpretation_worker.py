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
from app.services.source_observation_recovery import (  # noqa: E402
    build_source_observation_recovery_plan,
)
from app.storage import OperationsRepository  # noqa: E402
from scripts.run_capture_interpretation_deepseek import (  # noqa: E402
    load_local_env,
    run as run_single,
)


PRIORITY = {"NO_URL_RAW_ONLY": 0, "P2_CAPTURE_ONLY": 1}


def _safe_json(**values: Any) -> None:
    print(json.dumps(values, ensure_ascii=False, sort_keys=True))


def candidates(plan: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
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
            if observation_id and receipt:
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
    plan = build_source_observation_recovery_plan(Path(settings.ledger_db))
    operations = OperationsRepository(settings.operations_db)
    completed = 0
    skipped_terminal = 0
    deferred = 0
    failed = 0
    examined = 0

    for item in candidates(plan):
        if completed >= args.limit or examined >= args.scan_limit:
            break
        examined += 1
        latest = operations.latest_capture_interpretation(
            item["event_id"], item["capture_receipt_sha256"]
        )
        if latest and latest.get("status") in {"COMPLETED", "FAILED"}:
            skipped_terminal += 1
            continue
        try:
            code = run_single(
                SimpleNamespace(
                    event_id=item["event_id"],
                    observation_id=item["observation_id"],
                    env_file=args.env_file,
                )
            )
            completed += int(code == 0)
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

    health = operations.capture_interpretation_queue_health("deepseek")
    _safe_json(
        status="COMPLETED" if failed == 0 else "PARTIAL",
        examined=examined,
        completed=completed,
        skipped_terminal=skipped_terminal,
        deferred=deferred,
        failed=failed,
        queue=health.get("by_status") or {},
        daily=health.get("daily") or {},
        canonical_state_unchanged=True,
        no_trading=True,
    )
    return 0 if failed == 0 else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--limit", type=int, default=3)
    result.add_argument("--scan-limit", type=int, default=500)
    result.add_argument("--env-file", type=Path, default=ROOT / ".env.local")
    return result


def main() -> int:
    args = parser().parse_args()
    args.limit = max(1, min(int(args.limit), 20))
    args.scan_limit = max(args.limit, min(int(args.scan_limit), 10_000))
    try:
        return run(args)
    except Exception as exc:
        _safe_json(status="FAILED", error_class=type(exc).__name__, no_trading=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
