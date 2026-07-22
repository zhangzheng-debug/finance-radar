#!/usr/bin/env python3
"""Train the v4 binary semantic router behind the structured evidence gate."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
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
from scripts.train_risk_router_v3 import connected_groups, read_jsonl, stable_json  # noqa: E402
from app.models.semantic_policy_gate import SEMANTIC_POLICY_VERSION, assess_semantic_policy  # noqa: E402


DEFAULT_DEV = ROOT / "artifacts" / "risk_router_v4_semantic_dev.jsonl"
DEFAULT_BLIND = ROOT / "artifacts" / "risk_router_external_blind_v3.jsonl"
DEFAULT_FREEZE = ROOT / "artifacts" / "risk_router_external_blind_v3_freeze.json"
DEFAULT_V2_REPORT = ROOT / "artifacts" / "risk_router_external_blind_v2_report.json"
DEFAULT_ARTIFACT = ROOT / "artifacts" / "risk_router_v4_candidate.joblib"
DEFAULT_CARD = ROOT / "artifacts" / "risk_router_v4_candidate_model_card.json"
DEFAULT_REPORT = ROOT / "artifacts" / "risk_router_v4_candidate_dev_report.json"
DEFAULT_MARKDOWN = ROOT / "artifacts" / "risk_router_v4_candidate_dev_report.md"
DEFAULT_MANIFEST = ROOT / "artifacts" / "risk_router_v4_candidate_manifest.jsonl"

LABELS = ["RISK_REVIEW", "NON_TARGET"]
SEMANTIC_RISK_THRESHOLDS = [0.48, 0.50, 0.51, 0.52, 0.54, 0.56, 0.58, 0.60]
DEV_GATES = {
    "macro_f1_min": 0.85,
    "risk_recall_min": 0.85,
    "non_target_false_risk_max": 0.10,
    "group_overlap_max": 0,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def predict_policy(
    probability_rows: np.ndarray,
    *,
    texts: list[str],
    semantic_risk_threshold: float,
) -> list[str]:
    predictions: list[str] = []
    for text, values in zip(texts, probability_rows):
        policy = assess_semantic_policy(text)
        if policy.decision != "DEFER_TO_MODEL":
            label = policy.decision
        else:
            label = "RISK_REVIEW" if float(values[0]) >= semantic_risk_threshold else "NON_TARGET"
        predictions.append(label)
    return predictions


def metrics(truth: list[str], predictions: list[str]) -> dict[str, Any]:
    report = classification_report(truth, predictions, labels=LABELS, output_dict=True, zero_division=0)
    normal_total = sum(label == "NON_TARGET" for label in truth)
    return {
        "accuracy": accuracy_score(truth, predictions),
        "macro_f1": f1_score(truth, predictions, labels=LABELS, average="macro", zero_division=0),
        "risk_recall": report["RISK_REVIEW"]["recall"],
        "risk_precision": report["RISK_REVIEW"]["precision"],
        "non_target_recall": report["NON_TARGET"]["recall"],
        "non_target_false_risk_rate": sum(
            actual == "NON_TARGET" and predicted == "RISK_REVIEW"
            for actual, predicted in zip(truth, predictions)
        ) / max(1, normal_total),
        "classification_report": report,
        "confusion_matrix": confusion_matrix(truth, predictions, labels=LABELS).tolist(),
    }


def gate(metrics_block: dict[str, Any], overlap: int) -> dict[str, bool]:
    return {
        "macro_f1": metrics_block["macro_f1"] >= DEV_GATES["macro_f1_min"],
        "risk_recall": metrics_block["risk_recall"] >= DEV_GATES["risk_recall_min"],
        "non_target_false_risk": metrics_block["non_target_false_risk_rate"] <= DEV_GATES["non_target_false_risk_max"],
        "group_overlap": overlap <= DEV_GATES["group_overlap_max"],
    }


def train(args: argparse.Namespace) -> dict[str, Any]:
    rows = read_jsonl(args.dev)
    if len(rows) < 80 or set(row.get("label") for row in rows) != set(LABELS):
        raise ValueError("v4 development corpus must be a substantive two-class set")
    for row in rows:
        if row.get("reviewer_type") != "AI_RUBRIC_ADJUDICATOR_NOT_HUMAN" or row.get("human_reviewed") is not False:
            raise ValueError(f"invalid label provenance in {row.get('sample_id')}")
        if hashlib.sha256(row["text"].encode("utf-8")).hexdigest() != row["text_sha256"]:
            raise ValueError(f"text hash mismatch in {row.get('sample_id')}")
        if row["axes"]["evidence_state"] != "PRIMARY_SUPPORTED":
            raise ValueError(f"semantic training row lacks primary support: {row.get('sample_id')}")
    freeze = json.loads(args.freeze.read_text(encoding="utf-8"))
    if hashlib.sha256(args.blind.read_bytes()).hexdigest() != freeze["dataset_sha256"]:
        raise ValueError("blind-v3 freeze mismatch")
    v2_report = json.loads(args.v2_report.read_text(encoding="utf-8"))
    if v2_report.get("gate_pass") is not False:
        raise ValueError("v2 failure evidence is missing")
    blind_audit = [
        {
            "event_id": row["event_id"],
            "entity_group": row["entity_group"],
            "event_chain_group": row.get("event_chain_group") or "",
            "text_sha256": row["text_sha256"],
            "prediction": row.get("prediction"),
        }
        for row in read_jsonl(args.blind)
    ]
    if any(row["prediction"] is not None for row in blind_audit):
        raise ValueError("blind-v3 already contains predictions")
    overlap = {
        "event_id": len({row["event_id"] for row in rows} & {row["event_id"] for row in blind_audit}),
        "entity_group": len({row["entity_group"] for row in rows} & {row["entity_group"] for row in blind_audit}),
        "event_chain_group": len(
            {row["event_chain_group"] for row in rows if row.get("event_chain_group")}
            & {row["event_chain_group"] for row in blind_audit if row.get("event_chain_group")}
        ),
        "text_sha256": len({row["text_sha256"] for row in rows} & {row["text_sha256"] for row in blind_audit}),
    }
    if sum(overlap.values()):
        raise ValueError(f"v4 development/blind leakage: {overlap}")

    texts = [row["text"] for row in rows]
    truth = [row["label"] for row in rows]
    groups = connected_groups(rows)
    probabilities = np.zeros((len(rows), len(LABELS)), dtype=float)
    seen = np.zeros(len(rows), dtype=bool)
    folds: list[dict[str, Any]] = []
    group_overlap = 0
    splitter = StratifiedGroupKFold(n_splits=5, shuffle=True, random_state=43)
    for fold, (train_index, test_index) in enumerate(splitter.split(texts, truth, groups), 1):
        pipeline = build_pipeline("combined")
        pipeline.fit([texts[index] for index in train_index], [truth[index] for index in train_index])
        predicted_probabilities = pipeline.predict_proba([texts[index] for index in test_index])
        positions = {str(label): index for index, label in enumerate(pipeline.classes_)}
        for position, source_index in enumerate(test_index):
            probabilities[source_index] = [predicted_probabilities[position, positions[label]] for label in LABELS]
            seen[source_index] = True
        train_groups = {groups[index] for index in train_index}
        test_groups = {groups[index] for index in test_index}
        fold_overlap = len(train_groups & test_groups)
        group_overlap += fold_overlap
        folds.append(
            {
                "fold": fold,
                "train_rows": len(train_index),
                "test_rows": len(test_index),
                "train_labels": dict(Counter(truth[index] for index in train_index)),
                "test_labels": dict(Counter(truth[index] for index in test_index)),
                "group_overlap_count": fold_overlap,
            }
        )
    if not bool(seen.all()):
        raise RuntimeError("incomplete v4 OOF coverage")

    candidates: list[dict[str, Any]] = []
    for semantic_risk_threshold in SEMANTIC_RISK_THRESHOLDS:
        predictions = predict_policy(
            probabilities,
            texts=texts,
            semantic_risk_threshold=semantic_risk_threshold,
        )
        result = metrics(truth, predictions)
        gates = gate(result, group_overlap)
        candidates.append(
            {
                "semantic_risk_threshold": semantic_risk_threshold,
                "metrics": result,
                "gates": gates,
                "gate_pass": all(gates.values()),
            }
        )
    passing = [candidate for candidate in candidates if candidate["gate_pass"]]
    selected = max(
        passing or candidates,
        key=lambda candidate: (
            candidate["metrics"]["macro_f1"],
            candidate["metrics"]["risk_recall"],
            -candidate["metrics"]["non_target_false_risk_rate"],
        ),
    )

    development_sha256 = hashlib.sha256(args.dev.read_bytes()).hexdigest()
    model_version = f"risk-router-v4-{development_sha256[:12]}"
    final_pipeline = build_pipeline("combined")
    final_pipeline.fit(texts, truth)
    bundle = {
        "pipeline": final_pipeline,
        "model_version": model_version,
        "architecture": "structured_evidence_gate_plus_binary_semantic_router_v1",
        "abstain_threshold": 0.0,
        "semantic_policy_version": SEMANTIC_POLICY_VERSION,
        "semantic_risk_threshold": selected["semantic_risk_threshold"],
        "risk_rescue_floor": None,
        "risk_rescue_margin": None,
        "trained_at": utc_now(),
        "dataset_sha256": development_sha256,
        "classes": [str(value) for value in final_pipeline.classes_],
        "label_provenance": "AI_RUBRIC_ADJUDICATOR_NOT_HUMAN",
        "blind_freeze_id": freeze["freeze_id"],
        "blind_dataset_sha256": freeze["dataset_sha256"],
        "predecessor_v2_failure_sha256": hashlib.sha256(args.v2_report.read_bytes()).hexdigest(),
        "no_trading": True,
        "shadow": True,
    }
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, args.artifact, compress=3)
    artifact_sha256 = hashlib.sha256(args.artifact.read_bytes()).hexdigest()
    args.artifact.with_suffix(".sha256").write_text(f"{artifact_sha256}  {args.artifact.name}\n", encoding="ascii")
    report = {
        "schema_version": 1,
        "evaluation_type": "binary_semantic_grouped_5_fold_oof",
        "created_at": bundle["trained_at"],
        "model_version": model_version,
        "artifact_sha256": artifact_sha256,
        "architecture": bundle["architecture"],
        "development_dataset_sha256": development_sha256,
        "development_rows": len(rows),
        "label_counts": dict(Counter(truth)),
        "folds": folds,
        "development_blind_overlap": overlap,
        "selected_policy": {
            "semantic_policy_version": SEMANTIC_POLICY_VERSION,
            "semantic_risk_threshold": selected["semantic_risk_threshold"],
        },
        "selected_metrics": selected["metrics"],
        "policy_candidates": candidates,
        "gates": selected["gates"],
        "gate_thresholds": DEV_GATES,
        "gate_pass": selected["gate_pass"],
        "blind_v3_predictions_used": False,
        "promotion_decision": "ELIGIBLE_FOR_ONE_TIME_BLIND_V3" if selected["gate_pass"] else "HOLD_SHADOW",
        "no_trading": True,
        "shadow": True,
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown.write_text(
        "\n".join(
            [
                "# Risk Router v4 development evaluation",
                "",
                f"- Model: `{model_version}`",
                "- Architecture: structured evidence gate + binary semantic router.",
                f"- Development rows: `{len(rows)}` `{dict(Counter(truth))}`",
                f"- OOF accuracy / macro F1: `{selected['metrics']['accuracy']:.3f}` / `{selected['metrics']['macro_f1']:.3f}`",
                f"- OOF risk recall: `{selected['metrics']['risk_recall']:.3f}`",
                f"- OOF normal-news false-risk: `{selected['metrics']['non_target_false_risk_rate']:.3f}`",
                f"- Development gate: `{'PASS' if selected['gate_pass'] else 'FAIL'}`",
                "- Blind-v3 was hash-checked for separation only and not inferred.",
                "- Labels are AI rubric adjudications, explicitly not human labels.",
                "- Mode remains SHADOW / NO TRADING.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    card = {
        "schema_version": 4,
        "model_name": "Finance Radar Evidence-Gated Semantic Risk Router",
        "model_version": model_version,
        "artifact_sha256": artifact_sha256,
        "trained_at": bundle["trained_at"],
        "architecture": bundle["architecture"],
        "task": "Classify primary-supported event text as RISK_REVIEW or NON_TARGET; evidence gate owns ABSTAIN.",
        "label_provenance": "AI_RUBRIC_ADJUDICATOR_NOT_HUMAN",
        "human_labels_claimed": False,
        "development_evaluation": report,
        "blind_evaluation": {"freeze_id": freeze["freeze_id"], "status": "RESERVED_NOT_EVALUATED"},
        "limitations": [
            "AI rubric labels are not independent human double adjudication.",
            "The semantic model must never run as if unreviewed discovery text were primary-supported.",
            "The model is a shadow queue aid, not a fact verifier, sentiment engine, or trading model.",
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
                    "evidence_state": row["axes"]["evidence_state"],
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
    parser.add_argument("--v2-report", type=Path, default=DEFAULT_V2_REPORT)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--card", type=Path, default=DEFAULT_CARD)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    report = train(args)
    print(json.dumps({
        "model_version": report["model_version"],
        "metrics": report["selected_metrics"],
        "gates": report["gates"],
        "gate_pass": report["gate_pass"],
        "promotion_decision": report["promotion_decision"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["gate_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
