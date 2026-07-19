#!/usr/bin/env python3
"""Evaluate the frozen external blind set without changing its labels or rows."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.risk_router import RiskRouter  # noqa: E402


DEFAULT_DATASET = ROOT / "artifacts" / "risk_router_external_blind_v1.jsonl"
DEFAULT_FREEZE = ROOT / "artifacts" / "risk_router_external_blind_v1_freeze.json"
DEFAULT_ARTIFACT = ROOT / "artifacts" / "risk_router.joblib"
DEFAULT_MODEL_CARD = ROOT / "artifacts" / "risk_router_model_card.json"
DEFAULT_OUTPUT = ROOT / "artifacts" / "risk_router_external_blind_v1_report.json"
DEFAULT_MARKDOWN = ROOT / "artifacts" / "risk_router_external_blind_v1_report.md"


def load_and_verify(dataset_path: Path, freeze_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    dataset_bytes = dataset_path.read_bytes()
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    actual_sha = hashlib.sha256(dataset_bytes).hexdigest()
    if actual_sha != freeze["dataset_sha256"]:
        raise RuntimeError("external blind dataset hash differs from its freeze record")
    rows = [json.loads(line) for line in dataset_bytes.decode("utf-8").splitlines() if line.strip()]
    if len(rows) != freeze["rows"]:
        raise RuntimeError("external blind row count differs from its freeze record")
    if not freeze.get("label_policy_locked_before_inference") or freeze.get("predictions_present"):
        raise RuntimeError("freeze does not prove label-first evaluation")
    if any(row.get("prediction") is not None for row in rows):
        raise RuntimeError("frozen dataset unexpectedly contains predictions")
    overlap = freeze.get("overlap_audit") or {}
    if overlap.get("event_or_sample_id_overlap_count") or overlap.get("title_substring_overlap_count"):
        raise RuntimeError("frozen external set is not disjoint from training data")
    return rows, freeze


def evaluate(rows: list[dict[str, Any]], freeze: dict[str, Any], router: RiskRouter) -> dict[str, Any]:
    predictions: list[dict[str, Any]] = []
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    source_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        result = router.predict(row["text"])
        prediction = {
            "sample_id": row["sample_id"],
            "source_id": row["source_id"],
            "title": row["title"],
            "canonical_url": row.get("canonical_url"),
            "expected_label": row["expected_label"],
            "predicted_label": result["label"],
            "confidence": result["confidence"],
            "runtime": result["runtime"],
            "latency_ms": result["latency_ms"],
            "correct": result["label"] == row["expected_label"],
        }
        predictions.append(prediction)
        confusion[row["expected_label"]][result["label"]] += 1
        source_rows[row["source_id"]].append(prediction)

    covered = [row for row in predictions if row["predicted_label"] != "ABSTAIN"]
    correct = [row for row in predictions if row["correct"]]
    covered_correct = [row for row in covered if row["correct"]]
    risk_rows = [row for row in predictions if row["expected_label"] == "RISK_REVIEW"]
    control_rows = [row for row in predictions if row["expected_label"] == "NON_TARGET"]
    risk_recalled = sum(row["predicted_label"] == "RISK_REVIEW" for row in risk_rows)
    control_false_risk = sum(row["predicted_label"] == "RISK_REVIEW" for row in control_rows)
    coverage = len(covered) / len(predictions)
    strict_accuracy = len(correct) / len(predictions)
    covered_accuracy = len(covered_correct) / len(covered) if covered else 0.0
    risk_recall = risk_recalled / len(risk_rows) if risk_rows else 0.0
    control_false_risk_rate = control_false_risk / len(control_rows) if control_rows else 0.0
    thresholds = {
        "minimum_rows": 40,
        "coverage_gte": 0.65,
        "covered_accuracy_gte": 0.80,
        "risk_recall_gte": 0.80,
        "non_target_false_risk_rate_lte": 0.20,
    }
    gates = {
        "minimum_rows": len(predictions) >= thresholds["minimum_rows"],
        "coverage": coverage >= thresholds["coverage_gte"],
        "covered_accuracy": covered_accuracy >= thresholds["covered_accuracy_gte"],
        "risk_recall": risk_recall >= thresholds["risk_recall_gte"],
        "non_target_false_risk_rate": control_false_risk_rate <= thresholds["non_target_false_risk_rate_lte"],
        "zero_training_overlap": True,
        "label_first_freeze": True,
    }
    source_metrics: dict[str, Any] = {}
    for source_id, items in source_rows.items():
        source_covered = [item for item in items if item["predicted_label"] != "ABSTAIN"]
        source_metrics[source_id] = {
            "rows": len(items),
            "expected_label": items[0]["expected_label"],
            "route_distribution": dict(Counter(item["predicted_label"] for item in items)),
            "coverage": len(source_covered) / len(items),
            "strict_accuracy": sum(item["correct"] for item in items) / len(items),
        }
    return {
        "schema_version": 1,
        "evaluation_type": "true_external_blind_label_first",
        "freeze_id": freeze["freeze_id"],
        "dataset_sha256": freeze["dataset_sha256"],
        "rows": len(predictions),
        "label_counts": freeze["label_counts"],
        "source_counts": freeze["source_counts"],
        "model_version": router.status()["model_version"],
        "model_artifact_sha256": router.status()["artifact_sha256"],
        "training_dataset_sha256": freeze["training_dataset_sha256"],
        "overlap_audit": freeze["overlap_audit"],
        "metrics": {
            "coverage": coverage,
            "abstain_rate": 1.0 - coverage,
            "strict_accuracy": strict_accuracy,
            "covered_accuracy": covered_accuracy,
            "risk_recall": risk_recall,
            "non_target_false_risk_rate": control_false_risk_rate,
            "mean_latency_ms": sum(item["latency_ms"] for item in predictions) / len(predictions),
            "confusion_expected_to_predicted": {
                expected: dict(counts) for expected, counts in confusion.items()
            },
        },
        "source_metrics": source_metrics,
        "thresholds": thresholds,
        "gates": gates,
        "gate_pass": all(gates.values()),
        "promotion_decision": "REMAIN_SHADOW",
        "failures": [item for item in predictions if not item["correct"]],
        "predictions": predictions,
        "no_trading": True,
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    metrics = report["metrics"]
    lines = [
        "# Risk Router external blind evaluation v1",
        "",
        f"- Freeze: `{report['freeze_id']}`",
        f"- Rows: {report['rows']}",
        f"- Dataset SHA-256: `{report['dataset_sha256']}`",
        f"- Coverage: {metrics['coverage']:.1%}",
        f"- Strict accuracy: {metrics['strict_accuracy']:.1%}",
        f"- Covered accuracy: {metrics['covered_accuracy']:.1%}",
        f"- Risk recall: {metrics['risk_recall']:.1%}",
        f"- NON_TARGET false-risk rate: {metrics['non_target_false_risk_rate']:.1%}",
        f"- Gate: {'PASS' if report['gate_pass'] else 'FAIL'}",
        f"- Promotion: `{report['promotion_decision']}`",
        "",
        "The set was frozen with expected labels and source bytes before inference. It has zero title/ID overlap with the training corpus and is not eligible for retraining model v1.",
        "",
        "## Gate details",
        "",
    ]
    for name, passed in report["gates"].items():
        lines.append(f"- {'PASS' if passed else 'FAIL'} — `{name}`")
    lines.extend(["", "## Errors and abstentions", ""])
    if report["failures"]:
        for item in report["failures"]:
            lines.append(
                f"- `{item['sample_id']}` {item['expected_label']} -> {item['predicted_label']} "
                f"({item['confidence']:.1%}) — {item['title']}"
            )
    else:
        lines.append("- None")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--model-card", type=Path, default=DEFAULT_MODEL_CARD)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()
    rows, freeze = load_and_verify(args.dataset, args.freeze)
    router = RiskRouter(args.artifact, args.model_card)
    report = evaluate(rows, freeze, router)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(report, args.markdown)
    print(json.dumps({key: report[key] for key in ("rows", "metrics", "gates", "gate_pass", "promotion_decision")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
