#!/usr/bin/env python3
"""Train the evidence-first three-class risk router without blind-set tuning."""

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
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix, f1_score
from sklearn.model_selection import StratifiedGroupKFold


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.train_risk_router import build_pipeline  # noqa: E402


DEFAULT_DEV = ROOT / "artifacts" / "risk_router_v3_ai_adjudications_dev.jsonl"
DEFAULT_BLIND = ROOT / "artifacts" / "risk_router_external_blind_v2.jsonl"
DEFAULT_FREEZE = ROOT / "artifacts" / "risk_router_external_blind_v2_freeze.json"
DEFAULT_ARTIFACT = ROOT / "artifacts" / "risk_router_v3_candidate.joblib"
DEFAULT_CARD = ROOT / "artifacts" / "risk_router_v3_candidate_model_card.json"
DEFAULT_REPORT = ROOT / "artifacts" / "risk_router_v3_candidate_dev_report.json"
DEFAULT_MARKDOWN = ROOT / "artifacts" / "risk_router_v3_candidate_dev_report.md"
DEFAULT_MANIFEST = ROOT / "artifacts" / "risk_router_v3_candidate_manifest.jsonl"

LABELS = ["RISK_REVIEW", "NON_TARGET", "ABSTAIN"]
THRESHOLDS = [0.0, 0.45, 0.50, 0.55, 0.60, 0.65]
RISK_RESCUE_POLICIES: list[tuple[float | None, float | None]] = [
    (None, None), (0.15, 0.10), (0.20, 0.10), (0.25, 0.10), (0.30, 0.10),
    (0.25, 0.30), (0.30, 0.30),
]
DEV_GATES = {
    "macro_f1_min": 0.80,
    "risk_recall_min": 0.85,
    "non_target_false_risk_max": 0.10,
    "abstain_recall_min": 0.75,
    "group_overlap_max": 0,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def validate_development(rows: list[dict[str, Any]]) -> None:
    if len(rows) < 100:
        raise ValueError("development set is unexpectedly small")
    for row in rows:
        if row.get("label") not in LABELS:
            raise ValueError(f"invalid label in {row.get('sample_id')}")
        if row.get("reviewer_type") != "AI_RUBRIC_ADJUDICATOR_NOT_HUMAN":
            raise ValueError(f"missing explicit AI label provenance in {row.get('sample_id')}")
        if row.get("human_reviewed") is not False:
            raise ValueError(f"AI row cannot claim human review in {row.get('sample_id')}")
        if hashlib.sha256(str(row.get("text") or "").encode("utf-8")).hexdigest() != row.get("text_sha256"):
            raise ValueError(f"text hash mismatch in {row.get('sample_id')}")
        axes = row.get("axes") or {}
        expected = {
            "RISK_REVIEW": ("MATERIAL_ADVERSE", "ADVERSE", "PRIMARY_SUPPORTED"),
            "NON_TARGET": ("NOT_MATERIAL_ADVERSE", {"POSITIVE", "NEUTRAL"}, "PRIMARY_SUPPORTED"),
        }
        if row["label"] == "RISK_REVIEW" and (
            axes.get("materiality"), axes.get("polarity"), axes.get("evidence_state")
        ) != expected["RISK_REVIEW"]:
            raise ValueError(f"risk axes mismatch in {row.get('sample_id')}")
        if row["label"] == "NON_TARGET" and not (
            axes.get("materiality") == expected["NON_TARGET"][0]
            and axes.get("polarity") in expected["NON_TARGET"][1]
            and axes.get("evidence_state") == expected["NON_TARGET"][2]
        ):
            raise ValueError(f"non-target axes mismatch in {row.get('sample_id')}")
        if row["label"] == "ABSTAIN" and axes.get("evidence_state") not in {
            "DISCOVERY_ONLY", "CONFLICTED", "INSUFFICIENT"
        }:
            raise ValueError(f"abstain evidence-state mismatch in {row.get('sample_id')}")


def connected_groups(rows: list[dict[str, Any]]) -> list[str]:
    parent: dict[str, str] = {}

    def find(value: str) -> str:
        parent.setdefault(value, value)
        if parent[value] != value:
            parent[value] = find(parent[value])
        return parent[value]

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    keys_by_row: list[list[str]] = []
    for row in rows:
        keys = [f"entity:{row['entity_group']}"]
        if row.get("event_chain_group"):
            keys.append(f"chain:{row['event_chain_group']}")
        keys_by_row.append(keys)
        for key in keys[1:]:
            union(keys[0], key)
    return [find(keys[0]) for keys in keys_by_row]


def apply_threshold(
    classes: list[str],
    probability_rows: np.ndarray,
    threshold: float,
    *,
    risk_rescue_floor: float | None = None,
    risk_rescue_margin: float | None = None,
) -> list[str]:
    predictions: list[str] = []
    for values in probability_rows:
        best_index = int(values.argmax())
        best_label = classes[best_index]
        confidence = float(values[best_index])
        risk_index = classes.index("RISK_REVIEW")
        risk_probability = float(values[risk_index])
        if (
            risk_rescue_floor is not None
            and risk_rescue_margin is not None
            and risk_probability >= risk_rescue_floor
            and risk_probability >= confidence - risk_rescue_margin
        ):
            predictions.append("RISK_REVIEW")
        else:
            predictions.append(best_label if best_label == "ABSTAIN" or confidence >= threshold else "ABSTAIN")
    return predictions


def metric_block(truth: list[str], predictions: list[str]) -> dict[str, Any]:
    report = classification_report(truth, predictions, labels=LABELS, output_dict=True, zero_division=0)
    non_target_total = sum(label == "NON_TARGET" for label in truth)
    non_target_false_risk = sum(
        actual == "NON_TARGET" and predicted == "RISK_REVIEW"
        for actual, predicted in zip(truth, predictions)
    ) / max(1, non_target_total)
    all_non_risk_total = sum(label != "RISK_REVIEW" for label in truth)
    all_non_risk_false_risk = sum(
        actual != "RISK_REVIEW" and predicted == "RISK_REVIEW"
        for actual, predicted in zip(truth, predictions)
    ) / max(1, all_non_risk_total)
    return {
        "accuracy": accuracy_score(truth, predictions),
        "macro_f1": f1_score(truth, predictions, labels=LABELS, average="macro", zero_division=0),
        "risk_recall": report["RISK_REVIEW"]["recall"],
        "risk_precision": report["RISK_REVIEW"]["precision"],
        "non_target_recall": report["NON_TARGET"]["recall"],
        "non_target_false_risk_rate": non_target_false_risk,
        "all_non_risk_false_risk_rate": all_non_risk_false_risk,
        "abstain_recall": report["ABSTAIN"]["recall"],
        "abstain_precision": report["ABSTAIN"]["precision"],
        "classification_report": report,
        "confusion_matrix": confusion_matrix(truth, predictions, labels=LABELS).tolist(),
    }


def gate_metrics(metrics: dict[str, Any], overlap_count: int) -> dict[str, bool]:
    return {
        "macro_f1": metrics["macro_f1"] >= DEV_GATES["macro_f1_min"],
        "risk_recall": metrics["risk_recall"] >= DEV_GATES["risk_recall_min"],
        "non_target_false_risk": metrics["non_target_false_risk_rate"] <= DEV_GATES["non_target_false_risk_max"],
        "abstain_recall": metrics["abstain_recall"] >= DEV_GATES["abstain_recall_min"],
        "group_overlap": overlap_count <= DEV_GATES["group_overlap_max"],
    }


def train(args: argparse.Namespace) -> dict[str, Any]:
    rows = read_jsonl(args.dev)
    validate_development(rows)
    freeze = json.loads(args.freeze.read_text(encoding="utf-8"))
    if hashlib.sha256(args.blind.read_bytes()).hexdigest() != freeze["dataset_sha256"]:
        raise ValueError("frozen blind dataset hash mismatch")
    blind_audit_rows = [
        {
            "sample_id": item["sample_id"],
            "event_id": item["event_id"],
            "entity_group": item["entity_group"],
            "event_chain_group": item.get("event_chain_group") or "",
            "text_sha256": item["text_sha256"],
            "prediction": item.get("prediction"),
        }
        for item in read_jsonl(args.blind)
    ]
    if any(row["prediction"] is not None for row in blind_audit_rows):
        raise ValueError("blind dataset already contains predictions")
    dev_ids = {row["event_id"] for row in rows}
    dev_entities = {row["entity_group"] for row in rows}
    dev_chains = {row["event_chain_group"] for row in rows if row.get("event_chain_group")}
    dev_text_hashes = {row["text_sha256"] for row in rows}
    overlap = {
        "event_id": len(dev_ids & {row["event_id"] for row in blind_audit_rows}),
        "entity_group": len(dev_entities & {row["entity_group"] for row in blind_audit_rows}),
        "event_chain_group": len(dev_chains & {row["event_chain_group"] for row in blind_audit_rows if row["event_chain_group"]}),
        "text_sha256": len(dev_text_hashes & {row["text_sha256"] for row in blind_audit_rows}),
    }
    overlap_count = sum(overlap.values())
    if overlap_count:
        raise ValueError(f"development/blind leakage detected: {overlap}")

    texts = [row["text"] for row in rows]
    labels = [row["label"] for row in rows]
    groups = connected_groups(rows)
    split = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=42)
    oof_probabilities = np.zeros((len(rows), len(LABELS)), dtype=float)
    seen = np.zeros(len(rows), dtype=bool)
    fold_audit: list[dict[str, Any]] = []
    group_overlap_total = 0
    for fold, (train_index, test_index) in enumerate(split.split(texts, labels, groups), 1):
        pipeline = build_pipeline("combined")
        pipeline.fit([texts[index] for index in train_index], [labels[index] for index in train_index])
        probabilities = pipeline.predict_proba([texts[index] for index in test_index])
        class_positions = {str(label): position for position, label in enumerate(pipeline.classes_)}
        for row_position, source_index in enumerate(test_index):
            for target_position, label in enumerate(LABELS):
                oof_probabilities[source_index, target_position] = probabilities[row_position, class_positions[label]]
            seen[source_index] = True
        train_groups = {groups[index] for index in train_index}
        test_groups = {groups[index] for index in test_index}
        fold_overlap = len(train_groups & test_groups)
        group_overlap_total += fold_overlap
        fold_audit.append(
            {
                "fold": fold,
                "train_rows": len(train_index),
                "test_rows": len(test_index),
                "train_labels": dict(Counter(labels[index] for index in train_index)),
                "test_labels": dict(Counter(labels[index] for index in test_index)),
                "group_overlap_count": fold_overlap,
            }
        )
    if not bool(seen.all()):
        raise RuntimeError("out-of-fold prediction coverage is incomplete")

    threshold_candidates: list[dict[str, Any]] = []
    for threshold in THRESHOLDS:
        for risk_floor, risk_margin in RISK_RESCUE_POLICIES:
            predictions = apply_threshold(
                LABELS,
                oof_probabilities,
                threshold,
                risk_rescue_floor=risk_floor,
                risk_rescue_margin=risk_margin,
            )
            metrics = metric_block(labels, predictions)
            gates = gate_metrics(metrics, group_overlap_total)
            threshold_candidates.append(
                {
                    "threshold": threshold,
                    "risk_rescue_floor": risk_floor,
                    "risk_rescue_margin": risk_margin,
                    "metrics": metrics,
                    "gates": gates,
                    "gate_pass": all(gates.values()),
                }
            )
    passing = [item for item in threshold_candidates if item["gate_pass"]]
    candidates = passing or threshold_candidates
    selected = max(
        candidates,
        key=lambda item: (
            item["metrics"]["macro_f1"],
            item["metrics"]["risk_recall"],
            -item["metrics"]["non_target_false_risk_rate"],
            -item["threshold"],
        ),
    )

    dataset_sha256 = hashlib.sha256(args.dev.read_bytes()).hexdigest()
    model_version = f"risk-router-v3-{dataset_sha256[:12]}"
    final_pipeline = build_pipeline("combined")
    final_pipeline.fit(texts, labels)
    trained_at = utc_now()
    bundle = {
        "pipeline": final_pipeline,
        "model_version": model_version,
        "abstain_threshold": selected["threshold"],
        "risk_rescue_floor": selected["risk_rescue_floor"],
        "risk_rescue_margin": selected["risk_rescue_margin"],
        "trained_at": trained_at,
        "dataset_sha256": dataset_sha256,
        "classes": [str(value) for value in final_pipeline.classes_],
        "label_provenance": "AI_RUBRIC_ADJUDICATOR_NOT_HUMAN",
        "blind_freeze_id": freeze["freeze_id"],
        "blind_dataset_sha256": freeze["dataset_sha256"],
        "no_trading": True,
        "shadow": True,
    }
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, args.artifact, compress=3)
    artifact_sha256 = hashlib.sha256(args.artifact.read_bytes()).hexdigest()
    args.artifact.with_suffix(".sha256").write_text(
        f"{artifact_sha256}  {args.artifact.name}\n", encoding="ascii"
    )

    report = {
        "schema_version": 1,
        "evaluation_type": "development_grouped_5_fold_out_of_fold",
        "created_at": trained_at,
        "model_version": model_version,
        "artifact_sha256": artifact_sha256,
        "development_dataset_sha256": dataset_sha256,
        "development_rows": len(rows),
        "label_counts": dict(Counter(labels)),
        "label_provenance": "AI_RUBRIC_ADJUDICATOR_NOT_HUMAN",
        "human_labels_claimed": False,
        "blind_freeze_id_reserved_not_evaluated": freeze["freeze_id"],
        "blind_predictions_used_for_training_or_thresholding": False,
        "development_blind_overlap": overlap,
        "folds": fold_audit,
        "selected_threshold": selected["threshold"],
        "selected_risk_rescue_floor": selected["risk_rescue_floor"],
        "selected_risk_rescue_margin": selected["risk_rescue_margin"],
        "selected_metrics": selected["metrics"],
        "threshold_candidates": threshold_candidates,
        "gates": selected["gates"],
        "gate_thresholds": DEV_GATES,
        "gate_pass": selected["gate_pass"],
        "promotion_decision": "ELIGIBLE_FOR_ONE_TIME_BLIND" if selected["gate_pass"] else "HOLD_SHADOW",
        "no_trading": True,
        "shadow": True,
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown.write_text(
        "\n".join(
            [
                "# Risk Router v3 development evaluation",
                "",
                f"- Model: `{model_version}`",
                f"- Labels: AI rubric adjudications, explicitly not human labels",
                f"- Rows: `{len(rows)}`; grouped five-fold OOF; group overlap `{group_overlap_total}`",
                f"- Threshold selected without blind predictions: `{selected['threshold']:.2f}`",
                f"- Risk-rescue floor / margin selected on development only: `{selected['risk_rescue_floor']}` / `{selected['risk_rescue_margin']}`",
                f"- Accuracy / macro F1: `{selected['metrics']['accuracy']:.3f}` / `{selected['metrics']['macro_f1']:.3f}`",
                f"- Risk recall: `{selected['metrics']['risk_recall']:.3f}`",
                f"- Normal-news false-risk rate: `{selected['metrics']['non_target_false_risk_rate']:.3f}`",
                f"- Abstain recall: `{selected['metrics']['abstain_recall']:.3f}`",
                f"- Development gate: `{'PASS' if selected['gate_pass'] else 'FAIL'}`",
                "- Blind v2 was hash-checked for separation only and was not inferred during training.",
                "- Mode remains SHADOW / NO TRADING.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    card = {
        "schema_version": 3,
        "model_name": "Finance Radar Evidence-First Risk Router",
        "model_version": model_version,
        "artifact_sha256": artifact_sha256,
        "trained_at": trained_at,
        "task": "Route evidence-stage event text to RISK_REVIEW, NON_TARGET, or ABSTAIN.",
        "intended_use": "Shadow-mode queue prioritization behind evidence and event-truth gates.",
        "label_provenance": "AI_RUBRIC_ADJUDICATOR_NOT_HUMAN",
        "human_labels_claimed": False,
        "features": ["word TF-IDF 1-2 grams", "character TF-IDF 3-5 grams"],
        "estimator": "sigmoid-calibrated class-balanced logistic regression",
        "development_evaluation": {
            "strategy": report["evaluation_type"],
            "rows": len(rows),
            "label_counts": report["label_counts"],
            "metrics": selected["metrics"],
            "gate_pass": selected["gate_pass"],
        },
        "blind_evaluation": {
            "freeze_id": freeze["freeze_id"],
            "dataset_sha256": freeze["dataset_sha256"],
            "status": "RESERVED_NOT_EVALUATED",
        },
        "limitations": [
            "AI rubric labels are auditable but are not a substitute for independent human double adjudication.",
            "The model prioritizes material adverse risk and is not a general sentiment or return model.",
            "Source, issuer and language drift require ongoing shadow monitoring.",
            "ABSTAIN is a first-class output; it must not be silently converted to verified or rejected.",
        ],
        "explicit_non_uses": [
            "trading or order execution", "return prediction", "position sizing",
            "automatic event verification", "automatic factual finality",
        ],
        "no_trading": True,
        "shadow": True,
    }
    args.card.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    args.manifest.write_text(
        "\n".join(
            stable_json(
                {
                    "sample_id": row["sample_id"],
                    "event_id": row["event_id"],
                    "label": row["label"],
                    "text_sha256": row["text_sha256"],
                    "entity_group_sha256": hashlib.sha256(row["entity_group"].encode()).hexdigest(),
                    "event_chain_group_sha256": hashlib.sha256(row["event_chain_group"].encode()).hexdigest()
                    if row.get("event_chain_group") else None,
                    "label_provenance": row["reviewer_type"],
                    "human_reviewed": False,
                }
            )
            for row in rows
        ) + "\n",
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
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    report = train(args)
    print(
        json.dumps(
            {
                "model_version": report["model_version"],
                "artifact_sha256": report["artifact_sha256"],
                "selected_threshold": report["selected_threshold"],
                "selected_risk_rescue_floor": report["selected_risk_rescue_floor"],
                "selected_risk_rescue_margin": report["selected_risk_rescue_margin"],
                "metrics": report["selected_metrics"],
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
