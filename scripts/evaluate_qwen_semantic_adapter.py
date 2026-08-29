#!/usr/bin/env python3
"""Evaluate a Qwen semantic LoRA adapter without changing production state.

The evaluator consumes the immutable JSONL splits produced by
``prepare_qwen_semantic_consensus_sft.py``.  It decodes only the assistant
continuation, validates the bounded semantic contract, writes every prediction,
and reports exact/axis metrics plus a risk-priority gate decision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract one JSON object from plain or fenced model output."""

    candidate = str(text or "").strip()
    candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.IGNORECASE)
    candidate = re.sub(r"\s*```$", "", candidate)
    try:
        value = json.loads(candidate)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, character in enumerate(candidate):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(candidate[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def normalize_payload(value: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    fields = ("materiality", "polarity", "adverse_strength", "semantic_priority")
    return {field: str(value.get(field) or "").strip().upper() for field in fields}


def confusion_rows(truth: Iterable[str], predicted: Iterable[str]) -> dict[str, dict[str, int]]:
    result: dict[str, Counter[str]] = {}
    for expected, actual in zip(truth, predicted):
        result.setdefault(expected, Counter())[actual] += 1
    return {label: dict(sorted(counts.items())) for label, counts in sorted(result.items())}


def classification_metrics(truth: list[str], predicted: list[str]) -> dict[str, Any]:
    labels = sorted(set(truth))
    per_class: dict[str, dict[str, float | int]] = {}
    f1_values: list[float] = []
    for label in labels:
        true_positive = sum(a == label and b == label for a, b in zip(truth, predicted))
        false_positive = sum(a != label and b == label for a, b in zip(truth, predicted))
        false_negative = sum(a == label and b != label for a, b in zip(truth, predicted))
        precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
        recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        f1_values.append(f1)
        per_class[label] = {
            "support": sum(value == label for value in truth),
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    return {
        "accuracy": sum(a == b for a, b in zip(truth, predicted)) / len(truth) if truth else 0.0,
        "macro_f1_truth_supported_classes": sum(f1_values) / len(f1_values) if f1_values else 0.0,
        "truth_supported_labels": labels,
        "per_class": per_class,
        "confusion": confusion_rows(truth, predicted),
    }


def summarize_predictions(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    parsed = [row for row in rows if row["contract_valid"]]
    fields = ("materiality", "polarity", "adverse_strength", "semantic_priority")
    metrics: dict[str, Any] = {
        "rows": total,
        "contract_valid_rows": len(parsed),
        "parse_success_rate": len(parsed) / total if total else 0.0,
        "exact_payload_accuracy": sum(row["exact_match"] for row in rows) / total if total else 0.0,
    }
    for field in fields:
        truth = [row["expected"][field] for row in rows]
        predicted = [
            row["predicted"][field] if row["contract_valid"] else "__INVALID__"
            for row in rows
        ]
        metrics[field] = classification_metrics(truth, predicted)
    expected_priority = [row["expected"]["semantic_priority"] for row in rows]
    predicted_priority = [
        row["predicted"]["semantic_priority"] if row["contract_valid"] else "__INVALID__"
        for row in rows
    ]
    positive_support = sum(value == "PRIORITY_REVIEW" for value in expected_priority)
    true_positive = sum(
        expected == "PRIORITY_REVIEW" and actual == "PRIORITY_REVIEW"
        for expected, actual in zip(expected_priority, predicted_priority)
    )
    negative_support = sum(value != "PRIORITY_REVIEW" for value in expected_priority)
    false_positive = sum(
        expected != "PRIORITY_REVIEW" and actual == "PRIORITY_REVIEW"
        for expected, actual in zip(expected_priority, predicted_priority)
    )
    metrics["priority_review"] = {
        "support": positive_support,
        "recall": true_positive / positive_support if positive_support else None,
        "non_priority_support": negative_support,
        "false_priority_rate": false_positive / negative_support if negative_support else None,
    }
    return metrics


def gate_decision(metrics: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "parse_success_rate_ge_1_00": metrics["parse_success_rate"] >= 1.0,
        "materiality_macro_f1_ge_0_65": metrics["materiality"]["macro_f1_truth_supported_classes"] >= 0.65,
        "polarity_macro_f1_ge_0_55": metrics["polarity"]["macro_f1_truth_supported_classes"] >= 0.55,
        "priority_review_recall_ge_0_75": (metrics["priority_review"]["recall"] or 0.0) >= 0.75,
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "decision": "QUALIFIED_SHADOW_SEMANTIC_CANDIDATE" if all(checks.values()) else "NOT_QUALIFIED",
    }


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_inference(
    *,
    base_model: Path,
    adapter: Path,
    dataset: Path,
    output_dir: Path,
    max_new_tokens: int,
) -> dict[str, Any]:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    from app.models.qwen_risk_contract import validate_semantic_payload

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
    for index, row in enumerate(load_jsonl(dataset), start=1):
        messages = row["messages"][:-1]
        expected = normalize_payload(json.loads(row["messages"][-1]["content"]))
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
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
        predicted = normalize_payload(extract_json_object(raw_output))
        issues = validate_semantic_payload(predicted)
        contract_valid = not issues
        exact_match = bool(contract_valid and predicted == expected)
        predictions.append(
            {
                "index": index,
                "sample_id": row["metadata"]["sample_id"],
                "event_id": row["metadata"].get("event_id"),
                "expected": expected,
                "predicted": predicted,
                "raw_output": raw_output,
                "contract_issues": issues,
                "contract_valid": contract_valid,
                "exact_match": exact_match,
            }
        )
        print(f"{index}/{len(predictions) if False else '?'} {row['metadata']['sample_id']} valid={contract_valid} exact={exact_match}", flush=True)

    output_dir.mkdir(parents=True, exist_ok=True)
    prediction_path = output_dir / "predictions.jsonl"
    prediction_path.write_text(
        "".join(stable_json(row) + "\n" for row in predictions), encoding="utf-8"
    )
    metrics = summarize_predictions(predictions)
    report = {
        "schema_version": 1,
        "evaluation_only": True,
        "production_model_changed": False,
        "human_gold_claimed": False,
        "dataset_path": str(dataset),
        "dataset_sha256": sha256_file(dataset),
        "base_model": str(base_model),
        "adapter": str(adapter),
        "metrics": metrics,
        "gate": gate_decision(metrics),
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
    parser.add_argument("--max-new-tokens", type=int, default=96)
    args = parser.parse_args()
    report = run_inference(
        base_model=args.base_model.resolve(),
        adapter=args.adapter.resolve(),
        dataset=args.dataset.resolve(),
        output_dir=args.output_dir.resolve(),
        max_new_tokens=args.max_new_tokens,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
