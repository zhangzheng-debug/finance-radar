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
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--scan-limit", type=int, default=100)
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
    result = run_qwen_risk_batch(
        LedgerRepository(settings.ledger_db),
        operations,
        provider,
        scan_limit=args.scan_limit,
        run_limit=args.limit,
    )
    operations.set_state(
        "qwen_risk_worker_runtime_v1",
        {
            "status": "COMPLETED" if not result["errors"] else "PARTIAL",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "model_version": provider.model_version,
            "attempted": result.get("attempted", 0),
            "recorded": result.get("recorded", 0),
            "input_insufficient": result.get("input_insufficient", 0),
            "errors": len(result.get("errors") or []),
            "shadow": True,
            "no_trading": True,
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
