#!/usr/bin/env python3
"""Train an unreleased shadow router from prepared dual-human development rows."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
from sklearn.metrics import classification_report, confusion_matrix, f1_score

from app.models.semantic_policy_gate import SEMANTIC_POLICY_VERSION, assess_semantic_policy
from scripts.train_risk_router import build_pipeline


LABELS = ("RISK_REVIEW", "NON_TARGET")
THRESHOLDS = (0.48, 0.50, 0.52, 0.54, 0.56, 0.58, 0.60)
GATES = {"macro_f1_min": 0.85, "risk_recall_min": 0.85, "false_risk_max": 0.10}


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _metrics(truth: list[str], predictions: list[str]) -> dict[str, Any]:
    report = classification_report(
        truth, predictions, labels=list(LABELS), output_dict=True, zero_division=0
    )
    normal = sum(label == "NON_TARGET" for label in truth)
    return {
        "macro_f1": f1_score(truth, predictions, labels=list(LABELS), average="macro", zero_division=0),
        "risk_recall": report["RISK_REVIEW"]["recall"],
        "risk_precision": report["RISK_REVIEW"]["precision"],
        "non_target_false_risk_rate": sum(
            actual == "NON_TARGET" and predicted == "RISK_REVIEW"
            for actual, predicted in zip(truth, predictions)
        ) / max(1, normal),
        "confusion_matrix": confusion_matrix(truth, predictions, labels=list(LABELS)).tolist(),
    }


def _predict(probabilities: Any, classes: Any, texts: list[str], threshold: float) -> list[str]:
    risk_position = [str(value) for value in classes].index("RISK_REVIEW")
    result: list[str] = []
    for text, values in zip(texts, probabilities):
        policy = assess_semantic_policy(text)
        if policy.decision in LABELS:
            result.append(policy.decision)
        else:
            result.append("RISK_REVIEW" if float(values[risk_position]) >= threshold else "NON_TARGET")
    return result


def train(
    development: Path,
    artifact: Path,
    report_path: Path,
    card_path: Path,
    *,
    minimum_rows: int = 160,
) -> dict[str, Any]:
    rows = _rows(development)
    if len(rows) < minimum_rows:
        raise ValueError(f"human-gold development rows must be at least {minimum_rows}")
    for row in rows:
        if row.get("split") not in {"TRAIN", "VALIDATION"}:
            raise ValueError("HUMAN_BLIND or unknown split reached model training")
        if row.get("label") not in LABELS:
            raise ValueError("semantic development contains ABSTAIN or unknown label")
        if row.get("label_provenance") != "INDEPENDENT_DUAL_HUMAN_OR_ARBITRATED":
            raise ValueError("development label is not dual-human provenance")
        if row.get("post_event_market_data_included") is not False:
            raise ValueError("post-event market data reached model training")
        if row.get("model_output_included_in_review") is not False:
            raise ValueError("model output reached human review")
        if hashlib.sha256(str(row.get("text") or "").encode()).hexdigest() != row.get("content_sha256"):
            raise ValueError(f"content hash mismatch: {row.get('sample_id')}")

    train_rows = [row for row in rows if row["split"] == "TRAIN"]
    validation_rows = [row for row in rows if row["split"] == "VALIDATION"]
    if not train_rows or not validation_rows:
        raise ValueError("TRAIN and VALIDATION must both be present")
    if {row["label"] for row in train_rows} != set(LABELS):
        raise ValueError("TRAIN must contain both semantic labels")
    for field in ("event_id", "entity_group", "event_chain_group", "content_sha256"):
        overlap = {str(row.get(field)) for row in train_rows} & {
            str(row.get(field)) for row in validation_rows
        }
        if overlap:
            raise ValueError(f"TRAIN/VALIDATION leakage in {field}")

    pipeline = build_pipeline("combined")
    pipeline.fit(
        [row["text"] for row in train_rows],
        [row["label"] for row in train_rows],
    )
    texts = [row["text"] for row in validation_rows]
    truth = [row["label"] for row in validation_rows]
    probabilities = pipeline.predict_proba(texts)
    candidates = []
    for threshold in THRESHOLDS:
        metrics = _metrics(truth, _predict(probabilities, pipeline.classes_, texts, threshold))
        gates = {
            "macro_f1": metrics["macro_f1"] >= GATES["macro_f1_min"],
            "risk_recall": metrics["risk_recall"] >= GATES["risk_recall_min"],
            "non_target_false_risk": metrics["non_target_false_risk_rate"] <= GATES["false_risk_max"],
        }
        candidates.append({"threshold": threshold, "metrics": metrics, "gates": gates, "gate_pass": all(gates.values())})
    selected = max(
        [item for item in candidates if item["gate_pass"]] or candidates,
        key=lambda item: (
            item["metrics"]["macro_f1"],
            item["metrics"]["risk_recall"],
            -item["metrics"]["non_target_false_risk_rate"],
        ),
    )
    dataset_sha256 = hashlib.sha256(development.read_bytes()).hexdigest()
    model_version = f"risk-router-human-gold-{dataset_sha256[:12]}"
    bundle = {
        "pipeline": pipeline,
        "model_version": model_version,
        "semantic_policy_version": SEMANTIC_POLICY_VERSION,
        "semantic_risk_threshold": selected["threshold"],
        "dataset_sha256": dataset_sha256,
        "label_provenance": "INDEPENDENT_DUAL_HUMAN_OR_ARBITRATED",
        "human_blind_labels_read": False,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "no_trading": True,
        "shadow": True,
    }
    artifact.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, artifact, compress=3)
    artifact_sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
    report = {
        "schema_version": 1,
        "model_version": model_version,
        "artifact_sha256": artifact_sha256,
        "development_dataset_sha256": dataset_sha256,
        "train_rows": len(train_rows),
        "validation_rows": len(validation_rows),
        "train_labels": dict(Counter(row["label"] for row in train_rows)),
        "validation_labels": dict(Counter(truth)),
        "selected": selected,
        "candidates": candidates,
        "gate_thresholds": GATES,
        "promotion_decision": "ELIGIBLE_FOR_SEALED_HUMAN_BLIND_EVALUATION" if selected["gate_pass"] else "HOLD_SHADOW",
        "label_provenance": bundle["label_provenance"],
        "human_blind_labels_read": False,
        "production_model_changed": False,
        "no_trading": True,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    card = {
        "schema_version": 1,
        "model_version": model_version,
        "artifact_sha256": artifact_sha256,
        "label_provenance": bundle["label_provenance"],
        "human_labels_claimed": True,
        "human_blind_status": "RESERVED_NOT_READ",
        "validation": report,
        "limitations": [
            "This candidate has not been evaluated on the sealed HUMAN_BLIND split.",
            "ABSTAIN remains owned by the structured evidence gate.",
            "The artifact is shadow-only and cannot place trades.",
        ],
        "shadow": True,
        "no_trading": True,
    }
    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text(json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--card", type=Path, required=True)
    args = parser.parse_args()
    result = train(args.development, args.artifact, args.report, args.card)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["selected"]["gate_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
