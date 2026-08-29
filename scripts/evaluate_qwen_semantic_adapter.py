#!/usr/bin/env python3
"""Evaluate a Qwen semantic LoRA adapter without changing production state.

The evaluator consumes an explicitly-role-bound core-v1 JSONL split.  It
preflights every target before loading a model, decodes only the assistant
continuation, validates the bounded semantic contract, writes every prediction,
and reports exact/axis metrics plus an advisory risk-priority gate decision.
Only the strict checkpoint selector may select a DEV checkpoint.
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

from app.models.qwen_risk_contract import (  # noqa: E402
    expected_semantic_payload,
    validate_semantic_payload,
)
from app.models.risk_label_contract import MATERIALITY, POLARITIES  # noqa: E402


DATASET_ROLES = frozenset(
    {"DEV_SELECTION_ONLY", "SEALED_BENCHMARK_ONLY", "DIAGNOSTIC_ONLY"}
)
TARGET_CONTRACT = "core-v1"
LEGACY_MODEL_OUTPUT_CONTRACT = "core-payload-v1"
AXES_MODEL_OUTPUT_CONTRACT = "core-axes-v1"
MODEL_OUTPUT_CONTRACTS = frozenset(
    {LEGACY_MODEL_OUTPUT_CONTRACT, AXES_MODEL_OUTPUT_CONTRACT}
)
AXES_FIELDS = frozenset({"materiality", "polarity"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GENERATION_CONFIG_VERSION = "qwen-semantic-greedy-v1"
BASE_MODEL_FULL_HASH_MAX_BYTES = 8 * 1024 * 1024
BASE_MODEL_SAMPLE_BYTES = 1024 * 1024
ADAPTER_CONFIG_NAME = "adapter_config.json"
ADAPTER_WEIGHT_NAMES = ("adapter_model.safetensors", "adapter_model.bin")


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sampled_file_fingerprint(path: Path) -> dict[str, Any]:
    """Hash small files fully and sample the ends of large base-model files."""

    size = path.stat().st_size
    digest = hashlib.sha256()
    digest.update(f"size:{size}\n".encode("ascii"))
    mode = "full"
    with path.open("rb") as handle:
        if size <= BASE_MODEL_FULL_HASH_MAX_BYTES:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        else:
            mode = "head_tail"
            digest.update(handle.read(BASE_MODEL_SAMPLE_BYTES))
            handle.seek(max(0, size - BASE_MODEL_SAMPLE_BYTES))
            digest.update(handle.read(BASE_MODEL_SAMPLE_BYTES))
    return {"bytes": size, "mode": mode, "sha256": digest.hexdigest()}


def base_model_fingerprint(path: Path) -> dict[str, Any]:
    """Build a path-independent, lightweight and reproducible model fingerprint."""

    if not path.is_dir():
        raise FileNotFoundError(f"base model directory missing: {path}")
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if not files:
        raise ValueError(f"base model directory contains no files: {path}")
    entries = []
    for item in files:
        entries.append(
            {
                "path": item.relative_to(path).as_posix(),
                **_sampled_file_fingerprint(item),
            }
        )
    return {
        "scheme": "sha256-directory-manifest-full-small-head-tail-large-v1",
        "full_hash_max_bytes": BASE_MODEL_FULL_HASH_MAX_BYTES,
        "sample_bytes": BASE_MODEL_SAMPLE_BYTES,
        "sha256": hashlib.sha256(stable_json(entries).encode("utf-8")).hexdigest(),
        "file_count": len(entries),
        "total_bytes": sum(entry["bytes"] for entry in entries),
        "files": entries,
    }


def adapter_fingerprint(path: Path) -> dict[str, Any]:
    """Fully hash the PEFT configuration and adapter weight files used for loading."""

    if not path.is_dir():
        raise FileNotFoundError(f"adapter directory missing: {path}")
    config = path / ADAPTER_CONFIG_NAME
    weights = [path / name for name in ADAPTER_WEIGHT_NAMES if (path / name).is_file()]
    if not config.is_file() or not weights:
        raise ValueError(
            f"adapter must contain {ADAPTER_CONFIG_NAME} and one of "
            f"{', '.join(ADAPTER_WEIGHT_NAMES)}: {path}"
        )
    files = [config, *weights]
    entries = [
        {"path": item.name, "bytes": item.stat().st_size, "sha256": sha256_file(item)}
        for item in files
    ]
    return {
        "scheme": "sha256-peft-adapter-files-v1",
        "sha256": hashlib.sha256(stable_json(entries).encode("utf-8")).hexdigest(),
        "file_count": len(entries),
        "total_bytes": sum(entry["bytes"] for entry in entries),
        "files": entries,
    }


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


def extract_model_output(
    text: str, *, model_output_contract: str
) -> dict[str, Any] | None:
    """Parse model output according to the declared raw-output contract."""

    if model_output_contract == LEGACY_MODEL_OUTPUT_CONTRACT:
        return extract_json_object(text)
    if model_output_contract != AXES_MODEL_OUTPUT_CONTRACT:
        raise ValueError(f"unsupported model_output_contract: {model_output_contract}")
    try:
        value = json.loads(str(text or "").strip())
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def normalize_payload(value: dict[str, Any] | None) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    fields = ("materiality", "polarity", "adverse_strength", "semantic_priority")
    return {field: str(value.get(field) or "").strip().upper() for field in fields}


def normalize_model_output(
    value: Any,
    *,
    model_output_contract: str,
    allow_negative_polarity_alias: bool = False,
) -> dict[str, Any]:
    """Normalize one raw model object and derive the four-field semantic payload."""

    if model_output_contract not in MODEL_OUTPUT_CONTRACTS:
        raise ValueError(f"unsupported model_output_contract: {model_output_contract}")
    if not isinstance(allow_negative_polarity_alias, bool):
        raise ValueError("allow_negative_polarity_alias must be boolean")
    if not isinstance(value, dict):
        return {
            "normalized_model_output": None,
            "full_payload": None,
            "issues": ["payload_not_object"],
            "polarity_alias_applied": False,
        }

    alias_applied = False
    if model_output_contract == LEGACY_MODEL_OUTPUT_CONTRACT:
        normalized = normalize_payload(value)
        if normalized is not None and normalized["polarity"] == "NEGATIVE":
            if allow_negative_polarity_alias:
                normalized["polarity"] = "ADVERSE"
                alias_applied = True
        issues = validate_semantic_payload(normalized)
        if (
            normalized is not None
            and normalized["polarity"] == "NEGATIVE"
            and not allow_negative_polarity_alias
        ):
            issues = ["negative_polarity_alias_disabled", *issues]
        return {
            "normalized_model_output": normalized,
            "full_payload": normalized,
            "issues": issues,
            "polarity_alias_applied": alias_applied,
        }

    extra = sorted(set(value) - AXES_FIELDS)
    missing = sorted(AXES_FIELDS - set(value))
    issues: list[str] = []
    if extra:
        issues.append("model_output_unsupported_fields:" + ",".join(extra))
    if missing:
        issues.append("model_output_missing_fields:" + ",".join(missing))
    normalized_axes = {
        field: str(value.get(field) or "").strip().upper()
        for field in sorted(AXES_FIELDS)
    }
    if normalized_axes["polarity"] == "NEGATIVE":
        if allow_negative_polarity_alias:
            normalized_axes["polarity"] = "ADVERSE"
            alias_applied = True
        else:
            issues.append("negative_polarity_alias_disabled")
    if normalized_axes["materiality"] not in MATERIALITY:
        issues.append("invalid_materiality")
    if normalized_axes["polarity"] not in POLARITIES:
        issues.append("invalid_polarity")
    if issues:
        return {
            "normalized_model_output": normalized_axes,
            "full_payload": None,
            "issues": issues,
            "polarity_alias_applied": alias_applied,
        }
    full_payload = expected_semantic_payload(
        normalized_axes["materiality"], normalized_axes["polarity"]
    )
    full_issues = validate_semantic_payload(full_payload)
    if full_issues:
        raise ValueError(f"derived semantic payload is invalid: {full_issues}")
    return {
        "normalized_model_output": normalized_axes,
        "full_payload": full_payload,
        "issues": [],
        "polarity_alias_applied": alias_applied,
    }


def normalize_expected_payload(
    model_target: Any,
    *,
    model_output_contract: str,
    semantic_target: Any = None,
) -> tuple[dict[str, str] | None, list[str]]:
    """Validate model target and return the full four-field semantic truth."""

    if model_output_contract == AXES_MODEL_OUTPUT_CONTRACT:
        normalized_model_target = normalize_model_output(
            model_target,
            model_output_contract=model_output_contract,
            allow_negative_polarity_alias=False,
        )
        issues = [
            f"model_target:{issue}"
            for issue in normalized_model_target["issues"]
        ]
        if semantic_target is None:
            issues.append("semantic_target:missing")
            return None, issues
        semantic_issues = validate_semantic_payload(semantic_target)
        issues.extend(f"semantic_target:{issue}" for issue in semantic_issues)
        normalized_semantic_target = normalize_payload(semantic_target)
        if issues or normalized_semantic_target is None:
            return None, issues
        if normalized_semantic_target != normalized_model_target["full_payload"]:
            return None, ["semantic_target:inconsistent_with_model_target"]
        return normalized_semantic_target, []
    if model_output_contract != LEGACY_MODEL_OUTPUT_CONTRACT:
        raise ValueError(f"unsupported model_output_contract: {model_output_contract}")
    issues = validate_semantic_payload(model_target)
    return normalize_payload(model_target), issues


def _row_contract_binding(row: dict[str, Any], *, line_number: int) -> dict[str, Any]:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError(f"dataset line {line_number} metadata missing")
    target_contract = str(metadata.get("target_contract") or "").strip()
    if target_contract != TARGET_CONTRACT:
        raise ValueError(
            f"dataset line {line_number} target_contract must be {TARGET_CONTRACT}"
        )
    raw_model_output_contract = metadata.get("model_output_contract")
    model_output_contract_explicit = raw_model_output_contract is not None
    if model_output_contract_explicit and not isinstance(
        raw_model_output_contract, str
    ):
        raise ValueError(
            f"dataset line {line_number} model_output_contract must be text"
        )
    model_output_contract = (
        LEGACY_MODEL_OUTPUT_CONTRACT
        if not model_output_contract_explicit
        else str(raw_model_output_contract).strip()
    )
    if model_output_contract not in MODEL_OUTPUT_CONTRACTS:
        raise ValueError(
            f"dataset line {line_number} unsupported model_output_contract: "
            f"{model_output_contract!r}"
        )

    raw_prompt_version = metadata.get("prompt_version")
    raw_prompt_sha256 = metadata.get("prompt_sha256")
    if raw_prompt_version is not None and not isinstance(raw_prompt_version, str):
        raise ValueError(f"dataset line {line_number} prompt_version must be text")
    if raw_prompt_sha256 is not None and not isinstance(raw_prompt_sha256, str):
        raise ValueError(f"dataset line {line_number} prompt_sha256 must be text")
    prompt_version = (
        str(raw_prompt_version).strip() if raw_prompt_version is not None else None
    )
    prompt_sha256 = (
        str(raw_prompt_sha256).strip().lower()
        if raw_prompt_sha256 is not None
        else None
    )
    if (prompt_version is None) != (prompt_sha256 is None):
        raise ValueError(
            f"dataset line {line_number} prompt_version/prompt_sha256 must appear together"
        )
    if model_output_contract_explicit and (not prompt_version or not prompt_sha256):
        raise ValueError(
            f"dataset line {line_number} explicit model_output_contract requires prompt identity"
        )
    if prompt_version is not None:
        if not prompt_version:
            raise ValueError(f"dataset line {line_number} prompt_version is empty")
        if not prompt_sha256 or not SHA256_RE.fullmatch(prompt_sha256):
            raise ValueError(f"dataset line {line_number} prompt_sha256 is invalid")
        messages = row.get("messages")
        system_messages = [
            message
            for message in messages or []
            if isinstance(message, dict)
            and str(message.get("role") or "").strip().lower() == "system"
        ]
        if (
            len(system_messages) != 1
            or not messages
            or system_messages[0] is not messages[0]
            or not isinstance(system_messages[0].get("content"), str)
        ):
            raise ValueError(
                f"dataset line {line_number} prompt binding requires one leading system message"
            )
        actual_prompt_sha256 = hashlib.sha256(
            system_messages[0]["content"].encode("utf-8")
        ).hexdigest()
        if actual_prompt_sha256 != prompt_sha256:
            raise ValueError(f"dataset line {line_number} system prompt SHA256 mismatch")
    return {
        "target_contract": target_contract,
        "model_output_contract": model_output_contract,
        "model_output_contract_explicit": model_output_contract_explicit,
        "legacy_compatibility_mode": not model_output_contract_explicit,
        "prompt_version": prompt_version,
        "prompt_sha256": prompt_sha256,
        "prompt_binding_verified": prompt_version is not None,
    }


def dataset_contract_binding(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        raise ValueError("evaluation dataset is empty")
    bindings = [
        _row_contract_binding(row, line_number=index)
        for index, row in enumerate(rows, start=1)
    ]
    first = bindings[0]
    for index, binding in enumerate(bindings[1:], start=2):
        if binding != first:
            raise ValueError(f"dataset line {index} model/prompt contract mismatch")
    return first


def explicit_generation_config(tokenizer: Any, *, max_new_tokens: int) -> dict[str, Any]:
    """Resolve every generation setting whose inheritance could change decoding."""

    if isinstance(max_new_tokens, bool) or not isinstance(max_new_tokens, int):
        raise ValueError("max_new_tokens must be an integer")
    if max_new_tokens < 1:
        raise ValueError("max_new_tokens must be positive")
    eos_token_id = getattr(tokenizer, "eos_token_id", None)
    if (
        isinstance(eos_token_id, bool)
        or not isinstance(eos_token_id, int)
        or eos_token_id < 0
    ):
        raise ValueError("tokenizer eos_token_id must be a nonnegative integer")
    pad_token_id = getattr(tokenizer, "pad_token_id", None)
    if pad_token_id is None:
        pad_token_id = eos_token_id
    if (
        isinstance(pad_token_id, bool)
        or not isinstance(pad_token_id, int)
        or pad_token_id < 0
    ):
        raise ValueError("tokenizer pad_token_id must be a nonnegative integer or None")
    return {
        "max_new_tokens": max_new_tokens,
        "min_new_tokens": 0,
        "do_sample": False,
        "repetition_penalty": 1.0,
        "encoder_repetition_penalty": 1.0,
        "no_repeat_ngram_size": 0,
        "num_beams": 1,
        "num_beam_groups": 1,
        "num_return_sequences": 1,
        "use_cache": True,
        "eos_token_id": eos_token_id,
        "pad_token_id": pad_token_id,
    }


def polarity_alias_report(*, enabled: bool, applied_rows: int) -> dict[str, Any]:
    if not isinstance(enabled, bool):
        raise ValueError("polarity alias enabled flag must be boolean")
    if isinstance(applied_rows, bool) or not isinstance(applied_rows, int) or applied_rows < 0:
        raise ValueError("polarity alias applied_rows must be a nonnegative integer")
    if applied_rows and not enabled:
        raise ValueError("disabled polarity alias cannot have applied rows")
    return {
        "enabled": enabled,
        "mapping": {"NEGATIVE": "ADVERSE"},
        "applied_rows": applied_rows,
    }


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


def summarize_prediction_strata(
    rows: list[dict[str, Any]], *, field: str = "benchmark_stratum",
) -> dict[str, dict[str, Any]]:
    """Report source-frozen benchmark strata without using them for selection."""

    values = sorted({str(row.get(field) or "").strip() for row in rows} - {""})
    return {
        value: summarize_predictions([row for row in rows if str(row.get(field) or "").strip() == value])
        for value in values
    }


def gate_decision(metrics: dict[str, Any]) -> dict[str, Any]:
    false_priority_rate = metrics["priority_review"]["false_priority_rate"]
    checks = {
        "rows_ge_120": metrics["rows"] >= 120,
        "priority_support_ge_20": metrics["priority_review"]["support"] >= 20,
        "parse_success_rate_ge_1_00": metrics["parse_success_rate"] >= 1.0,
        "materiality_macro_f1_ge_0_65": metrics["materiality"]["macro_f1_truth_supported_classes"] >= 0.65,
        "polarity_macro_f1_ge_0_55": metrics["polarity"]["macro_f1_truth_supported_classes"] >= 0.55,
        "priority_review_recall_ge_0_75": (metrics["priority_review"]["recall"] or 0.0) >= 0.75,
        "false_priority_rate_le_0_10": false_priority_rate is not None and false_priority_rate <= 0.10,
    }
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "decision": "QUALIFIED_SHADOW_SEMANTIC_CANDIDATE" if all(checks.values()) else "NOT_QUALIFIED",
    }


def load_evaluation_dataset(
    path: Path, *, dataset_role: str
) -> list[dict[str, Any]]:
    """Validate the complete evaluation dataset before model or GPU initialization."""

    if dataset_role not in DATASET_ROLES:
        raise ValueError(f"unsupported dataset role: {dataset_role}")
    if not path.is_file():
        raise FileNotFoundError(path)
    rows: list[dict[str, Any]] = []
    seen_sample_ids: set[str] = set()
    common_binding: dict[str, Any] | None = None
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"dataset line {line_number} is not valid JSON") from exc
        if not isinstance(row, dict):
            raise ValueError(f"dataset line {line_number} is not an object")

        messages = row.get("messages")
        if not isinstance(messages, list) or len(messages) < 2:
            raise ValueError(f"dataset line {line_number} has invalid messages")
        for message_index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise ValueError(
                    f"dataset line {line_number} message {message_index} is not an object"
                )
            role = str(message.get("role") or "").strip().lower()
            if role not in {"system", "user", "assistant"}:
                raise ValueError(
                    f"dataset line {line_number} message {message_index} has invalid role"
                )
            if not isinstance(message.get("content"), str):
                raise ValueError(
                    f"dataset line {line_number} message {message_index} content is not text"
                )
        if str(messages[-1].get("role") or "").strip().lower() != "assistant":
            raise ValueError(f"dataset line {line_number} final message is not assistant")

        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError(f"dataset line {line_number} metadata missing")
        sample_id = str(metadata.get("sample_id") or "").strip()
        if not sample_id or sample_id in seen_sample_ids:
            raise ValueError(
                f"dataset line {line_number} has duplicate or missing sample_id"
            )
        seen_sample_ids.add(sample_id)
        target_contract = str(metadata.get("target_contract") or "").strip()
        if target_contract != TARGET_CONTRACT:
            raise ValueError(
                f"dataset line {line_number} target_contract must be {TARGET_CONTRACT}"
            )
        if dataset_role == "DEV_SELECTION_ONLY" and (
            str(metadata.get("split") or "").strip().upper() != "DEV"
        ):
            raise ValueError(
                f"dataset line {line_number} DEV_SELECTION_ONLY requires metadata.split=DEV"
            )
        binding = _row_contract_binding(row, line_number=line_number)
        if common_binding is None:
            common_binding = binding
        elif binding != common_binding:
            raise ValueError(f"dataset line {line_number} model/prompt contract mismatch")

        try:
            model_target = json.loads(messages[-1]["content"])
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"dataset line {line_number} assistant target is not valid JSON"
            ) from exc
        expected, target_issues = normalize_expected_payload(
            model_target,
            model_output_contract=binding["model_output_contract"],
            semantic_target=metadata.get("semantic_target"),
        )
        if target_issues or expected is None:
            raise ValueError(
                f"dataset line {line_number} target contract invalid: {target_issues}"
            )
        rows.append(row)
    if not rows:
        raise ValueError("evaluation dataset is empty")
    return rows


def run_inference(
    *,
    base_model: Path,
    adapter: Path,
    dataset: Path,
    dataset_role: str,
    output_dir: Path,
    max_new_tokens: int,
    allow_negative_polarity_alias: bool = False,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    if not isinstance(allow_negative_polarity_alias, bool):
        raise ValueError("allow_negative_polarity_alias must be boolean")
    if isinstance(max_new_tokens, bool) or not isinstance(max_new_tokens, int):
        raise ValueError("max_new_tokens must be an integer")
    if max_new_tokens < 1:
        raise ValueError("max_new_tokens must be positive")
    dataset_rows = load_evaluation_dataset(dataset, dataset_role=dataset_role)
    contract_binding = dataset_contract_binding(dataset_rows)
    model_fingerprint = base_model_fingerprint(base_model)
    peft_fingerprint = adapter_fingerprint(adapter)

    import torch
    from peft import PeftModel
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
        GenerationConfig,
    )

    tokenizer = AutoTokenizer.from_pretrained(base_model, local_files_only=True)
    generation_overrides = explicit_generation_config(
        tokenizer, max_new_tokens=max_new_tokens
    )
    safe_generation_config = GenerationConfig(**generation_overrides)
    resolved_generation_config = safe_generation_config.to_dict()
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
    alias_applied_rows = 0
    for index, row in enumerate(dataset_rows, start=1):
        messages = row["messages"][:-1]
        model_target = json.loads(row["messages"][-1]["content"])
        expected, expected_issues = normalize_expected_payload(
            model_target,
            model_output_contract=contract_binding["model_output_contract"],
            semantic_target=row["metadata"].get("semantic_target"),
        )
        if expected_issues or expected is None:
            raise ValueError(
                f"dataset row changed after preflight: {row['metadata']['sample_id']}"
            )
        normalized_expected_model_output = normalize_model_output(
            model_target,
            model_output_contract=contract_binding["model_output_contract"],
            allow_negative_polarity_alias=False,
        )
        if normalized_expected_model_output["issues"]:
            raise ValueError(
                f"dataset model target changed after preflight: "
                f"{row['metadata']['sample_id']}"
            )
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        encoded = tokenizer(prompt, return_tensors="pt").to(model.device)
        with torch.inference_mode():
            generated = model.generate(
                **encoded,
                generation_config=safe_generation_config,
            )
        continuation = generated[0, encoded["input_ids"].shape[1] :]
        raw_output = tokenizer.decode(continuation, skip_special_tokens=True).strip()
        parsed_model_output = extract_model_output(
            raw_output,
            model_output_contract=contract_binding["model_output_contract"],
        )
        normalized_output = normalize_model_output(
            parsed_model_output,
            model_output_contract=contract_binding["model_output_contract"],
            allow_negative_polarity_alias=allow_negative_polarity_alias,
        )
        predicted = normalized_output["full_payload"]
        issues = normalized_output["issues"]
        contract_valid = not issues
        exact_match = bool(contract_valid and predicted == expected)
        alias_applied = bool(normalized_output["polarity_alias_applied"])
        alias_applied_rows += int(alias_applied)
        predictions.append(
            {
                "index": index,
                "sample_id": row["metadata"]["sample_id"],
                "event_id": row["metadata"].get("event_id"),
                "benchmark_stratum": row["metadata"].get("benchmark_stratum"),
                "expected": expected,
                "expected_model_output": normalized_expected_model_output[
                    "normalized_model_output"
                ],
                "model_output_contract": contract_binding["model_output_contract"],
                "parsed_model_output": parsed_model_output,
                "normalized_model_output": normalized_output[
                    "normalized_model_output"
                ],
                "predicted": predicted,
                "raw_output": raw_output,
                "polarity_alias_applied": alias_applied,
                "contract_issues": issues,
                "contract_valid": contract_valid,
                "exact_match": exact_match,
            }
        )
        print(f"{index}/{len(predictions) if False else '?'} {row['metadata']['sample_id']} valid={contract_valid} exact={exact_match}", flush=True)

    output_dir.mkdir(parents=True)
    prediction_path = output_dir / "predictions.jsonl"
    prediction_path.write_text(
        "".join(stable_json(row) + "\n" for row in predictions), encoding="utf-8"
    )
    metrics = summarize_predictions(predictions)
    metrics_by_benchmark_stratum = summarize_prediction_strata(predictions)
    report = {
        "schema_version": 2,
        "evaluation_only": True,
        "production_model_changed": False,
        "human_gold_claimed": False,
        "dataset_role": dataset_role,
        "reserved_test_only": dataset_role == "SEALED_BENCHMARK_ONLY",
        "target_contract": contract_binding["target_contract"],
        "model_output_contract": contract_binding["model_output_contract"],
        "model_output_contract_explicit": contract_binding[
            "model_output_contract_explicit"
        ],
        "legacy_compatibility_mode": contract_binding[
            "legacy_compatibility_mode"
        ],
        "prompt_version": contract_binding["prompt_version"],
        "prompt_sha256": contract_binding["prompt_sha256"],
        "prompt_binding_verified": contract_binding["prompt_binding_verified"],
        "dataset_path": str(dataset),
        "dataset_sha256": sha256_file(dataset),
        "base_model": str(base_model),
        "base_model_fingerprint": model_fingerprint,
        "adapter": str(adapter),
        "adapter_fingerprint": peft_fingerprint,
        "max_new_tokens": max_new_tokens,
        "generation_config_version": GENERATION_CONFIG_VERSION,
        "generation_config_inherits_base_model": False,
        "generation_config": resolved_generation_config,
        "polarity_alias": polarity_alias_report(
            enabled=allow_negative_polarity_alias,
            applied_rows=alias_applied_rows,
        ),
        "metrics": metrics,
        "metrics_by_benchmark_stratum": metrics_by_benchmark_stratum,
        "gate": gate_decision(metrics),
        "evaluator_gate_advisory_only": True,
        "checkpoint_selection_authority": (
            "summarize_qwen_v4_checkpoint_evaluations.py strict selector gate"
        ),
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
    parser.add_argument(
        "--dataset-role", choices=sorted(DATASET_ROLES), required=True
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--max-new-tokens", type=int, default=96)
    parser.add_argument(
        "--allow-negative-polarity-alias",
        action="store_true",
        help="map model polarity NEGATIVE to ADVERSE and record every use",
    )
    args = parser.parse_args()
    report = run_inference(
        base_model=args.base_model.resolve(),
        adapter=args.adapter.resolve(),
        dataset=args.dataset.resolve(),
        dataset_role=args.dataset_role,
        output_dir=args.output_dir.resolve(),
        max_new_tokens=args.max_new_tokens,
        allow_negative_polarity_alias=args.allow_negative_polarity_alias,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["gate"]["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
