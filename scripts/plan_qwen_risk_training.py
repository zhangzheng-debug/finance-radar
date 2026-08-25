#!/usr/bin/env python3
"""Verify a frozen Qwen SFT export and emit an executable ms-swift plan.

The default mode is read-only.  Training is allowed only when the final human
gold export, its content hashes, blind holdout boundary, and semantic contract
all pass the same preflight.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.models.qwen_risk_contract import validate_semantic_payload


PLAN_VERSION = "qwen-risk-ms-swift-plan-v2"
BASE_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
MIN_TRAIN_ROWS = 160
MIN_VALIDATION_ROWS = 40


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _jsonl(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path.name}:{number} is not an object")
        result.append(value)
    return result


def _verified_output(manifest_dir: Path, outputs: dict[str, Any], name: str) -> Path:
    expected = str(outputs.get(name) or "").lower()
    if len(expected) != 64:
        raise ValueError(f"missing output digest for {name}")
    path = (manifest_dir / name).resolve()
    if not path.is_file() or _sha256(path) != expected:
        raise ValueError(f"output digest mismatch for {name}")
    return path


def _verify_semantic_rows(path: Path, expected_split: str) -> int:
    rows = _jsonl(path)
    seen: set[str] = set()
    for number, row in enumerate(rows, 1):
        messages = row.get("messages")
        metadata = row.get("metadata")
        if not isinstance(messages, list) or [item.get("role") for item in messages] != [
            "system",
            "user",
            "assistant",
        ]:
            raise ValueError(f"{path.name}:{number} has invalid messages")
        if not isinstance(metadata, dict) or metadata.get("split") != expected_split:
            raise ValueError(f"{path.name}:{number} has invalid split")
        if metadata.get("evidence_state_used_as_model_target") is not False:
            raise ValueError(f"{path.name}:{number} leaks evidence state into model target")
        if metadata.get("post_event_market_data_included") is not False:
            raise ValueError(f"{path.name}:{number} leaks post-event market data")
        if metadata.get("model_output_included_in_review") is not False:
            raise ValueError(f"{path.name}:{number} leaks model output")
        sample_id = str(metadata.get("sample_id") or "")
        if not sample_id or sample_id in seen:
            raise ValueError(f"{path.name}:{number} has duplicate or missing sample_id")
        seen.add(sample_id)
        payload = json.loads(str(messages[-1].get("content") or ""))
        issues = validate_semantic_payload(payload)
        if issues:
            raise ValueError(f"{path.name}:{number} invalid target: {','.join(issues)}")
    return len(rows)


def _verify_balanced_train(
    path: Path,
    unique_train_path: Path,
    policy: dict[str, Any],
) -> dict[str, Any]:
    unique_rows = _jsonl(unique_train_path)
    balanced_rows = _jsonl(path)
    originals = {
        str(row["metadata"]["sample_id"]): row
        for row in unique_rows
        if isinstance(row.get("metadata"), dict)
    }
    if len(originals) != len(unique_rows):
        raise ValueError("unique TRAIN rows are not uniquely keyed")

    instance_ids: set[str] = set()
    occurrences: dict[str, list[int]] = {sample_id: [] for sample_id in originals}
    priority_unique = 0
    priority_effective = 0
    max_occurrences = int(policy.get("max_occurrences_per_sample") or 0)
    if max_occurrences < 1:
        raise ValueError("invalid TRAIN resampling occurrence cap")

    for number, row in enumerate(balanced_rows, 1):
        metadata = row.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("split") != "TRAIN":
            raise ValueError(f"{path.name}:{number} has invalid TRAIN metadata")
        origin_id = str(metadata.get("origin_sample_id") or "")
        instance_id = str(metadata.get("training_instance_id") or "")
        repeat_index = metadata.get("oversample_repeat_index")
        if origin_id not in originals or not instance_id or instance_id in instance_ids:
            raise ValueError(f"{path.name}:{number} has invalid resampling identity")
        if not isinstance(repeat_index, int) or not 0 <= repeat_index < max_occurrences:
            raise ValueError(f"{path.name}:{number} has invalid repeat index")
        instance_ids.add(instance_id)
        occurrences[origin_id].append(repeat_index)

        original = originals[origin_id]
        if row.get("messages") != original.get("messages"):
            raise ValueError(f"{path.name}:{number} changes the original semantic example")
        original_metadata = original["metadata"]
        for field, value in original_metadata.items():
            if metadata.get(field) != value:
                raise ValueError(f"{path.name}:{number} changes original metadata:{field}")
        payload = json.loads(str(row["messages"][-1].get("content") or ""))
        issues = validate_semantic_payload(payload)
        if issues:
            raise ValueError(f"{path.name}:{number} invalid target: {','.join(issues)}")
        priority = payload.get("semantic_priority") == "PRIORITY_REVIEW"
        priority_effective += int(priority)
        if repeat_index > 0:
            if metadata.get("oversampled") is not True or not priority:
                raise ValueError(f"{path.name}:{number} repeats a non-priority example")
        elif metadata.get("oversampled") is not False or instance_id != origin_id:
            raise ValueError(f"{path.name}:{number} has invalid base occurrence")

    for sample_id, indexes in occurrences.items():
        if sorted(indexes) != list(range(len(indexes))):
            raise ValueError(f"TRAIN resampling indexes are incomplete for {sample_id}")
        if not indexes:
            raise ValueError(f"TRAIN resampling omitted original sample {sample_id}")
        payload = json.loads(str(originals[sample_id]["messages"][-1]["content"]))
        priority_unique += int(payload.get("semantic_priority") == "PRIORITY_REVIEW")

    effective_rows = len(balanced_rows)
    achieved = priority_effective / effective_rows if effective_rows else 0.0
    actual = {
        "unique_train_rows": len(unique_rows),
        "unique_priority_review_rows": priority_unique,
        "effective_train_rows": effective_rows,
        "effective_priority_review_rows": priority_effective,
        "oversampled_rows": effective_rows - len(unique_rows),
        "achieved_priority_fraction": achieved,
    }
    for field, value in actual.items():
        expected = policy.get(field)
        if field == "achieved_priority_fraction":
            if abs(float(expected) - float(value)) > 1e-12:
                raise ValueError(f"TRAIN resampling manifest mismatch: {field}")
        elif int(expected) != int(value):
            raise ValueError(f"TRAIN resampling manifest mismatch: {field}")
    if policy.get("policy") != "TRAIN_ONLY_PRIORITY_REVIEW_CAPPED_REPEAT_V1":
        raise ValueError("unsupported TRAIN resampling policy")
    if policy.get("selection_uses_human_semantic_target") is not True:
        raise ValueError("TRAIN resampling selection basis missing")
    if policy.get("validation_resampled") is not False or policy.get("human_blind_resampled") is not False:
        raise ValueError("evaluation sets may not be resampled")
    if policy.get("target_met") is not True or achieved < float(policy.get("target_priority_fraction")):
        raise ValueError("TRAIN priority-review resampling target was not met")
    return actual


def _verify_evidence_posture_audit(path: Path) -> int:
    rows = _jsonl(path)
    seen: set[str] = set()
    for number, row in enumerate(rows, 1):
        sample_id = str(row.get("sample_id") or "")
        if not sample_id or sample_id in seen:
            raise ValueError(f"{path.name}:{number} has duplicate or missing sample_id")
        seen.add(sample_id)
        if row.get("split") not in {"TRAIN", "VALIDATION"}:
            raise ValueError(f"{path.name}:{number} has invalid split")
        if row.get("qwen_training_included") is not True:
            raise ValueError(f"{path.name}:{number} unexpectedly excludes semantic text")
        if row.get("evidence_state_exposed_to_model") is not False:
            raise ValueError(f"{path.name}:{number} exposes evidence posture to Qwen")
    return len(rows)


def build_plan(
    manifest_path: Path,
    output_dir: Path,
    *,
    min_train_rows: int = MIN_TRAIN_ROWS,
    min_validation_rows: int = MIN_VALIDATION_ROWS,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("base_model") != BASE_MODEL:
        raise ValueError("unexpected base model")
    required_false = (
        "evidence_state_used_as_model_target",
        "evidence_state_exposed_to_model",
        "human_blind_labels_exported",
        "human_blind_content_exported",
        "deepseek_output_included",
        "post_event_market_data_included",
        "production_model_changed",
    )
    for field in required_false:
        if manifest.get(field) is not False:
            raise ValueError(f"unsafe manifest flag: {field}")
    if manifest.get("no_trading") is not True:
        raise ValueError("no_trading boundary missing")

    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict):
        raise ValueError("manifest outputs missing")
    parent = manifest_path.parent
    train = _verified_output(parent, outputs, "qwen_risk_sft_train.jsonl")
    balanced_train = _verified_output(
        parent, outputs, "qwen_risk_sft_train_balanced.jsonl"
    )
    validation = _verified_output(parent, outputs, "qwen_risk_sft_validation.jsonl")
    blind = _verified_output(parent, outputs, "qwen_risk_blind_manifest.jsonl")
    evidence_audit = _verified_output(
        parent, outputs, "qwen_risk_evidence_posture_audit.jsonl"
    )

    train_rows = _verify_semantic_rows(train, "TRAIN")
    resampling_policy = manifest.get("train_priority_resampling")
    if not isinstance(resampling_policy, dict):
        raise ValueError("TRAIN resampling policy missing")
    resampling = _verify_balanced_train(balanced_train, train, resampling_policy)
    validation_rows = _verify_semantic_rows(validation, "VALIDATION")
    blind_rows = len(_jsonl(blind))
    evidence_audit_rows = _verify_evidence_posture_audit(evidence_audit)
    expected_counts = {
        "train_rows": train_rows,
        "train_effective_rows": resampling["effective_train_rows"],
        "train_oversampled_rows": resampling["oversampled_rows"],
        "validation_rows": validation_rows,
        "human_blind_rows": blind_rows,
        "evidence_posture_audit_rows": evidence_audit_rows,
    }
    for field, actual in expected_counts.items():
        if manifest.get(field) != actual:
            raise ValueError(f"manifest count mismatch: {field}")
    if train_rows < min_train_rows:
        raise ValueError(f"insufficient training rows: {train_rows} < {min_train_rows}")
    if validation_rows < min_validation_rows:
        raise ValueError(
            f"insufficient validation rows: {validation_rows} < {min_validation_rows}"
        )
    if blind_rows <= 0:
        raise ValueError("sealed human blind holdout is required")
    if evidence_audit_rows != train_rows + validation_rows:
        raise ValueError("evidence posture audit does not cover every development row")

    output_dir = output_dir.resolve()
    command = [
        "swift",
        "sft",
        "--model",
        BASE_MODEL,
        "--dataset",
        str(balanced_train),
        "--val_dataset",
        str(validation),
        "--split_dataset_ratio",
        "0",
        "--train_type",
        "lora",
        "--quant_method",
        "bnb",
        "--quant_bits",
        "4",
        "--bnb_4bit_quant_type",
        "nf4",
        "--bnb_4bit_use_double_quant",
        "true",
        "--lora_rank",
        "8",
        "--lora_alpha",
        "32",
        "--target_modules",
        "all-linear",
        "--torch_dtype",
        "float16",
        "--num_train_epochs",
        "3",
        "--per_device_train_batch_size",
        "1",
        "--per_device_eval_batch_size",
        "1",
        "--gradient_accumulation_steps",
        "16",
        "--learning_rate",
        "0.0001",
        "--max_length",
        "2048",
        "--eval_steps",
        "20",
        "--save_steps",
        "20",
        "--save_total_limit",
        "2",
        "--logging_steps",
        "5",
        "--warmup_ratio",
        "0.05",
        "--dataloader_num_workers",
        "0",
        "--strict",
        "true",
        "--output_dir",
        str(output_dir),
    ]
    return {
        "schema_version": 1,
        "plan_version": PLAN_VERSION,
        "ready": True,
        "base_model": BASE_MODEL,
        "dataset_manifest": str(manifest_path),
        "dataset_manifest_sha256": _sha256(manifest_path),
        "train_rows": train_rows,
        "train_effective_rows": resampling["effective_train_rows"],
        "train_oversampled_rows": resampling["oversampled_rows"],
        "train_priority_resampling": resampling_policy,
        "validation_rows": validation_rows,
        "sealed_blind_rows": blind_rows,
        "evidence_posture_audit_rows": evidence_audit_rows,
        "hardware_profile": "single_nvidia_8gb_qlora_nf4",
        "command": command,
        "execution_requires_explicit_execute": True,
        "production_model_changed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--plan-out", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    plan = build_plan(args.manifest, args.output_dir)
    if args.plan_out:
        args.plan_out.resolve().write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    if not args.execute:
        return 0
    if not shutil.which("swift"):
        raise SystemExit("ms-swift executable not found; preflight passed but training did not start")
    return subprocess.run(plan["command"], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
