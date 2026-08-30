#!/usr/bin/env python3
"""Build a fail-closed Qwen v3 runtime manifest from frozen evaluation artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


RUNTIME_CONTRACT = "finance-radar-qwen-risk-runtime-v2"
BASE_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
PROMPT_VERSION = "qwen-risk-dual-review-consensus-v2"
SEMANTIC_CONTRACT = "qwen-risk-semantics-v1"
HYBRID_POLICY_VERSION = "qwen-v3-narrow-anchors-v1"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def build(
    model_path: Path,
    lora_path: Path,
    adapter_path: Path,
    sft_manifest_path: Path,
    model_report_path: Path,
    hybrid_report_path: Path,
    policy_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    paths = [
        model_path,
        lora_path,
        adapter_path,
        sft_manifest_path,
        model_report_path,
        hybrid_report_path,
        policy_path,
    ]
    paths = [path.resolve() for path in paths]
    (
        model_path,
        lora_path,
        adapter_path,
        sft_manifest_path,
        model_report_path,
        hybrid_report_path,
        policy_path,
    ) = paths
    output_path = output_path.resolve()
    if output_path.exists():
        raise ValueError("runtime manifest already exists")
    if model_path.name != "qwen2.5-1.5b-instruct-q4_k_m.gguf":
        raise ValueError("unexpected base GGUF filename")
    if lora_path.name != "finance-radar-qwen-risk-v3-lora-f16.gguf":
        raise ValueError("unexpected LoRA GGUF filename")

    sft = _read_json(sft_manifest_path)
    model_report = _read_json(model_report_path)
    hybrid_report = _read_json(hybrid_report_path)
    policy = _read_json(policy_path)
    if sft.get("base_model") != BASE_MODEL:
        raise ValueError("unexpected SFT base model")
    if sft.get("prompt_version") != PROMPT_VERSION:
        raise ValueError("unexpected SFT prompt version")
    if sft.get("semantic_contract_version") != SEMANTIC_CONTRACT:
        raise ValueError("unexpected SFT semantic contract")
    if sft.get("human_gold_claimed") is not False:
        raise ValueError("AI-consensus training must not be described as human gold")
    if model_report.get("predictions_sha256") != hybrid_report.get("model_predictions_sha256"):
        raise ValueError("hybrid report is not bound to the model predictions")
    if model_report.get("dataset_sha256") != hybrid_report.get("dataset_sha256"):
        raise ValueError("model and hybrid reports use different datasets")
    if hybrid_report.get("dataset_sha256") != (sft.get("output_sha256") or {}).get("validation"):
        raise ValueError("evaluation report is not bound to the SFT validation split")

    gate = hybrid_report.get("hybrid_gate") or {}
    metrics = hybrid_report.get("hybrid_metrics") or {}
    priority = metrics.get("priority_review") or {}
    frozen_metrics = {
        "rows": int(metrics.get("rows") or 0),
        "exact_payload_accuracy": float(metrics.get("exact_payload_accuracy") or 0),
        "materiality_macro_f1": float(
            ((metrics.get("materiality") or {}).get("macro_f1_truth_supported_classes")) or 0
        ),
        "polarity_macro_f1": float(
            ((metrics.get("polarity") or {}).get("macro_f1_truth_supported_classes")) or 0
        ),
        "priority_review_recall": float(priority.get("recall") or 0),
        "false_priority_rate": float(priority.get("false_priority_rate") or 1),
    }
    thresholds = policy.get("minimum_metrics") or {}
    metric_checks = {
        "rows": frozen_metrics["rows"] >= int(thresholds.get("rows") or 0),
        "exact_payload_accuracy": frozen_metrics["exact_payload_accuracy"]
        >= float(thresholds.get("exact_payload_accuracy") or 1),
        "materiality_macro_f1": frozen_metrics["materiality_macro_f1"]
        >= float(thresholds.get("materiality_macro_f1") or 1),
        "polarity_macro_f1": frozen_metrics["polarity_macro_f1"]
        >= float(thresholds.get("polarity_macro_f1") or 1),
        "priority_review_recall": frozen_metrics["priority_review_recall"]
        >= float(thresholds.get("priority_review_recall") or 1),
        "false_priority_rate": frozen_metrics["false_priority_rate"]
        <= float(thresholds.get("false_priority_rate") or 0),
    }
    if gate.get("passed") is not True or gate.get("decision") != policy.get("accepted_gate"):
        raise ValueError("hybrid evaluation gate has not passed the publication policy")
    if not all(metric_checks.values()):
        raise ValueError("hybrid evaluation metrics have not passed the publication policy")

    actual_hashes = {
        "model_sha256": _sha256_file(model_path),
        "lora_sha256": _sha256_file(lora_path),
        "adapter_sha256": _sha256_file(adapter_path),
        "sft_manifest_sha256": _sha256_file(sft_manifest_path),
        "model_report_sha256": _sha256_file(model_report_path),
        "hybrid_report_sha256": _sha256_file(hybrid_report_path),
    }
    for key, actual in actual_hashes.items():
        expected = str((policy.get("frozen_hashes") or {}).get(key) or "").casefold()
        if actual != expected:
            raise ValueError(f"publication policy hash mismatch: {key}")

    manifest = {
        "schema_version": 2,
        "contract": RUNTIME_CONTRACT,
        "base_model": BASE_MODEL,
        "model_file": model_path.name,
        "model_sha256": actual_hashes["model_sha256"],
        "lora_file": lora_path.name,
        "lora_sha256": actual_hashes["lora_sha256"],
        "adapter_sha256": actual_hashes["adapter_sha256"],
        "sft_manifest_sha256": actual_hashes["sft_manifest_sha256"],
        "model_report_sha256": actual_hashes["model_report_sha256"],
        "hybrid_report_sha256": actual_hashes["hybrid_report_sha256"],
        "publication_policy_sha256": _sha256_file(policy_path),
        "prompt_version": PROMPT_VERSION,
        "semantic_contract_version": SEMANTIC_CONTRACT,
        "hybrid_policy_version": HYBRID_POLICY_VERSION,
        "training_basis": "DUAL_REVIEW_AI_CONSENSUS",
        "evaluation": {
            "status": "PASS",
            "source_decision": gate["decision"],
            "metrics": frozen_metrics,
        },
        "production_authorization": policy["production_authorization"],
        "production_eligible": True,
        "no_trading": True,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--lora", type=Path, required=True)
    parser.add_argument("--adapter-model", type=Path, required=True)
    parser.add_argument("--sft-manifest", type=Path, required=True)
    parser.add_argument("--model-report", type=Path, required=True)
    parser.add_argument("--hybrid-report", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = build(
        args.model,
        args.lora,
        args.adapter_model,
        args.sft_manifest,
        args.model_report,
        args.hybrid_report,
        args.policy,
        args.output,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
