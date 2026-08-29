#!/usr/bin/env python3
"""Evaluate deterministic hard-case anchors layered over Qwen predictions.

This evaluator is offline-only.  It does not mutate a runtime manifest or open
the owner holdout.  The deterministic lane handles only the narrow mechanisms
defined by the independent hard-case miner; Qwen remains responsible for all
other semantic polarity/materiality decisions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.qwen_risk_contract import expected_semantic_payload
from scripts.evaluate_qwen_semantic_adapter import (
    gate_decision,
    load_jsonl,
    normalize_payload,
    sha256_file,
    stable_json,
    summarize_predictions,
)
from scripts.prepare_qwen_semantic_hardcase_sft import (
    _semantic_text,
    classify_hardcase,
)


def apply_hybrid_prediction(
    dataset_row: dict[str, Any], prediction: dict[str, Any]
) -> dict[str, Any]:
    result = json.loads(json.dumps(prediction))
    content = json.loads(dataset_row["messages"][1]["content"])
    decision = classify_hardcase(_semantic_text(content))
    result["model_predicted"] = prediction.get("predicted")
    result["model_contract_valid"] = bool(prediction.get("contract_valid"))
    if decision:
        (materiality, polarity), rule = decision
        result["predicted"] = normalize_payload(expected_semantic_payload(materiality, polarity))
        result["contract_valid"] = True
        result["contract_issues"] = []
        result["decision_source"] = "DETERMINISTIC_HARDCASE_ANCHOR"
        result["hardcase_rule"] = rule
    else:
        result["decision_source"] = "QWEN_ADAPTER"
        result["hardcase_rule"] = None
    result["exact_match"] = bool(
        result["contract_valid"] and result["predicted"] == result["expected"]
    )
    return result


def evaluate(*, dataset: Path, predictions: Path, output_dir: Path) -> dict[str, Any]:
    dataset_rows = load_jsonl(dataset)
    model_rows = load_jsonl(predictions)
    if len(dataset_rows) != len(model_rows):
        raise ValueError("dataset and prediction row counts differ")
    hybrid_rows: list[dict[str, Any]] = []
    for dataset_row, prediction in zip(dataset_rows, model_rows):
        expected_sample_id = str(dataset_row.get("metadata", {}).get("sample_id") or "")
        if expected_sample_id != str(prediction.get("sample_id") or ""):
            raise ValueError("dataset and predictions are not aligned by sample_id")
        hybrid_rows.append(apply_hybrid_prediction(dataset_row, prediction))

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "hybrid_predictions.jsonl"
    output_path.write_text(
        "".join(stable_json(row) + "\n" for row in hybrid_rows), encoding="utf-8"
    )
    model_metrics = summarize_predictions(model_rows)
    hybrid_metrics = summarize_predictions(hybrid_rows)
    report = {
        "schema_version": 1,
        "evaluation_only": True,
        "production_model_changed": False,
        "owner_holdout_opened": False,
        "human_gold_claimed": False,
        "dataset_path": str(dataset),
        "dataset_sha256": sha256_file(dataset),
        "model_predictions_path": str(predictions),
        "model_predictions_sha256": sha256_file(predictions),
        "decision_source_counts": {
            source: sum(row["decision_source"] == source for row in hybrid_rows)
            for source in ("DETERMINISTIC_HARDCASE_ANCHOR", "QWEN_ADAPTER")
        },
        "model_only_metrics": model_metrics,
        "hybrid_metrics": hybrid_metrics,
        "hybrid_gate": gate_decision(hybrid_metrics),
        "hybrid_predictions_sha256": sha256_file(output_path),
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(
        dataset=args.dataset.resolve(),
        predictions=args.predictions.resolve(),
        output_dir=args.output_dir.resolve(),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["hybrid_gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
