#!/usr/bin/env python3
"""Run the one-time frozen blind-v3 evaluation for the evidence-gated v4 router."""

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


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.risk_router import EVIDENCE_GATE_VERSION, RiskRouter  # noqa: E402
from scripts.train_risk_router_v3 import metric_block, read_jsonl  # noqa: E402
from scripts.train_risk_router_v4 import LABELS, metrics as semantic_metrics  # noqa: E402


DEFAULT_DEV = ROOT / "artifacts" / "risk_router_v4_semantic_dev.jsonl"
DEFAULT_BLIND = ROOT / "artifacts" / "risk_router_external_blind_v3.jsonl"
DEFAULT_FREEZE = ROOT / "artifacts" / "risk_router_external_blind_v3_freeze.json"
DEFAULT_V2_REPORT = ROOT / "artifacts" / "risk_router_external_blind_v2_report.json"
DEFAULT_ARTIFACT = ROOT / "artifacts" / "risk_router_v4_candidate.joblib"
DEFAULT_CARD = ROOT / "artifacts" / "risk_router_v4_candidate_model_card.json"
DEFAULT_DEV_REPORT = ROOT / "artifacts" / "risk_router_v4_candidate_dev_report.json"
DEFAULT_REPORT = ROOT / "artifacts" / "risk_router_external_blind_v3_report.json"
DEFAULT_MARKDOWN = ROOT / "artifacts" / "risk_router_external_blind_v3_report.md"

GATES = {
    "rows_exact": 80,
    "full_accuracy_min": 0.80,
    "full_macro_f1_min": 0.80,
    "full_risk_recall_min": 0.85,
    "full_non_target_false_risk_max": 0.10,
    "full_abstain_recall_min": 0.90,
    "semantic_macro_f1_min": 0.85,
    "semantic_risk_recall_min": 0.85,
    "semantic_non_target_false_risk_max": 0.10,
    "overlap_max": 0,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def evidence_context(row: dict[str, Any]) -> dict[str, Any]:
    state = str(row["axes"]["evidence_state"])
    if state == "PRIMARY_SUPPORTED":
        state = "PRIMARY_SUPPORTED_FROZEN_BLIND"
    return {
        "version": EVIDENCE_GATE_VERSION,
        "state": state,
        "reason_codes": ["frozen_blind_structured_evidence_state"],
        "evidence_count": 1,
    }


def evaluate(args: argparse.Namespace) -> dict[str, Any]:
    freeze = json.loads(args.freeze.read_text(encoding="utf-8"))
    blind_sha256 = hashlib.sha256(args.blind.read_bytes()).hexdigest()
    if blind_sha256 != freeze["dataset_sha256"]:
        raise ValueError("blind-v3 freeze hash mismatch")
    rows = read_jsonl(args.blind)
    if any(row.get("prediction") is not None for row in rows):
        raise ValueError("blind-v3 must remain prediction-free")
    v2_report = json.loads(args.v2_report.read_text(encoding="utf-8"))
    if v2_report.get("gate_pass") is not False:
        raise ValueError("the predecessor v2 failure report must remain present")
    bundle = joblib.load(args.artifact)
    artifact_sha256 = hashlib.sha256(args.artifact.read_bytes()).hexdigest()
    dev_sha256 = hashlib.sha256(args.dev.read_bytes()).hexdigest()
    if bundle.get("architecture") != "structured_evidence_gate_plus_binary_semantic_router_v1":
        raise ValueError("candidate does not implement the v4 split architecture")
    if bundle.get("dataset_sha256") != dev_sha256:
        raise ValueError("candidate development hash mismatch")
    if bundle.get("blind_dataset_sha256") != blind_sha256 or bundle.get("blind_freeze_id") != freeze["freeze_id"]:
        raise ValueError("candidate was not reserved against blind-v3")
    dev_report = json.loads(args.dev_report.read_text(encoding="utf-8"))
    if not dev_report.get("gate_pass") or dev_report.get("artifact_sha256") != artifact_sha256:
        raise ValueError("v4 development gate or artifact hash is invalid")

    dev_rows = read_jsonl(args.dev)
    overlap = {
        "event_id": len({row["event_id"] for row in dev_rows} & {row["event_id"] for row in rows}),
        "entity_group": len({row["entity_group"] for row in dev_rows} & {row["entity_group"] for row in rows}),
        "event_chain_group": len(
            {row["event_chain_group"] for row in dev_rows if row.get("event_chain_group")}
            & {row["event_chain_group"] for row in rows if row.get("event_chain_group")}
        ),
        "text_sha256": len({row["text_sha256"] for row in dev_rows} & {row["text_sha256"] for row in rows}),
    }
    overlap_count = sum(overlap.values())

    router = RiskRouter(args.artifact, args.card)
    results = [router.predict(row["text"], evidence_context=evidence_context(row)) for row in rows]
    truth = [row["expected_label"] for row in rows]
    predictions = [result["label"] for result in results]
    full_metrics = metric_block(truth, predictions)

    substantive_indices = [index for index, row in enumerate(rows) if row["expected_label"] in LABELS]
    semantic_truth = [truth[index] for index in substantive_indices]
    semantic_predictions = [predictions[index] for index in substantive_indices]
    if any(label not in LABELS for label in semantic_predictions):
        semantic_result = {
            "accuracy": sum(a == b for a, b in zip(semantic_truth, semantic_predictions)) / len(semantic_truth),
            "macro_f1": 0.0,
            "risk_recall": 0.0,
            "risk_precision": 0.0,
            "non_target_recall": 0.0,
            "non_target_false_risk_rate": 1.0,
            "classification_report": {},
            "confusion_matrix": [],
        }
    else:
        semantic_result = semantic_metrics(semantic_truth, semantic_predictions)

    gates = {
        "rows": len(rows) == GATES["rows_exact"],
        "full_accuracy": full_metrics["accuracy"] >= GATES["full_accuracy_min"],
        "full_macro_f1": full_metrics["macro_f1"] >= GATES["full_macro_f1_min"],
        "full_risk_recall": full_metrics["risk_recall"] >= GATES["full_risk_recall_min"],
        "full_non_target_false_risk": full_metrics["non_target_false_risk_rate"] <= GATES["full_non_target_false_risk_max"],
        "full_abstain_recall": full_metrics["abstain_recall"] >= GATES["full_abstain_recall_min"],
        "semantic_macro_f1": semantic_result["macro_f1"] >= GATES["semantic_macro_f1_min"],
        "semantic_risk_recall": semantic_result["risk_recall"] >= GATES["semantic_risk_recall_min"],
        "semantic_non_target_false_risk": semantic_result["non_target_false_risk_rate"] <= GATES["semantic_non_target_false_risk_max"],
        "overlap": overlap_count <= GATES["overlap_max"],
        "development_gate": bool(dev_report["gate_pass"]),
        "v2_failure_preserved": v2_report.get("gate_pass") is False,
    }
    gate_pass = all(gates.values())
    prediction_rows = [
        {
            "row": index,
            "sample_id": row["sample_id"],
            "event_id": row["event_id"],
            "source_group": row["source_group"],
            "evidence_state": row["axes"]["evidence_state"],
            "expected_label": row["expected_label"],
            "prediction": result["label"],
            "runtime": result["runtime"],
            "semantic_policy": result["runtime"] == "semantic_policy_gate",
            "correct": row["expected_label"] == result["label"],
        }
        for index, (row, result) in enumerate(zip(rows, results), 1)
    ]
    report = {
        "schema_version": 3,
        "evaluation_type": "frozen_label_first_blind_v3_split_architecture_one_time",
        "evaluated_at": utc_now(),
        "freeze_id": freeze["freeze_id"],
        "dataset_sha256": blind_sha256,
        "rows": len(rows),
        "label_counts": dict(Counter(truth)),
        "source_counts": dict(Counter(row["source_group"] for row in rows)),
        "label_provenance": freeze["reviewer_type"],
        "human_labels_claimed": False,
        "model_version": bundle["model_version"],
        "model_artifact_sha256": artifact_sha256,
        "training_dataset_sha256": dev_sha256,
        "architecture": bundle["architecture"],
        "semantic_policy_version": bundle["semantic_policy_version"],
        "semantic_risk_threshold": bundle["semantic_risk_threshold"],
        "predecessor_v2": {
            "freeze_id": v2_report["freeze_id"],
            "gate_pass": False,
            "role": "EXPOSED_FAILURE_DIAGNOSTIC",
        },
        "overlap_audit": overlap,
        "full_layered_metrics": full_metrics,
        "semantic_substantive_metrics": semantic_result,
        "gate_thresholds": GATES,
        "gates": gates,
        "gate_pass": gate_pass,
        "promotion_decision": "QUALIFIED_SHADOW" if gate_pass else "HOLD_SHADOW",
        "frozen_file_mutated_with_predictions": False,
        "predictions": prediction_rows,
        "no_trading": True,
        "shadow": True,
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown.write_text(
        "\n".join(
            [
                "# Risk Router external blind v3",
                "",
                f"- Freeze / model: `{freeze['freeze_id']}` / `{bundle['model_version']}`",
                "- Architecture: structured evidence gate + high-precision semantic policy + binary small model.",
                f"- Rows / labels: `{len(rows)}` / `{dict(Counter(truth))}`",
                f"- Full accuracy / macro F1: `{full_metrics['accuracy']:.3f}` / `{full_metrics['macro_f1']:.3f}`",
                f"- Full risk recall / normal false-risk: `{full_metrics['risk_recall']:.3f}` / `{full_metrics['non_target_false_risk_rate']:.3f}`",
                f"- Full abstain recall: `{full_metrics['abstain_recall']:.3f}`",
                f"- Semantic macro F1 / risk recall: `{semantic_result['macro_f1']:.3f}` / `{semantic_result['risk_recall']:.3f}`",
                f"- Blind gate: `{'PASS' if gate_pass else 'FAIL'}`; decision `{report['promotion_decision']}`",
                "- Blind-v2 FAIL remains preserved; blind-v3 is disjoint and was frozen before v4 inference.",
                "- Frozen blind-v3 file remains prediction-free.",
                "- Labels are AI rubric adjudications, explicitly not human labels.",
                "- Mode remains SHADOW / NO TRADING.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    card = json.loads(args.card.read_text(encoding="utf-8"))
    card["blind_evaluation"] = {
        "freeze_id": freeze["freeze_id"],
        "dataset_sha256": blind_sha256,
        "report_path": args.report.name,
        "full_layered_metrics": full_metrics,
        "semantic_substantive_metrics": semantic_result,
        "gate_pass": gate_pass,
        "promotion_decision": report["promotion_decision"],
    }
    args.card.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev", type=Path, default=DEFAULT_DEV)
    parser.add_argument("--blind", type=Path, default=DEFAULT_BLIND)
    parser.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE)
    parser.add_argument("--v2-report", type=Path, default=DEFAULT_V2_REPORT)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--card", type=Path, default=DEFAULT_CARD)
    parser.add_argument("--dev-report", type=Path, default=DEFAULT_DEV_REPORT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    args = parser.parse_args()
    report = evaluate(args)
    print(json.dumps({
        "model_version": report["model_version"],
        "full_layered_metrics": report["full_layered_metrics"],
        "semantic_substantive_metrics": report["semantic_substantive_metrics"],
        "gates": report["gates"],
        "gate_pass": report["gate_pass"],
        "promotion_decision": report["promotion_decision"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["gate_pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
