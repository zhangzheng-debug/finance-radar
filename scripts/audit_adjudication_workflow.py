#!/usr/bin/env python3
"""Write an honest readiness report for the v3 human adjudication workflow."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.services import AdjudicationService
from app.storage import LedgerRepository, OperationsRepository


def render_markdown(report: dict) -> str:
    counts = report.get("status_counts") or {}
    labels = report.get("label_counts") or {}
    deficits = report.get("label_deficits") or {}
    return "\n".join(
        [
            "# Risk label v3 adjudication workflow",
            "",
            f"- Generated: `{report['generated_at']}`",
            f"- Status: **{report['status']}**",
            f"- Samples: **{report['samples']}**",
            f"- Valid dual-review annotations: **{report['valid_annotations']}**",
            f"- Workflow states: `{json.dumps(counts, ensure_ascii=False, sort_keys=True)}`",
            f"- Derived labels: `{json.dumps(labels, ensure_ascii=False, sort_keys=True)}`",
            f"- Remaining label minimums: `{json.dumps(deficits, ensure_ascii=False, sort_keys=True)}`",
            f"- Independent source families: **{report.get('source_families', 0)} / {report.get('minimum_source_families', report['minimum_source_groups'])}**",
            f"- Raw source IDs represented: **{report['source_groups']}**",
            "",
            "Reviewer submissions contain only materiality, polarity and evidence-state axes. "
            "The target label is derived after two hidden independent reviews; conflicts require a third arbiter.",
            "",
            "No split has been assigned, no blind-v2 set has been frozen, and the production model is unchanged. "
            "Real human reviews are still required; this report does not fabricate them.",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", type=Path, default=ROOT / "reports" / "adjudication_v3_latest.json"
    )
    parser.add_argument(
        "--markdown", type=Path, default=ROOT / "reports" / "adjudication_v3_latest.md"
    )
    args = parser.parse_args()
    settings = Settings.from_env()
    service = AdjudicationService(
        LedgerRepository(settings.ledger_db), OperationsRepository(settings.operations_db)
    )
    report = service.pre_freeze_report()
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "annotations"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
