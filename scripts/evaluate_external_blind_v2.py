#!/usr/bin/env python3
"""Run the one-time frozen blind-v2 evaluation for the v3 candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.risk_router import RiskRouter  # noqa: E402
from scripts.train_risk_router_v3 import (  # noqa: E402
    LABELS,
    apply_threshold,
    metric_block,
    read_jsonl,
)


DEFAULT_DEV = ROOT / "artifacts" / "risk_router_v3_ai_adjudications_dev.jsonl"
DEFAULT_BLIND = ROOT / "artifacts" / "risk_router_external_blind_v2.jsonl"
DEFAULT_FREEZE = ROOT / "artifacts" / "risk_router_external_blind_v2_freeze.json"
DEFAULT_ARTIFACT = ROOT / "artifacts" / "risk_router_v3_candidate.joblib"
DEFAULT_CARD = ROOT / "artifacts" / "risk_router_v3_candidate_model_card.json"
DEFAULT_DEV_REPORT = ROOT / "artifacts" / "risk_router_v3_candidate_dev_report.json"
DEFAULT_REPORT = ROOT / "artifacts" / "risk_router_external_blind_v2_report.json"
DEFAULT_MARKDOWN = ROOT / "artifacts" / "risk_router_external_blind_v2_report.md"

BLIND_GATES = {
    "rows_exact": 80,
    "risk_rows_min": 30,
    "non_target_rows_min": 30,
    "abstain_rows_min": 20,
    "accuracy_min": 0.80,
    "macro_f1_min": 0.80,
    "risk_recall_min": 0.85,
    "non_target_false_risk_max": 0.10,
    "abstain_recall_min": 0.75,
    "runtime_accuracy_min": 0.75,
    "runtime_risk_recall_min": 0.80,
    "overlap_max": 0,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_metrics(rows: list[dict[str, Any]], predictions: list[str]) -> dict[str, Any]:
    grouped_truth: dict[str, list[str]] = defaultdict(list)
    grouped_predictions: dict[str, list[str]] = defaultdict(list)
    for row, prediction in zip(rows, predictions):
        grouped_truth[row["source_group"]].append(row["expected_label"])
        grouped_predictions[row["source_group"]].append(prediction)
    output: dict[str, Any] = {}
    for source in sorted(grouped_truth):
        truth = grouped_truth[source]
        predicted = grouped_predictions[source]
        output[source] = {
            "rows": len(truth),
            "label_counts": dict(Counter(truth)),
            "accuracy": sum(a == b for a, b in zip(truth, predicted)) / len(truth),
            "risk_false_positive_count": sum(
                actual != "RISK_REVIEW" and guess == "RISK_REVIEW"
                for actual, guess in zip(truth, predicted)
            ),
        }
    return output


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    freeze = json.loads(args.freeze.read_text(encoding="utf-8"))
    blind_sha256 = hashlib.sha256(args.blind.read_bytes()).hexdigest()
    if blind_sha256 != freeze["dataset_sha256"]:
        raise ValueError("blind-v2 bytes no longer match the freeze manifest")
    rows = read_jsonl(args.blind)
    if any(row.get("prediction") is not None for row in rows):
        raise ValueError("frozen blind-v2 must remain prediction-free")
    if any(row.get("expected_label") not in LABELS for row in rows):
        raise ValueError("blind-v2 contains an invalid expected label")

    artifact_sha256 = hashlib.sha256(args.artifact.read_bytes()).hexdigest()
    bundle = joblib.load(args.artifact)
    dev_sha256 = hashlib.sha256(args.dev.read_bytes()).hexdigest()
    if bundle.get("dataset_sha256") != dev_sha256:
        raise ValueError("candidate was not trained from the current development dataset")
    if bundle.get("blind_dataset_sha256") != blind_sha256 or bundle.get("blind_freeze_id") != freeze["freeze_id"]:
        raise ValueError("candidate was not reserved against this exact blind freeze")
    dev_report = json.loads(args.dev_report.read_text(encoding="utf-8"))
    if not dev_report.get("gate_pass"):
        raise ValueError("development gate did not pass")
    if dev_report.get("artifact_sha256") != artifact_sha256:
        raise ValueError("development report artifact hash mismatch")

    dev_rows = read_jsonl(args.dev)
    dev_ids = {row["event_id"] for row in dev_rows}
    dev_entities = {row["entity_group"] for row in dev_rows}
    dev_chains = {row["event_chain_group"] for row in dev_rows if row.get("event_chain_group")}
    dev_hashes = {row["text_sha256"] for row in dev_rows}
    overlap = {
        "event_id": len(dev_ids & {row["event_id"] for row in rows}),
        "entity_group": len(dev_entities & {row["entity_group"] for row in rows}),
        "event_chain_group": len(dev_chains & {row["event_chain_group"] for row in rows if row.get("event_chain_group")}),
        "text_sha256": len(dev_hashes & {row["text_sha256"] for row in rows}),
    }
    overlap_count = sum(overlap.values())

    pipeline = bundle["pipeline"]
    probability_rows = pipeline.predict_proba([row["text"] for row in rows])
    class_positions = {str(label): position for position, label in enumerate(pipeline.classes_)}
    ordered_probabilities = np.asarray(
        [[values[class_positions[label]] for label in LABELS] for values in probability_rows],
        dtype=float,
    )
    direct_predictions = apply_threshold(
        LABELS,
        ordered_probabilities,
        float(bundle.get("abstain_threshold", 0.0)),
        risk_rescue_floor=bundle.get("risk_rescue_floor"),
        risk_rescue_margin=bundle.get("risk_rescue_margin"),
    )
    truth = [row["expected_label"] for row in rows]
    direct_metrics = metric_block(truth, direct_predictions)

    runtime_router = RiskRouter(args.artifact, args.card)
    runtime_results = [runtime_router.predict(row["text"]) for row in rows]
    runtime_predictions = [result["label"] for result in runtime_results]
    runtime_metrics = metric_block(truth, runtime_predictions)

    label_counts = Counter(truth)
    gates = {
        "rows": len(rows) == BLIND_GATES["rows_exact"],
        "risk_rows": label_counts["RISK_REVIEW"] >= BLIND_GATES["risk_rows_min"],
        "non_target_rows": label_counts["NON_TARGET"] >= BLIND_GATES["non_target_rows_min"],
        "abstain_rows": label_counts["ABSTAIN"] >= BLIND_GATES["abstain_rows_min"],
        "accuracy": direct_metrics["accuracy"] >= BLIND_GATES["accuracy_min"],
        "macro_f1": direct_metrics["macro_f1"] >= BLIND_GATES["macro_f1_min"],
        "risk_recall": direct_metrics["risk_recall"] >= BLIND_GATES["risk_recall_min"],
        "non_target_false_risk": direct_metrics["non_target_false_risk_rate"] <= BLIND_GATES["non_target_false_risk_max"],
        "abstain_recall": direct_metrics["abstain_recall"] >= BLIND_GATES["abstain_recall_min"],
        "runtime_accuracy": runtime_metrics["accuracy"] >= BLIND_GATES["runtime_accuracy_min"],
        "runtime_risk_recall": runtime_metrics["risk_recall"] >= BLIND_GATES["runtime_risk_recall_min"],
        "overlap": overlap_count <= BLIND_GATES["overlap_max"],
        "freeze_hash": blind_sha256 == freeze["dataset_sha256"],
        "development_gate": bool(dev_report["gate_pass"]),
    }
    gate_pass = all(gates.values())
    predictions = []
    for index, (row, direct, runtime, values, runtime_result) in enumerate(
        zip(rows, direct_predictions, runtime_predictions, ordered_probabilities, runtime_results), 1
    ):
        predictions.append(
            {
                "row": index,
                "sample_id": row["sample_id"],
                "event_id": row["event_id"],
                "source_group": row["source_group"],
                "expected_label": row["expected_label"],
                "direct_prediction": direct,
                "runtime_prediction": runtime,
                "direct_probabilities": {
                    label: round(float(value), 8) for label, value in zip(LABELS, values)
                },
                "runtime": runtime_result["runtime"],
                "scope_gate_decision": runtime_result["scope_gate"]["decision"],
                "direct_correct": direct == row["expected_label"],
                "runtime_correct": runtime == row["expected_label"],
            }
        )
    report = {
        "schema_version": 2,
        "evaluation_type": "frozen_label_first_external_blind_v2_one_time",
        "evaluated_at": utc_now(),
        "freeze_id": freeze["freeze_id"],
        "dataset_sha256": blind_sha256,
        "rows": len(rows),
        "label_counts": dict(label_counts),
        "source_counts": dict(Counter(row["source_group"] for row in rows)),
        "label_provenance": freeze["reviewer_type"],
        "human_labels_claimed": False,
        "model_version": bundle["model_version"],
        "model_artifact_sha256": artifact_sha256,
        "training_dataset_sha256": dev_sha256,
        "decision_policy": {
            "abstain_threshold": bundle.get("abstain_threshold"),
            "risk_rescue_floor": bundle.get("risk_rescue_floor"),
            "risk_rescue_margin": bundle.get("risk_rescue_margin"),
            "selected_on": "development OOF only",
        },
        "overlap_audit": overlap,
        "direct_metrics": direct_metrics,
        "runtime_metrics": runtime_metrics,
        "direct_source_metrics": source_metrics(rows, direct_predictions),
        "runtime_source_metrics": source_metrics(rows, runtime_predictions),
        "gate_thresholds": BLIND_GATES,
        "gates": gates,
        "gate_pass": gate_pass,
        "promotion_decision": "QUALIFIED_SHADOW" if gate_pass else "HOLD_SHADOW",
        "frozen_file_mutated_with_predictions": False,
        "predictions": predictions,
        "no_trading": True,
        "shadow": True,
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown.write_text(
        "\n".join(
            [
                "# Risk Router external blind v2",
                "",
                f"- Freeze: `{freeze['freeze_id']}`",
                f"- Model: `{bundle['model_version']}`",
                f"- Rows / labels: `{len(rows)}` / `{dict(label_counts)}`",
                "- Labels: auditable AI rubric adjudications, explicitly not human labels",
                f"- Direct accuracy / macro F1: `{direct_metrics['accuracy']:.3f}` / `{direct_metrics['macro_f1']:.3f}`",
                f"- Direct risk recall: `{direct_metrics['risk_recall']:.3f}`",
                f"- Direct normal-news false-risk rate: `{direct_metrics['non_target_false_risk_rate']:.3f}`",
                f"- Direct abstain recall: `{direct_metrics['abstain_recall']:.3f}`",
                f"- Runtime accuracy / risk recall: `{runtime_metrics['accuracy']:.3f}` / `{runtime_metrics['risk_recall']:.3f}`",
                f"- Development/blind overlap: `{overlap_count}`",
                f"- Blind gate: `{'PASS' if gate_pass else 'FAIL'}`",
                f"- Decision: `{report['promotion_decision']}`",
                "- Frozen blind file remains prediction-free; detailed predictions live only in this report.",
                "- Mode remains SHADOW / NO TRADING.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", type=Path, default=DEFAULT_DEV)
    parser.add_argument("--blind", type=Path, default=DEFAULT_BLIND)
    parser.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--card", type=Path, default=DEFAULT_CARD)
    parser.add_argument("--dev-report", type=Path, default=DEFAULT_DEV_REPORT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()
    report = evaluate(args)
    print(
        json.dumps(
            {
                "model_version": report["model_version"],
                "direct_metrics": report["direct_metrics"],
                "runtime_metrics": report["runtime_metrics"],
                "gates": report["gates"],
                "gate_pass": report["gate_pass"],
                "promotion_decision": report["promotion_decision"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["gate_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
