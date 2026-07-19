#!/usr/bin/env python3
"""Audit whether a frozen risk-router set contains the evidence text it claims to test."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = ROOT / "artifacts" / "risk_router_external_blind_v1.jsonl"
DEFAULT_JSON = ROOT / "reports" / "risk_router_input_contract_audit_v1.json"
DEFAULT_MARKDOWN = ROOT / "reports" / "risk_router_input_contract_audit_v1.md"
CONTENT_MODEL_MIN_RISK_BODY_COVERAGE = 0.80
ADVERSE_DISCOVERY_TERMS = (
    "alleged",
    "charges",
    "civil action",
    "complaint",
    "court order",
    "default",
    "delisting",
    "fraud",
    "investigation",
    "judgment",
    "penalty",
    "recall",
    "settlement",
    "suspension",
    "violated",
)


def normalize(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def residual_body(title: str, text: str) -> str:
    """Return normalized text left after repeated copies of the feed title."""
    normalized_title = normalize(title)
    normalized_text = normalize(text)
    if not normalized_title:
        return normalized_text
    previous = None
    while normalized_text != previous:
        previous = normalized_text
        normalized_text = normalized_text.replace(normalized_title, " ")
        normalized_text = " ".join(normalized_text.split())
    return normalized_text


def audit_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    label_counts: Counter[str] = Counter()
    source_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        title = str(row.get("title") or "")
        text = str(row.get("text") or "")
        label = str(row.get("expected_label") or "UNKNOWN")
        source_id = str(row.get("source_id") or "unknown")
        body = residual_body(title, text)
        normalized_title = normalize(title)
        adverse_terms = sorted(term for term in ADVERSE_DISCOVERY_TERMS if term in normalized_title)
        title_only = len(body) < 20
        record = {
            "sample_id": row.get("sample_id"),
            "source_id": source_id,
            "expected_label": label,
            "title": title,
            "title_only": title_only,
            "residual_body_characters": len(body),
            "adverse_title_terms": adverse_terms,
            "content_ambiguous_at_discovery": bool(
                label == "RISK_REVIEW" and title_only and not adverse_terms
            ),
        }
        items.append(record)
        label_counts[label] += 1
        source_counts[source_id]["rows"] += 1
        source_counts[source_id]["title_only"] += int(title_only)

    risk = [item for item in items if item["expected_label"] == "RISK_REVIEW"]
    risk_with_body = sum(not item["title_only"] for item in risk)
    risk_body_coverage = risk_with_body / len(risk) if risk else 0.0
    ambiguous = [item for item in risk if item["content_ambiguous_at_discovery"]]
    gates = {
        "minimum_rows": len(items) >= 40,
        "risk_rows_present": bool(risk),
        "risk_body_coverage": risk_body_coverage >= CONTENT_MODEL_MIN_RISK_BODY_COVERAGE,
    }
    return {
        "schema_version": 1,
        "audit_type": "risk_router_input_contract",
        "rows": len(items),
        "label_counts": dict(label_counts),
        "risk_body_coverage": risk_body_coverage,
        "risk_title_only_rows": len(risk) - risk_with_body,
        "risk_content_ambiguous_rows": len(ambiguous),
        "source_metrics": {
            source_id: dict(counts) for source_id, counts in sorted(source_counts.items())
        },
        "thresholds": {
            "minimum_rows": 40,
            "risk_body_coverage_gte": CONTENT_MODEL_MIN_RISK_BODY_COVERAGE,
        },
        "gates": gates,
        "benchmark_contract_valid": all(gates.values()),
        "required_remediation": (
            "Freeze exact official-page evidence passages before evaluating the content model; "
            "keep P0 enforcement-source discovery routing outside learned text features."
        ),
        "ambiguous_risk_samples": ambiguous,
        "samples": items,
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Risk Router input-contract audit v1",
        "",
        f"- Rows: `{report['rows']}`",
        f"- Risk rows with real body text: `{report['risk_body_coverage']:.1%}`",
        f"- Risk rows that are title-only: `{report['risk_title_only_rows']}`",
        f"- Content-ambiguous risk rows at discovery: `{report['risk_content_ambiguous_rows']}`",
        f"- Benchmark content contract: `{'PASS' if report['benchmark_contract_valid'] else 'FAIL'}`",
        "",
        "This audit does not train, tune or run inference. It checks whether the frozen bytes contain the evidence-stage text declared by the model contract.",
        "",
        "## Gate details",
        "",
        *[
            f"- {'PASS' if passed else 'FAIL'} — `{name}`"
            for name, passed in report["gates"].items()
        ],
        "",
        "## Required remediation",
        "",
        f"- {report['required_remediation']}",
        "- The existing v1 failure remains valid evidence that the deployed router must stay shadow. It is not a promotion test for v2.",
        "",
        "## Ambiguous risk samples",
        "",
    ]
    for item in report["ambiguous_risk_samples"]:
        lines.append(
            f"- `{item['sample_id']}` · `{item['source_id']}` · {item['title']}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()
    report = audit_rows(load_jsonl(args.dataset))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    write_markdown(report, args.markdown)
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "rows",
                    "risk_body_coverage",
                    "risk_title_only_rows",
                    "risk_content_ambiguous_rows",
                    "gates",
                    "benchmark_contract_valid",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
