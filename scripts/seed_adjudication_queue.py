#!/usr/bin/env python3
"""Seed a diverse, unlabeled v3 adjudication queue from persisted real events."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.services import AdjudicationService
from app.storage import LedgerRepository, OperationsRepository


def diverse_candidates(ledger: LedgerRepository, statuses: list[str], limit: int) -> list[str]:
    pool: list[dict] = []
    for status in statuses:
        pool.extend(ledger.list_events(status=status, limit=200)["items"])
    selected: list[str] = []
    family_counts: Counter[str] = Counter()
    remaining = list(pool)
    while remaining and len(selected) < limit:
        remaining.sort(
            key=lambda row: (
                family_counts[str(row.get("event_family") or "unknown")],
                -int(row.get("evidence_count") or 0),
                str(row.get("event_date") or ""),
                str(row.get("event_id") or ""),
            )
        )
        item = remaining.pop(0)
        family = str(item.get("event_family") or "unknown")
        selected.append(item["event_id"])
        family_counts[family] += 1
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--statuses", default="verified,candidate")
    args = parser.parse_args()
    settings = Settings.from_env()
    ledger = LedgerRepository(settings.ledger_db)
    operations = OperationsRepository(settings.operations_db)
    service = AdjudicationService(ledger, operations)
    statuses = [item.strip() for item in args.statuses.split(",") if item.strip()]
    created: list[dict] = []
    rejected: list[dict] = []
    for event_id in diverse_candidates(ledger, statuses, max(1, min(args.limit, 200))):
        try:
            result = service.create_sample_from_event(event_id)
            if result["created"]:
                created.append(result)
        except (KeyError, ValueError) as exc:
            rejected.append({"event_id": event_id, "reason": str(exc)})
    report = {
        "status": "SEEDED" if created else "NO_NEW_SAMPLES",
        "requested": args.limit,
        "statuses": statuses,
        "created": len(created),
        "rejected": rejected,
        "queue": service.pre_freeze_report(),
        "target_labels_assigned": False,
        "source_used_as_label": False,
        "market_outcomes_used": False,
    }
    report["queue"].pop("annotations", None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
