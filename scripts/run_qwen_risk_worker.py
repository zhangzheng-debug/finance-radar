#!/usr/bin/env python3
"""Run one bounded Qwen semantic-risk backfill/refresh cycle."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.services import QwenRiskModelProvider, run_qwen_risk_batch
from app.storage import LedgerRepository, OperationsRepository


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--scan-limit", type=int, default=64)
    parser.add_argument("--concurrency", type=int, default=1)
    args = parser.parse_args()
    settings = Settings.from_env()
    if not settings.qwen_risk_enabled:
        print(json.dumps({"status": "DISABLED", "no_mutation": True}))
        return 0
    provider = QwenRiskModelProvider(
        settings.qwen_risk_url,
        settings.qwen_risk_model,
        settings.qwen_risk_adapter_sha256,
        timeout_seconds=settings.qwen_risk_timeout_seconds,
        max_tokens=settings.qwen_risk_max_tokens,
    )
    operations = OperationsRepository(settings.operations_db)
    started_at = datetime.now(timezone.utc).isoformat()
    runtime_base = {
        "started_at": started_at,
        "model_version": provider.model_version,
        "concurrency": max(1, min(int(args.concurrency), 4)),
        "shadow": True,
        "no_trading": True,
    }
    operations.set_state(
        "qwen_risk_worker_runtime_v1",
        {
            **runtime_base,
            "status": "RUNNING",
            "updated_at": started_at,
            "heartbeat_at": started_at,
        },
    )
    try:
        result = run_qwen_risk_batch(
            LedgerRepository(settings.ledger_db),
            operations,
            provider,
            scan_limit=args.scan_limit,
            run_limit=args.limit,
            concurrency=args.concurrency,
        )
    except Exception as exc:
        failed_at = datetime.now(timezone.utc).isoformat()
        operations.set_state(
            "qwen_risk_worker_runtime_v1",
            {
                **runtime_base,
                "status": "FAILED",
                "updated_at": failed_at,
                "finished_at": failed_at,
                "error_code": type(exc).__name__[:120],
            },
        )
        raise
    finished_at = datetime.now(timezone.utc).isoformat()
    operations.set_state(
        "qwen_risk_worker_runtime_v1",
        {
            **runtime_base,
            "status": "COMPLETED" if not result["errors"] else "PARTIAL",
            "updated_at": finished_at,
            "finished_at": finished_at,
            "attempted": result.get("attempted", 0),
            "recorded": result.get("recorded", 0),
            "input_insufficient": result.get("input_insufficient", 0),
            "priority_claimed": result.get("priority_claimed", 0),
            "retry_deferred": result.get("retry_deferred", 0),
            "errors": len(result.get("errors") or []),
        },
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    # A bounded batch can still make durable progress when individual model
    # requests time out or fail contract validation.  Keep the timer healthy
    # after partial success, while failing closed if the whole attempted batch
    # produced no usable result.
    return 1 if result["errors"] and not result.get("recorded") else 0


if __name__ == "__main__":
    raise SystemExit(main())
