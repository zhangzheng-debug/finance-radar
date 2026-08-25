#!/usr/bin/env python3
"""Run one bounded Qwen semantic-risk backfill/refresh cycle."""

from __future__ import annotations

import argparse
import json

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
    result = run_qwen_risk_batch(
        LedgerRepository(settings.ledger_db),
        OperationsRepository(settings.operations_db),
        provider,
        scan_limit=args.scan_limit,
        run_limit=args.limit,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if not result["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
