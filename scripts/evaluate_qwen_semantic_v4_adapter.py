#!/usr/bin/env python3
"""Evaluate a Qwen semantic v4 LoRA on the reserved group-isolated TEST set.

This evaluator understands the v2 categorical/mechanism contract emitted by
``prepare_qwen_semantic_v4_sft.py``. It performs no production writes. The TEST
set must be evaluated only after model and decoding parameters are frozen.
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

from app.models.qwen_risk_contract import validate_semantic_payload  # noqa: E402
from app.models.qwen_risk_contract_v2 import (  # noqa: E402
    semantic_priority_v2,
    validate_semantic_v2_payload,
)
from scripts.evaluate_qwen_semantic_adapter import (  # noqa: E402
    classification_metrics,
    extract_json_object,
    gate_decision as gate_core_v1_decision,
    normalize_payload as normalize_core_v1_payload,
    sha256_file,
    stable_json,
    summarize_predictions as summarize_core_v1_predictions,
)


CATEGORICAL_FIELDS = (
    "materiality",
    "polarity",
    "impact_strength",
    "event_realization",
    "subject_relation",
    "risk_status",
    "novelty",
)
TARGET_CONTRACTS = frozenset({"core-v1", "full-v2"})


def _adapter_sha256(path: Path) -> str:
    if path.is_file():
        return sha256_file(path)
    candidate = path / "adapter_model.safetensors"
    if not candidate.is_file():
        candidate = path / "adapter_model.bin"
    if not candidate.is_file():
        raise ValueError("adapter directory has no adapter_model weights")
    return sha256_file(candidate)


def _normalized_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    normalized = {
        **{
            field: str(value.get(field) or "").strip().upper()
            for field in CATEGORICAL_FIELDS
        },
        "reason_codes": sorted(
            str(code).strip().upper()
            for code in (value.get("reason_codes") or [])
            if str(code).strip()
        ),
        "brief_reason": " ".join(str(value.get("brief_reason") or "").split()),
    }
    return normalized


def _reason_code_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    codes = sorted(
        {
            code
            for row in rows
            for source in (row["expected"], row.get("predicted") or {})
            for code in source.get("reason_codes", [])
        }
    )
    per_code: dict[str, Any] = {}
    total_tp = total_fp = total_fn = 0
    for code in codes:
        tp = fp = fn = 0
        for row in rows:
            expected = code in row["expected"]["reason_codes"]
            predicted = bool(row["contract_valid"] and code in row["predicted"]["reason_codes"])
            tp += int(expected and predicted)
            fp += int(not expected and predicted)
            fn += int(expected and not predicted)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_code[code] = {"support": tp + fn, "precision": precision, "recall": recall, "f1": f1}
        total_tp += tp
        total_fp += fp
        total_fn += fn
    micro_precision = total_tp / (total_tp + total_fp) if total_tp + total_fp else 0.0
    micro_recall = total_tp / (total_tp + total_fn) if total_tp + total_fn else 0.0
    micro_f1 = (
        2 * micro_precision * micro_recall / (micro_precision + micro_recall)
        if micro_precision + micro_recall
        else 0.0
    )
    return {
        "micro_precision": micro_precision,
        "micro_recall": micro_recall,
        "micro_f1": micro_f1,
        "per_code": per_code,
    }


def summarize_full_v2_predictions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    metrics: dict[str, Any] = {
        "rows": total,
        "contract_valid_rows": sum(row["contract_valid"] for row in rows),
        "parse_success_rate": (
            sum(row["contract_valid"] for row in rows) / total if total else 0.0
        ),
        "semantic_exact_accuracy": (
            sum(row["semantic_exact_match"] for row in rows) / total if total else 0.0
        ),
    }
    for field in CATEGORICAL_FIELDS:
        truth = [row["expected"][field] for row in rows]
        predicted = [
            row["predicted"][field] if row["contract_valid"] else "__INVALID__"
            for row in rows
        ]
        metrics[field] = classification_metrics(truth, predicted)
    expected_priority = [
        semantic_priority_v2(row["expected"]["materiality"], row["expected"]["polarity"])
        for row in rows
    ]
    predicted_priority = [
        (
            semantic_priority_v2(row["predicted"]["materiality"], row["predicted"]["polarity"])
            if row["contract_valid"]
            else "__INVALID__"
        )
        for row in rows
    ]
    support = sum(value == "PRIORITY_REVIEW" for value in expected_priority)
    true_positive = sum(
        expected == "PRIORITY_REVIEW" and predicted == "PRIORITY_REVIEW"
        for expected, predicted in zip(expected_priority, predicted_priority)
    )
    non_priority = total - support
    false_positive = sum(
        expected != "PRIORITY_REVIEW" and predicted == "PRIORITY_REVIEW"
        for expected, predicted in zip(expected_priority, predicted_priority)
    )
    metrics["priority_review"] = {
        "support": support,
        "recall": true_positive / support if support else None,
        "non_priority_support": non_priority,
        "false_priority_rate": false_positive / non_priority if non_priority else None,
    }
    metrics["reason_codes"] = _reason_code_metrics(rows)
    return metrics


def gate_full_v2_decision(metrics: dict[str, Any]) -> dict[str, Any]:
    mixed = metrics["polarity"]["per_class"].get("MIXED")
    positive = metrics["polarity"]["per_class"].get("POSITIVE")
    false_priority = metrics["priority_review"]["false_priority_rate"]
    checks = {
        "rows_ge_120": metrics["rows"] >= 120,
        "priority_support_ge_20": metrics["priority_review"]["support"] >= 20,
        "parse_success_rate_eq_1_00": metrics["parse_success_rate"] >= 1.0,
        "materiality_macro_f1_ge_0_70": metrics["materiality"]["macro_f1_truth_supported_classes"] >= 0.70,
        "polarity_macro_f1_ge_0_65": metrics["polarity"]["macro_f1_truth_supported_classes"] >= 0.65,
        "impact_strength_macro_f1_ge_0_60": metrics["impact_strength"][
            "macro_f1_truth_supported_classes"
        ]
        >= 0.60,
        "priority_recall_ge_0_75": (metrics["priority_review"]["recall"] or 0.0) >= 0.75,
        "false_priority_rate_le_0_10": false_priority is not None and false_priority <= 0.10,
        "mixed_recall_ge_0_50_when_supported": (
            mixed is None or mixed["support"] < 10 or mixed["recall"] >= 0.50
        ),
        "positive_recall_ge_0_60_when_supported": (
            positive is None or positive["support"] < 10 or positive["recall"] >= 0.60
        ),
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "decision": "QUALIFIED_FOR_SHADOW_ONLY" if all(checks.values()) else "NOT_QUALIFIED",
    }


def summarize_predictions(
    rows: list[dict[str, Any]], target_contract: str = "full-v2"
) -> dict[str, Any]:
    if target_contract == "full-v2":
        return summarize_full_v2_predictions(rows)
    if target_contract == "core-v1":
        core_rows = [
            {
                **row,
                "exact_match": row["semantic_exact_match"],
            }
            for row in rows
        ]
        return summarize_core_v1_predictions(core_rows)
    raise ValueError(f"unsupported target contract: {target_contract}")


def gate_decision(
    metrics: dict[str, Any], target_contract: str = "full-v2"
) -> dict[str, Any]:
    if target_contract == "full-v2":
        return gate_full_v2_decision(metrics)
    if target_contract == "core-v1":
        return gate_core_v1_decision(metrics)
    raise ValueError(f"unsupported target contract: {target_contract}")


def _load_dataset(path: Path, target_contract: str) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        raise ValueError("reserved TEST dataset is empty")
    for number, row in enumerate(rows, 1):
        if row.get("metadata", {}).get("split") != "TEST":
            raise ValueError(f"dataset line {number} is not reserved TEST")
        if row.get("metadata", {}).get("target_contract") != target_contract:
            raise ValueError(f"dataset line {number} target contract mismatch")
        raw_expected = json.loads(row["messages"][-1]["content"])
        issues = (
            validate_semantic_payload(raw_expected)
            if target_contract == "core-v1"
            else validate_semantic_v2_payload(raw_expected)
        )
        if issues:
            raise ValueError(f"dataset line {number} has invalid expected target: {','.join(issues)}")
    return rows


def run_inference(
    *,
    base_model: Path,
    adapter: Path,
    dataset: Path,
    output_dir: Path,
    max_new_tokens: int,
    target_contract: str = "full-v2",
) -> dict[str, Any]:
    if target_contract not in TARGET_CONTRACTS:
        raise ValueError("target contract must be core-v1 or full-v2")
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    rows = _load_dataset(dataset, target_contract)
    tokenizer = AutoTokenizer.from_pretrained(base_model, local_files_only=True)
    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=torch.float16,
    )
    model = AutoModelForCausalLM.from_pretrained(
        base_model,
        local_files_only=True,
        device_map="auto",
        torch_dtype=torch.float16,
        quantization_config=quantization,
    )
    model = PeftModel.from_pretrained(model, adapter, is_trainable=False)
    model.eval()
    predictions: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        expected_raw = json.loads(row["messages"][-1]["content"])
        expected = (
            normalize_core_v1_payload(expected_raw)
            if target_contract == "core-v1"
            else _normalized_payload(expected_raw)
        )
        prompt = tokenizer.apply_chat_template(
            row["messages"][:-1], tokenize=False, add_generation_prompt=True
        )
        encoded = tokenizer(prompt, return_tensors="pt").to(model.device)
        with torch.inference_mode():
            generated = model.generate(
                **encoded,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                pad_token_id=tokenizer.eos_token_id,
            )
        continuation = generated[0, encoded["input_ids"].shape[1] :]
        raw_output = tokenizer.decode(continuation, skip_special_tokens=True).strip()
        raw_predicted = extract_json_object(raw_output)
        issues = (
            validate_semantic_payload(raw_predicted)
            if target_contract == "core-v1"
            else validate_semantic_v2_payload(raw_predicted)
        )
        predicted = (
            normalize_core_v1_payload(raw_predicted)
            if target_contract == "core-v1"
            else _normalized_payload(raw_predicted)
        )
        valid = not issues
        if target_contract == "core-v1":
            semantic_exact = bool(valid and predicted == expected)
        else:
            semantic_exact = bool(
                valid
                and all(predicted[field] == expected[field] for field in CATEGORICAL_FIELDS)
                and predicted["reason_codes"] == expected["reason_codes"]
            )
        predictions.append(
            {
                "index": index,
                "sample_id": row["metadata"]["sample_id"],
                "event_id": row["metadata"].get("event_id"),
                "expected": expected,
                "predicted": predicted,
                "raw_output": raw_output,
                "contract_issues": issues,
                "contract_valid": valid,
                "semantic_exact_match": semantic_exact,
            }
        )
        print(f"{index}/{len(rows)} valid={valid} exact={semantic_exact}", flush=True)
    output_dir.mkdir(parents=True)
    prediction_path = output_dir / "predictions.jsonl"
    prediction_path.write_text(
        "".join(stable_json(row) + "\n" for row in predictions), encoding="utf-8"
    )
    metrics = summarize_predictions(predictions, target_contract)
    report = {
        "schema_version": 1,
        "evaluation_only": True,
        "reserved_test_only": True,
        "production_model_changed": False,
        "production_ledger_changed": False,
        "human_gold_claimed": False,
        "target_contract": target_contract,
        "dataset_sha256": sha256_file(dataset),
        "adapter_sha256": _adapter_sha256(adapter),
        "base_model": str(base_model),
        "metrics": metrics,
        "gate": gate_decision(metrics, target_contract),
        "predictions_sha256": sha256_file(prediction_path),
    }
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-model", type=Path, required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-new-tokens", type=int, default=320)
    parser.add_argument(
        "--target-contract", choices=sorted(TARGET_CONTRACTS), default="full-v2"
    )
    args = parser.parse_args()
    report = run_inference(
        base_model=args.base_model.resolve(),
        adapter=args.adapter.resolve(),
        dataset=args.dataset.resolve(),
        output_dir=args.output_dir.resolve(),
        max_new_tokens=args.max_new_tokens,
        target_contract=args.target_contract,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
