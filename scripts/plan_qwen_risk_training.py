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


PLAN_VERSION = "qwen-risk-ms-swift-plan-v1"
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
    validation = _verified_output(parent, outputs, "qwen_risk_sft_validation.jsonl")
    blind = _verified_output(parent, outputs, "qwen_risk_blind_manifest.jsonl")
    evidence_audit = _verified_output(
        parent, outputs, "qwen_risk_evidence_posture_audit.jsonl"
    )

    train_rows = _verify_semantic_rows(train, "TRAIN")
    validation_rows = _verify_semantic_rows(validation, "VALIDATION")
    blind_rows = len(_jsonl(blind))
    evidence_audit_rows = _verify_evidence_posture_audit(evidence_audit)
    expected_counts = {
        "train_rows": train_rows,
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
        str(train),
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
