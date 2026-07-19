#!/usr/bin/env python3
"""Audit a JSONL annotation manifest against the pre-freeze v3 label contract."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.risk_label_contract import validate_annotation  # noqa: E402


DEFAULT_INPUT = ROOT / "artifacts" / "risk_router_v2_candidate_manifest.jsonl"
DEFAULT_JSON = ROOT / "reports" / "risk_label_contract_v3_readiness.json"
DEFAULT_MARKDOWN = ROOT / "reports" / "risk_label_contract_v3_readiness.md"
MINIMUMS = {"RISK_REVIEW": 30, "NON_TARGET": 30, "ABSTAIN": 20}


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"line {line_number} is not an object")
        rows.append(payload)
    return rows


def audit_rows(rows: list[dict[str, Any]], *, source: str) -> dict[str, Any]:
    issue_counts: Counter[str] = Counter()
    label_counts: Counter[str] = Counter()
    source_groups: set[str] = set()
    examples: list[dict[str, Any]] = []
    valid_rows = 0
    for index, row in enumerate(rows, 1):
        issues = validate_annotation(row)
        if row.get("split") not in (None, "UNASSIGNED"):
            issues.append("legacy:preassigned_split")
        label_basis = str(row.get("label_basis") or "").casefold()
        if any(marker in label_basis for marker in ("source", "historical", "publisher", "corpus")):
            issues.append("legacy:source_or_corpus_label_basis")
        if issues:
            issue_counts.update(issues)
            if len(examples) < 12:
                examples.append(
                    {
                        "row": index,
                        "sample_id": row.get("sample_id") or row.get("event_id"),
                        "issues": issues,
                    }
                )
            continue
        valid_rows += 1
        label_counts[str(row["label"])] += 1
        source_groups.add(str(row["source_id"]))

    distribution_gates = {
        label: label_counts[label] >= minimum for label, minimum in MINIMUMS.items()
    }
    distribution_gates["source_groups"] = len(source_groups) >= 4
    ready = bool(rows) and valid_rows == len(rows) and all(distribution_gates.values())
    return {
        "schema_version": 1,
        "source": source,
        "status": "READY_FOR_BLIND_V2_FREEZE" if ready else "NOT_READY_FOR_BLIND_V2",
        "production_changed": False,
        "rows": len(rows),
        "valid_rows": valid_rows,
        "invalid_rows": len(rows) - valid_rows,
        "label_counts_valid_rows": dict(sorted(label_counts.items())),
        "source_groups_valid_rows": len(source_groups),
        "distribution_gates": distribution_gates,
        "issue_counts": dict(issue_counts.most_common()),
        "issue_examples": examples,
        "next_action": (
            "freeze blind-v2 with grouped zero-overlap split"
            if ready
            else "human content adjudication under config/risk_label_contract_v3.json"
        ),
        "no_blind_v2_claim": not ready,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Risk label contract v3 readiness",
        "",
        f"- Status: **{report['status']}**",
        f"- Input: `{report['source']}`",
        f"- Rows: {report['rows']:,}",
        f"- Contract-valid rows: {report['valid_rows']:,}",
        f"- Production changed: `{report['production_changed']}`",
        f"- No blind-v2 claim: `{report['no_blind_v2_claim']}`",
        "",
        "## Why the current candidate manifest cannot become blind-v2",
        "",
    ]
    lines.extend(f"- `{name}`: {count:,}" for name, count in report["issue_counts"].items())
    lines.extend(
        [
            "",
            "The v3 contract requires content-present dual adjudication across materiality, polarity and evidence state. P0/P1/P2 determine only the deterministic evidence lane; source identity is forbidden as a target label. Splits stay `UNASSIGNED` until validation succeeds, after which entity/event-chain/source groups are frozen without overlap.",
            "",
            "Current action: **do not train or freeze blind-v2**. Obtain authentic human content labels first; then rerun this audit.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()
    report = audit_rows(load_rows(args.input), source=str(args.input))
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
