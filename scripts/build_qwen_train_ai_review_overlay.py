#!/usr/bin/env python3
"""Build unique and trainable TRAIN overlays from independent AI reviews.

The 729-row unique overlay is an audit artifact and retains every resolved
review pair.  The trainable overlay retains every valid pair, including
UNCLEAR values, and applies a caller-selected, versioned pair multiplier
policy without changing any label.  The v13 TRAIN-only curriculum keeps every
reviewer-C arbitration at one copy and weights only clean reviewer-A/B pair
consensus.  Reviewer A/B agreement is on the complete pair; reviewer C must
cover exactly the A/B disagreement set.

This command accepts no model prediction, DEV metric, market result or sealed
benchmark input.  It publishes a new directory atomically and refuses to
overwrite an existing directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.qwen_risk_contract import (  # noqa: E402
    expected_semantic_payload,
    validate_semantic_payload,
)
from app.models.risk_label_contract import MATERIALITY, POLARITIES  # noqa: E402
from app.models.qwen_weak_supervision_contract import (  # noqa: E402
    QWEN_WEAK_MODEL_OUTPUT_CONTRACT,
    QWEN_WEAK_PROMPT_SHA256,
    QWEN_WEAK_PROMPT_VERSION,
    QWEN_WEAK_SUPERVISION_VERSION,
    QWEN_WEAK_SYSTEM_PROMPT,
)
from scripts import build_qwen_dev_ai_review_overlay as shared  # noqa: E402


CONTRACT_VERSION = "qwen-core-train-independent-ai-review-overlay-v1"
PAIR_MULTIPLIER_CONTRACT_VERSION = "qwen-core-pair-multipliers-v1"
RESOLUTION_MULTIPLIER_CONTRACT_VERSION = (
    "qwen-core-resolution-aware-pair-multipliers-v1"
)
V13_CURRICULUM_PRESET = "v13-train-only-ai-review-curriculum-v1"
V13_CURRICULUM_VERSION = "qwen-semantic-core-v13-train-only-curriculum-v1"
V13_POLICY_DESIGN_PROVENANCE = "ADAPTIVE_DEV_INFORMED"
NEUTRAL_POLICY_DESIGN_PROVENANCE = "STATIC_NEUTRAL_PRESET"
EXPLICIT_POLICY_DESIGN_PROVENANCE = "CALLER_SUPPLIED_DERIVATION_NOT_ATTESTED"
BUILDER_RUNTIME_INPUT_ISOLATION = {
    "dev_metrics_read": False,
    "qwen_predictions_read": False,
    "market_results_read": False,
    "sealed_benchmark_read": False,
}
NUMERIC_TABLE_EXCLUSION_REASON = "NUMERIC_TABLE_DOMINATED_SOURCE"
NUMERIC_TABLE_MIN_STABLE_JSON_CHARS = 1800
NUMERIC_TABLE_MIN_DIGIT_RATIO = 0.40
QUALITY_EXCLUSIONS_CONTRACT_V1 = "qwen-train-quality-exclusions-v1"
QUALITY_EXCLUSIONS_CONTRACT_V2 = "qwen-train-quality-exclusions-v2"
# The unqualified aliases describe the current write contract.  V1 remains a
# read-compatible historical contract and must never acquire new reason codes.
QUALITY_EXCLUSIONS_CONTRACT_VERSION = QUALITY_EXCLUSIONS_CONTRACT_V2
QUALITY_EXCLUSION_REASON_CODES_V1 = frozenset({"SOURCE_FIELD_CONFLICT"})
QUALITY_EXCLUSION_REASON_CODES_V2 = frozenset(
    {
        "SEQUENCE_LENGTH_HARDWARE_EXCLUSION",
        "SOURCE_FIELD_CONFLICT",
    }
)
QUALITY_EXCLUSION_REASON_CODES = QUALITY_EXCLUSION_REASON_CODES_V2
QUALITY_EXCLUSION_REASON_CODES_BY_CONTRACT = {
    QUALITY_EXCLUSIONS_CONTRACT_V1: QUALITY_EXCLUSION_REASON_CODES_V1,
    QUALITY_EXCLUSIONS_CONTRACT_V2: QUALITY_EXCLUSION_REASON_CODES_V2,
}
SEQUENCE_LENGTH_HARDWARE_EXCLUSION = "SEQUENCE_LENGTH_HARDWARE_EXCLUSION"
SEQUENCE_LENGTH_HARDWARE_PLAN_CONTRACT = (
    "qwen-sequence-length-hardware-plan-v1"
)
TRAIN_MEMBERSHIP_COMMITMENT_CONTRACT = "qwen-train-membership-commitment-v2"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
TARGET_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
SEQUENCE_LENGTH_EVIDENCE_FIELDS = frozenset(
    {
        "measured_full_tokens",
        "max_length",
        "source_unique_row_sha256",
        "unique_dataset_sha256",
        "base_model_weights_sha256",
        "tokenizer_bundle_sha256",
        "chat_template_sha256",
        "measurement_tool_version",
        "target_modules",
        "hardware_plan",
        "hardware_plan_sha256",
        "token_audit_receipt_sha256",
    }
)
TARGET_CONTRACT = "core-v1"
MODEL_OUTPUT_CONTRACT = QWEN_WEAK_MODEL_OUTPUT_CONTRACT
LABEL_PROVENANCE = "INDEPENDENT_AI_REVIEW_CONSENSUS"
LABEL_CLASSIFICATION = "AI_REVIEW_NOT_HUMAN_GOLD"
EXPECTED_UNIQUE_ROW_COUNT = 729
UNIQUE_OUTPUT_NAME = "qwen_core_v11_train_ai_review_unique_overlay.jsonl"
TRAINABLE_OUTPUT_NAME = (
    "qwen_core_v11_train_ai_review_trainable_balanced_overlay.jsonl"
)
MANIFEST_NAME = "manifest.json"
MAX_PAIR_MULTIPLIER = 100
POLICY_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
TRAINABLE_MATERIALITY = frozenset(MATERIALITY)
TRAINABLE_POLARITY = frozenset(POLARITIES)


def stable_json(value: Any) -> str:
    return shared.stable_json(value)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _pair_key(materiality: str, polarity: str) -> str:
    return f"{materiality}|{polarity}"


def _trainable_pair_keys() -> tuple[str, ...]:
    keys: list[str] = []
    for materiality in sorted(TRAINABLE_MATERIALITY):
        for polarity in sorted(TRAINABLE_POLARITY):
            payload = expected_semantic_payload(materiality, polarity)
            if not validate_semantic_payload(payload):
                keys.append(_pair_key(materiality, polarity))
    return tuple(keys)


TRAINABLE_PAIR_KEYS = _trainable_pair_keys()
V13_CONSENSUS_PAIR_MULTIPLIERS = {
    **{key: 1 for key in TRAINABLE_PAIR_KEYS},
    "MATERIAL_ADVERSE|ADVERSE": 2,
    "NOT_MATERIAL_ADVERSE|NEUTRAL": 2,
    "NOT_MATERIAL_ADVERSE|POSITIVE": 1,
    "NOT_MATERIAL_ADVERSE|MIXED": 2,
    "NOT_MATERIAL_ADVERSE|ADVERSE": 3,
    "UNCLEAR|UNCLEAR": 2,
}
PAIR_MULTIPLIER_PRESETS = {
    "neutral-1x-v1": {
        "contract_version": PAIR_MULTIPLIER_CONTRACT_VERSION,
        "policy_version": "neutral-1x-v1",
        "multipliers": {key: 1 for key in TRAINABLE_PAIR_KEYS},
    },
    V13_CURRICULUM_PRESET: {
        "contract_version": PAIR_MULTIPLIER_CONTRACT_VERSION,
        "policy_version": V13_CURRICULUM_PRESET,
        "multipliers": V13_CONSENSUS_PAIR_MULTIPLIERS,
    },
}


def _resolution_multiplier_policy(*, preset: str | None) -> dict[str, Any]:
    if preset == V13_CURRICULUM_PRESET:
        return {
            "contract_version": RESOLUTION_MULTIPLIER_CONTRACT_VERSION,
            "curriculum_version": V13_CURRICULUM_VERSION,
            "split_scope": "TRAIN_ONLY",
            "a_b_consensus_multiplier_source": "JOINT_PAIR_POLICY",
            "c_arbitration_fixed_multiplier": 1,
        }
    return {
        "contract_version": RESOLUTION_MULTIPLIER_CONTRACT_VERSION,
        "curriculum_version": None,
        "split_scope": "TRAIN_ONLY",
        "a_b_consensus_multiplier_source": "JOINT_PAIR_POLICY",
        "c_arbitration_fixed_multiplier": None,
    }


def _policy_design_provenance(*, source: str, preset: str | None) -> str:
    if preset == V13_CURRICULUM_PRESET:
        return V13_POLICY_DESIGN_PROVENANCE
    if preset == "neutral-1x-v1":
        return NEUTRAL_POLICY_DESIGN_PROVENANCE
    if source == "EXPLICIT_JSON_FILE":
        return EXPLICIT_POLICY_DESIGN_PROVENANCE
    raise ValueError("pair multiplier policy design provenance is undefined")


def _source_structure_metrics(content: dict[str, Any]) -> dict[str, Any]:
    raw = stable_json(content)
    digit_count = sum(character.isdigit() for character in raw)
    character_count = len(raw)
    digit_ratio = digit_count / character_count if character_count else 0.0
    return {
        "stable_json_character_count": character_count,
        "digit_character_count": digit_count,
        "digit_character_ratio": digit_ratio,
        "numeric_table_dominated": (
            character_count >= NUMERIC_TABLE_MIN_STABLE_JSON_CHARS
            and digit_ratio >= NUMERIC_TABLE_MIN_DIGIT_RATIO
        ),
    }


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"quality exclusions duplicate JSON key: {key}")
        value[key] = item
    return value


def _sample_ids_sha256(sample_ids: set[str] | list[str]) -> str:
    return sha256_bytes(stable_json(sorted(sample_ids)).encode("utf-8"))


def _membership_binding(sample_ids: set[str]) -> dict[str, Any]:
    return {
        "count": len(sample_ids),
        "sample_ids_sha256": _sample_ids_sha256(sample_ids),
    }


def _strict_sha256(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} must be a lowercase SHA256")
    return value


def _strict_positive_int(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _strict_target_modules(value: Any, *, label: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(
            not isinstance(module, str) or not TARGET_MODULE_RE.fullmatch(module)
            for module in value
        )
        or value != sorted(set(value))
    ):
        raise ValueError(f"{label} must be a sorted unique module list")
    return list(value)


def _validate_hardware_plan(
    value: Any,
    *,
    max_length: int,
    target_modules: list[str],
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "contract_version",
        "quantization",
        "lora",
        "training",
    }:
        raise ValueError(f"{label} schema is invalid")
    if value.get("contract_version") != SEQUENCE_LENGTH_HARDWARE_PLAN_CONTRACT:
        raise ValueError(f"{label} contract_version mismatch")

    quantization = value.get("quantization")
    if not isinstance(quantization, dict) or set(quantization) != {
        "load_in_4bit",
        "bnb_4bit_quant_type",
        "bnb_4bit_use_double_quant",
        "bnb_4bit_compute_dtype",
        "bnb_4bit_quant_storage",
    }:
        raise ValueError(f"{label} quantization schema is invalid")
    if (
        quantization.get("load_in_4bit") is not True
        or quantization.get("bnb_4bit_quant_type") != "nf4"
        or quantization.get("bnb_4bit_use_double_quant") is not True
        or quantization.get("bnb_4bit_compute_dtype")
        not in {"float16", "bfloat16", "float32"}
        or quantization.get("bnb_4bit_quant_storage") != "uint8"
    ):
        raise ValueError(f"{label} quantization values are invalid")

    lora = value.get("lora")
    if not isinstance(lora, dict) or set(lora) != {
        "r",
        "lora_alpha",
        "lora_dropout",
        "target_modules",
    }:
        raise ValueError(f"{label} LoRA schema is invalid")
    _strict_positive_int(lora.get("r"), label=f"{label} LoRA rank")
    _strict_positive_int(lora.get("lora_alpha"), label=f"{label} LoRA alpha")
    dropout = lora.get("lora_dropout")
    if (
        isinstance(dropout, bool)
        or not isinstance(dropout, (int, float))
        or not 0 <= float(dropout) < 1
    ):
        raise ValueError(f"{label} LoRA dropout is invalid")
    plan_targets = _strict_target_modules(
        lora.get("target_modules"), label=f"{label} LoRA target_modules"
    )
    if plan_targets != target_modules:
        raise ValueError(f"{label} target_modules mismatch")

    training = value.get("training")
    if not isinstance(training, dict) or set(training) != {
        "per_device_train_batch_size",
        "gradient_accumulation_steps",
        "max_length",
        "optimizer",
        "gradient_checkpointing",
    }:
        raise ValueError(f"{label} training schema is invalid")
    _strict_positive_int(
        training.get("per_device_train_batch_size"),
        label=f"{label} per-device batch size",
    )
    _strict_positive_int(
        training.get("gradient_accumulation_steps"),
        label=f"{label} gradient accumulation",
    )
    if training.get("max_length") != max_length:
        raise ValueError(f"{label} max_length mismatch")
    if training.get("optimizer") not in {"adamw_torch_fused", "paged_adamw_8bit"}:
        raise ValueError(f"{label} optimizer is invalid")
    if training.get("gradient_checkpointing") is not True:
        raise ValueError(f"{label} gradient checkpointing must be enabled")
    return value


def _validate_sequence_length_evidence(
    value: Any,
    *,
    sample_id: str,
    reason_code: str,
    expected_source_row_sha256: str,
    expected_unique_dataset_sha256: str,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != SEQUENCE_LENGTH_EVIDENCE_FIELDS:
        raise ValueError(f"{label} schema is invalid")
    measured = _strict_positive_int(
        value.get("measured_full_tokens"), label=f"{label} measured_full_tokens"
    )
    max_length = _strict_positive_int(
        value.get("max_length"), label=f"{label} max_length"
    )
    if measured <= max_length:
        raise ValueError(f"{label} does not exceed max_length")
    source_row_sha = _strict_sha256(
        value.get("source_unique_row_sha256"),
        label=f"{label} source_unique_row_sha256",
    )
    if source_row_sha != expected_source_row_sha256:
        raise ValueError(f"{label} source unique row binding mismatch")
    unique_dataset_sha = _strict_sha256(
        value.get("unique_dataset_sha256"),
        label=f"{label} unique_dataset_sha256",
    )
    if unique_dataset_sha != expected_unique_dataset_sha256:
        raise ValueError(f"{label} unique dataset binding mismatch")
    for field in (
        "base_model_weights_sha256",
        "tokenizer_bundle_sha256",
        "chat_template_sha256",
        "hardware_plan_sha256",
        "token_audit_receipt_sha256",
    ):
        _strict_sha256(value.get(field), label=f"{label} {field}")
    measurement_tool_version = value.get("measurement_tool_version")
    if (
        not isinstance(measurement_tool_version, str)
        or not VERSION_RE.fullmatch(measurement_tool_version)
    ):
        raise ValueError(f"{label} measurement_tool_version is invalid")
    target_modules = _strict_target_modules(
        value.get("target_modules"), label=f"{label} target_modules"
    )
    hardware_plan = _validate_hardware_plan(
        value.get("hardware_plan"),
        max_length=max_length,
        target_modules=target_modules,
        label=f"{label} hardware_plan",
    )
    expected_plan_sha = sha256_bytes(stable_json(hardware_plan).encode("utf-8"))
    if value["hardware_plan_sha256"] != expected_plan_sha:
        raise ValueError(f"{label} hardware_plan_sha256 mismatch")
    receipt_payload = {
        "sample_id": sample_id,
        "reason_code": reason_code,
        **{
            field: field_value
            for field, field_value in value.items()
            if field != "token_audit_receipt_sha256"
        },
    }
    expected_receipt_sha = sha256_bytes(
        stable_json(receipt_payload).encode("utf-8")
    )
    if value["token_audit_receipt_sha256"] != expected_receipt_sha:
        raise ValueError(f"{label} token_audit_receipt_sha256 mismatch")
    return value


def _load_quality_exclusions(
    path: Path,
    *,
    expected_ids: set[str],
    expected_source_row_sha256_by_id: dict[str, str],
    expected_unique_dataset_sha256: str,
) -> tuple[dict[str, dict[str, Any]], bytes, dict[str, Any]]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("quality exclusions JSON is not UTF-8") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_json_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("quality exclusions JSON is invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "contract_version",
        "label_classification",
        "entries",
    }:
        raise ValueError("quality exclusions JSON schema is invalid")
    contract_version = payload.get("contract_version")
    if contract_version not in QUALITY_EXCLUSION_REASON_CODES_BY_CONTRACT:
        raise ValueError("quality exclusions contract_version mismatch")
    if payload.get("label_classification") != LABEL_CLASSIFICATION:
        raise ValueError("quality exclusions must not claim human gold")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("quality exclusions entries must be an array")

    by_id: dict[str, dict[str, Any]] = {}
    reason_code_counts: Counter[str] = Counter()
    sequence_profile_bindings: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"quality exclusions entry {index} schema is invalid")
        entry_fields = set(entry)
        sample_id = entry.get("sample_id")
        reason_code = entry.get("reason_code")
        reason = entry.get("reason")
        if (
            not isinstance(sample_id, str)
            or not sample_id
            or sample_id != sample_id.strip()
            or sample_id not in expected_ids
        ):
            raise ValueError(
                f"quality exclusions entry {index} sample_id is outside TRAIN"
            )
        if sample_id in by_id:
            raise ValueError(f"quality exclusions duplicate sample_id: {sample_id}")
        allowed_reason_codes = QUALITY_EXCLUSION_REASON_CODES_BY_CONTRACT[
            contract_version
        ]
        if reason_code not in allowed_reason_codes:
            raise ValueError(
                f"quality exclusions entry {index} reason_code is invalid"
            )
        expected_entry_fields = {"sample_id", "reason_code", "reason"}
        if reason_code == SEQUENCE_LENGTH_HARDWARE_EXCLUSION:
            expected_entry_fields.add("evidence")
        if entry_fields != expected_entry_fields:
            raise ValueError(f"quality exclusions entry {index} schema is invalid")
        if (
            not isinstance(reason, str)
            or not reason
            or reason != reason.strip()
            or len(reason) > 1000
            or any(ord(character) < 32 for character in reason)
        ):
            raise ValueError(f"quality exclusions entry {index} reason is invalid")
        normalized_entry: dict[str, Any] = {
            "sample_id": sample_id,
            "reason_code": reason_code,
            "reason": reason,
        }
        if reason_code == SEQUENCE_LENGTH_HARDWARE_EXCLUSION:
            evidence = _validate_sequence_length_evidence(
                entry.get("evidence"),
                sample_id=sample_id,
                reason_code=reason_code,
                expected_source_row_sha256=(
                    expected_source_row_sha256_by_id[sample_id]
                ),
                expected_unique_dataset_sha256=expected_unique_dataset_sha256,
                label=f"quality exclusions entry {index} evidence",
            )
            normalized_entry["evidence"] = evidence
            sequence_profile_bindings.add(
                stable_json(
                    {
                        field: evidence[field]
                        for field in (
                            "max_length",
                            "unique_dataset_sha256",
                            "base_model_weights_sha256",
                            "tokenizer_bundle_sha256",
                            "chat_template_sha256",
                            "measurement_tool_version",
                            "target_modules",
                            "hardware_plan",
                            "hardware_plan_sha256",
                        )
                    }
                )
            )
        by_id[sample_id] = normalized_entry
        reason_code_counts[reason_code] += 1

    if len(sequence_profile_bindings) > 1:
        raise ValueError(
            "quality exclusions sequence hardware evidence uses mixed profiles"
        )

    binding = {
        "enabled": True,
        "contract_version": contract_version,
        "label_classification": LABEL_CLASSIFICATION,
        "input_file": {
            "filename": path.name,
            "sha256": sha256_bytes(raw),
        },
        "entry_count": len(by_id),
        "sample_ids_sha256": _sample_ids_sha256(set(by_id)),
        "reason_code_counts": dict(sorted(reason_code_counts.items())),
    }
    return by_id, raw, binding


def _load_train_sft(
    path: Path,
) -> tuple[list[dict[str, Any]], bytes, dict[str, str]]:
    rows, raw = shared._read_jsonl_objects(path, label="train_unique SFT")
    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    common_prompt: tuple[str, str] | None = None
    for line_number, row in enumerate(rows, start=1):
        if set(row) != {"messages", "metadata"}:
            raise ValueError(
                f"train_unique SFT line {line_number} must contain only "
                "messages and metadata"
            )
        messages = row.get("messages")
        if not isinstance(messages, list) or len(messages) != 3:
            raise ValueError(
                f"train_unique SFT line {line_number} has invalid messages"
            )
        for index, (message, expected_role) in enumerate(
            zip(messages, ("system", "user", "assistant")), start=1
        ):
            if (
                not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or message.get("role") != expected_role
                or not isinstance(message.get("content"), str)
            ):
                raise ValueError(
                    f"train_unique SFT line {line_number} message {index} "
                    "is invalid"
                )
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError(
                f"train_unique SFT line {line_number} metadata is invalid"
            )
        sample_id = shared._strict_sample_id(
            metadata.get("sample_id"),
            label="train_unique SFT",
            line_number=line_number,
        )
        if sample_id in seen:
            raise ValueError(f"train_unique SFT duplicate sample_id: {sample_id}")
        seen.add(sample_id)
        if str(metadata.get("split") or "").strip().upper() != "TRAIN":
            raise ValueError(f"train_unique SFT row is not TRAIN: {sample_id}")
        if metadata.get("target_contract") != TARGET_CONTRACT:
            raise ValueError(
                f"train_unique SFT target_contract mismatch: {sample_id}"
            )
        if metadata.get("model_output_contract") != MODEL_OUTPUT_CONTRACT:
            raise ValueError(
                f"train_unique SFT model_output_contract mismatch: {sample_id}"
            )
        if metadata.get("weak_supervision_version") != (
            QWEN_WEAK_SUPERVISION_VERSION
        ):
            raise ValueError(
                f"train_unique SFT weak supervision version mismatch: {sample_id}"
            )
        for field in shared.REQUIRED_FALSE_BOUNDARIES:
            if metadata.get(field) is not False:
                raise ValueError(
                    f"train_unique SFT {field} must be false: {sample_id}"
                )

        prompt_version = metadata.get("prompt_version")
        prompt_sha256 = metadata.get("prompt_sha256")
        if prompt_version != QWEN_WEAK_PROMPT_VERSION:
            raise ValueError(
                f"train_unique SFT prompt_version mismatch: {sample_id}"
            )
        if not isinstance(prompt_sha256, str):
            raise ValueError(
                f"train_unique SFT prompt_sha256 is invalid: {sample_id}"
            )
        prompt_sha256 = prompt_sha256.strip().lower()
        if not shared.SHA256_RE.fullmatch(prompt_sha256):
            raise ValueError(
                f"train_unique SFT prompt_sha256 is invalid: {sample_id}"
            )
        if prompt_sha256 != QWEN_WEAK_PROMPT_SHA256:
            raise ValueError(
                f"train_unique SFT prompt contract SHA mismatch: {sample_id}"
            )
        if messages[0]["content"] != QWEN_WEAK_SYSTEM_PROMPT:
            raise ValueError(
                f"train_unique SFT system prompt text mismatch: {sample_id}"
            )
        actual_prompt_sha256 = sha256_bytes(
            messages[0]["content"].encode("utf-8")
        )
        if actual_prompt_sha256 != prompt_sha256:
            raise ValueError(f"train_unique SFT prompt SHA mismatch: {sample_id}")
        prompt_binding = (prompt_version.strip(), prompt_sha256)
        if common_prompt is None:
            common_prompt = prompt_binding
        elif prompt_binding != common_prompt:
            raise ValueError(
                f"train_unique SFT prompt binding is mixed: {sample_id}"
            )

        try:
            content = json.loads(messages[1]["content"])
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"train_unique SFT user content is not JSON: {sample_id}"
            ) from exc
        if not isinstance(content, dict) or not content:
            raise ValueError(
                f"train_unique SFT user content is invalid: {sample_id}"
            )
        content_sha256 = sha256_bytes(stable_json(content).encode("utf-8"))
        stored_content_sha256 = metadata.get("content_sha256")
        if not isinstance(stored_content_sha256, str) or (
            stored_content_sha256.strip().lower() != content_sha256
        ):
            raise ValueError(
                f"train_unique SFT content SHA mismatch: {sample_id}"
            )

        base_metadata = {
            field: metadata[field]
            for field in shared.SAFE_BASE_METADATA_FIELDS
            if field in metadata
        }
        prepared.append(
            {
                "sample_id": sample_id,
                "source_sft_row_sha256": sha256_bytes(
                    stable_json(row).encode("utf-8")
                ),
                "system_message": dict(messages[0]),
                "user_message": dict(messages[1]),
                "content": content,
                "content_sha256": content_sha256,
                "base_metadata": base_metadata,
            }
        )
    assert common_prompt is not None
    return prepared, raw, {
        "version": common_prompt[0],
        "sha256": common_prompt[1],
    }


def _bind_train_source_content(
    *, source_content: dict[str, Any], train_content: dict[str, Any], sample_id: str
) -> dict[str, str]:
    source_json = stable_json(source_content)
    train_json = stable_json(train_content)
    raw_match = source_json == train_json
    try:
        normalized_train = shared._normalize_content_timestamps(train_content)
        normalized_source = (
            normalized_train
            if raw_match
            else shared._normalize_content_timestamps(source_content)
        )
    except ValueError as exc:
        raise ValueError(
            f"source-only timestamp normalization failed for {sample_id}: {exc}"
        ) from exc
    normalized_json = stable_json(normalized_train)
    if not raw_match and stable_json(normalized_source) != normalized_json:
        raise ValueError(
            "source-only content does not match train_unique user payload under "
            f"{shared.SOURCE_CONTENT_EQUIVALENCE_CONTRACT_VERSION}: {sample_id}"
        )
    return {
        "match_method": "RAW_EXACT" if raw_match else "TIMEZONE_NORMALIZED",
        "raw_source_content_sha256": sha256_bytes(source_json.encode("utf-8")),
        "raw_train_content_sha256": sha256_bytes(train_json.encode("utf-8")),
        "normalized_content_sha256": sha256_bytes(
            normalized_json.encode("utf-8")
        ),
    }


def _validate_multiplier_policy(
    payload: Any,
    *,
    source: str,
    preset: str | None,
    input_file: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {
        "contract_version",
        "policy_version",
        "multipliers",
    }:
        raise ValueError(
            "pair multiplier policy must contain only contract_version, "
            "policy_version and multipliers"
        )
    if payload.get("contract_version") != PAIR_MULTIPLIER_CONTRACT_VERSION:
        raise ValueError("pair multiplier policy contract_version mismatch")
    policy_version = payload.get("policy_version")
    if (
        not isinstance(policy_version, str)
        or not POLICY_VERSION_RE.fullmatch(policy_version)
    ):
        raise ValueError("pair multiplier policy_version is invalid")
    multipliers = payload.get("multipliers")
    if not isinstance(multipliers, dict) or set(multipliers) != set(
        TRAINABLE_PAIR_KEYS
    ):
        raise ValueError(
            "pair multiplier keys must exactly cover all trainable core-axis pairs"
        )
    normalized_multipliers: dict[str, int] = {}
    for key in TRAINABLE_PAIR_KEYS:
        value = multipliers[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= MAX_PAIR_MULTIPLIER
        ):
            raise ValueError(
                f"pair multiplier for {key} must be an integer from 1 to "
                f"{MAX_PAIR_MULTIPLIER}"
            )
        normalized_multipliers[key] = value
    canonical_payload = {
        "contract_version": PAIR_MULTIPLIER_CONTRACT_VERSION,
        "policy_version": policy_version,
        "multipliers": normalized_multipliers,
    }
    resolution_policy = _resolution_multiplier_policy(preset=preset)
    canonical_behavior = {
        "pair_multiplier_policy": canonical_payload,
        "resolution_multiplier_policy": resolution_policy,
    }
    return {
        **canonical_payload,
        "resolution_multiplier_policy": resolution_policy,
        "source": source,
        "preset": preset,
        "policy_sha256": sha256_bytes(
            stable_json(canonical_behavior).encode("utf-8")
        ),
        "input_file": input_file,
        "policy_design_provenance": _policy_design_provenance(
            source=source,
            preset=preset,
        ),
        "builder_runtime_input_isolation": dict(
            BUILDER_RUNTIME_INPUT_ISOLATION
        ),
    }


def _resolve_multiplier_policy(
    *,
    pair_multipliers_json: Path | None,
    pair_multiplier_preset: str | None,
) -> dict[str, Any]:
    if (pair_multipliers_json is None) == (pair_multiplier_preset is None):
        raise ValueError(
            "select exactly one pair multiplier JSON or versioned preset"
        )
    if pair_multiplier_preset is not None:
        if pair_multiplier_preset not in PAIR_MULTIPLIER_PRESETS:
            raise ValueError(
                f"unknown pair multiplier preset: {pair_multiplier_preset}"
            )
        return _validate_multiplier_policy(
            PAIR_MULTIPLIER_PRESETS[pair_multiplier_preset],
            source="VERSIONED_PRESET",
            preset=pair_multiplier_preset,
            input_file=None,
        )

    assert pair_multipliers_json is not None
    policy_path = pair_multipliers_json.resolve()
    if not policy_path.is_file():
        raise FileNotFoundError(
            f"pair multiplier policy input is missing: {policy_path}"
        )
    raw = policy_path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("pair multiplier policy is not UTF-8") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("pair multiplier policy is not valid JSON") from exc
    return _validate_multiplier_policy(
        payload,
        source="EXPLICIT_JSON_FILE",
        preset=None,
        input_file={
            "filename": policy_path.name,
            "sha256": sha256_bytes(raw),
        },
    )


def _effective_pair_multiplier(
    *,
    multiplier_policy: dict[str, Any],
    decision_source: str,
    pair_key: str,
) -> int:
    resolution_policy = multiplier_policy["resolution_multiplier_policy"]
    c_multiplier = resolution_policy["c_arbitration_fixed_multiplier"]
    if decision_source == "C_ARBITRATION" and c_multiplier is not None:
        return c_multiplier
    return multiplier_policy["multipliers"][pair_key]


def _distribution_payload(
    *,
    row_count: int,
    materiality: Counter[str],
    polarity: Counter[str],
    pair: Counter[str],
    semantic_priority: Counter[str],
) -> dict[str, Any]:
    return {
        "row_count": row_count,
        "materiality": dict(sorted(materiality.items())),
        "polarity": dict(sorted(polarity.items())),
        "pair": dict(sorted(pair.items())),
        "semantic_priority": dict(sorted(semantic_priority.items())),
    }


def _output_bytes(rows: list[dict[str, Any]]) -> bytes:
    return b"".join((stable_json(row) + "\n").encode("utf-8") for row in rows)


def build_overlay(
    *,
    train_sft: Path,
    source_only: Path,
    review_a: Path,
    review_b: Path,
    review_c: Path,
    output_dir: Path,
    pair_multipliers_json: Path | None = None,
    pair_multiplier_preset: str | None = None,
    quality_exclusions_json: Path | None = None,
) -> dict[str, Any]:
    """Validate all bindings and atomically publish both TRAIN overlays."""

    if os.path.lexists(output_dir):
        raise FileExistsError(f"output directory already exists: {output_dir}")
    output_dir = output_dir.resolve()
    if os.path.lexists(output_dir):
        raise FileExistsError(f"output directory already exists: {output_dir}")
    multiplier_policy = _resolve_multiplier_policy(
        pair_multipliers_json=pair_multipliers_json,
        pair_multiplier_preset=pair_multiplier_preset,
    )
    paths = {
        "train_sft": train_sft.resolve(),
        "source_only": source_only.resolve(),
        "review_a": review_a.resolve(),
        "review_b": review_b.resolve(),
        "review_c": review_c.resolve(),
    }
    missing = [f"{name}={path}" for name, path in paths.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError("required input is missing: " + ", ".join(missing))

    train_rows, train_raw, prompt = _load_train_sft(paths["train_sft"])
    if len(train_rows) != EXPECTED_UNIQUE_ROW_COUNT:
        raise ValueError(
            f"train_unique SFT must contain exactly {EXPECTED_UNIQUE_ROW_COUNT} "
            f"rows; found {len(train_rows)}"
        )
    ordered_ids = [row["sample_id"] for row in train_rows]
    expected_ids = set(ordered_ids)

    quality_exclusions_by_id: dict[str, dict[str, Any]] = {}
    quality_exclusions_raw: bytes | None = None
    quality_exclusions_binding: dict[str, Any] = {
        "enabled": False,
        "contract_version": QUALITY_EXCLUSIONS_CONTRACT_VERSION,
        "label_classification": LABEL_CLASSIFICATION,
        "input_file": None,
        "entry_count": 0,
        "sample_ids_sha256": _sample_ids_sha256(set()),
        "reason_code_counts": {},
    }
    if quality_exclusions_json is not None:
        quality_exclusions_path = quality_exclusions_json.resolve()
        if not quality_exclusions_path.is_file():
            raise FileNotFoundError(
                "quality exclusions input is missing: "
                f"{quality_exclusions_path}"
            )
        (
            quality_exclusions_by_id,
            quality_exclusions_raw,
            quality_exclusions_binding,
        ) = _load_quality_exclusions(
            quality_exclusions_path,
            expected_ids=expected_ids,
            expected_source_row_sha256_by_id={
                row["sample_id"]: row["source_sft_row_sha256"]
                for row in train_rows
            },
            expected_unique_dataset_sha256=sha256_bytes(train_raw),
        )

    source_by_id, source_raw = shared._load_source_only(paths["source_only"])
    review_a_by_id, review_a_raw = shared._load_reviews(
        paths["review_a"], slot="A"
    )
    review_b_by_id, review_b_raw = shared._load_reviews(
        paths["review_b"], slot="B"
    )
    shared._require_coverage(
        label="source-only", actual=set(source_by_id), expected=expected_ids
    )
    shared._require_coverage(
        label="review A", actual=set(review_a_by_id), expected=expected_ids
    )
    shared._require_coverage(
        label="review B", actual=set(review_b_by_id), expected=expected_ids
    )

    source_binding_by_id: dict[str, dict[str, str]] = {}
    source_match_counts: Counter[str] = Counter()
    for train_row in train_rows:
        sample_id = train_row["sample_id"]
        binding = _bind_train_source_content(
            source_content=source_by_id[sample_id],
            train_content=train_row["content"],
            sample_id=sample_id,
        )
        if binding["raw_train_content_sha256"] != train_row["content_sha256"]:
            raise RuntimeError(f"train_unique content binding changed: {sample_id}")
        source_binding_by_id[sample_id] = binding
        source_match_counts[binding["match_method"]] += 1
    raw_match_count = source_match_counts["RAW_EXACT"]
    timezone_normalized_match_count = source_match_counts["TIMEZONE_NORMALIZED"]
    if (
        raw_match_count + timezone_normalized_match_count
        != EXPECTED_UNIQUE_ROW_COUNT
    ):
        raise RuntimeError("source content equivalence accounting is incomplete")

    disagreement_ids = {
        sample_id
        for sample_id in expected_ids
        if review_a_by_id[sample_id]["pair"]
        != review_b_by_id[sample_id]["pair"]
    }
    review_c_by_id, review_c_raw = shared._load_reviews(
        paths["review_c"], slot="C", allow_empty=True
    )
    shared._require_coverage(
        label="review C arbitration",
        actual=set(review_c_by_id),
        expected=disagreement_ids,
    )

    input_raw = {
        "train_sft": train_raw,
        "source_only": source_raw,
        "review_a": review_a_raw,
        "review_b": review_b_raw,
        "review_c": review_c_raw,
    }
    input_sha256 = {name: sha256_bytes(raw) for name, raw in input_raw.items()}
    input_rows = {
        "train_sft": len(train_rows),
        "source_only": len(source_by_id),
        "review_a": len(review_a_by_id),
        "review_b": len(review_b_by_id),
        "review_c": len(review_c_by_id),
    }
    if quality_exclusions_raw is not None:
        input_sha256["quality_exclusions"] = sha256_bytes(
            quality_exclusions_raw
        )
        input_rows["quality_exclusions"] = len(quality_exclusions_by_id)
    policy_summary = {
        key: value
        for key, value in multiplier_policy.items()
        if key != "multipliers"
    }

    unique_rows: list[dict[str, Any]] = []
    resolution_counts: Counter[str] = Counter()
    unique_materiality: Counter[str] = Counter()
    unique_polarity: Counter[str] = Counter()
    unique_pair: Counter[str] = Counter()
    unique_priority: Counter[str] = Counter()
    trainable_unique_materiality: Counter[str] = Counter()
    trainable_unique_polarity: Counter[str] = Counter()
    trainable_unique_pair: Counter[str] = Counter()
    trainable_unique_priority: Counter[str] = Counter()
    trainable_resolution_counts: Counter[str] = Counter()
    excluded_resolution_counts: Counter[str] = Counter()
    excluded_pair_resolution_counts: Counter[str] = Counter()
    exclusion_reasons: Counter[str] = Counter()
    excluded_effective_replica_count = 0
    numeric_exclusion_ids: set[str] = set()
    numeric_source_exclusion_enabled = (
        multiplier_policy["preset"] == V13_CURRICULUM_PRESET
    )

    for train_row in train_rows:
        sample_id = train_row["sample_id"]
        source_binding = source_binding_by_id[sample_id]
        a = review_a_by_id[sample_id]
        b = review_b_by_id[sample_id]
        if a["pair"] == b["pair"]:
            selected_pair = a["pair"]
            decision_source = "A_B_CONSENSUS"
            selected_slot = "A+B"
            review_hashes = {
                "A": a["row_sha256"],
                "B": b["row_sha256"],
            }
        else:
            c = review_c_by_id[sample_id]
            selected_pair = c["pair"]
            decision_source = "C_ARBITRATION"
            selected_slot = "C"
            review_hashes = {
                "A": a["row_sha256"],
                "B": b["row_sha256"],
                "C": c["row_sha256"],
            }
        materiality, polarity = selected_pair
        semantic_target = expected_semantic_payload(materiality, polarity)
        semantic_issues = validate_semantic_payload(semantic_target)
        if semantic_issues:
            raise RuntimeError(
                f"derived core-v1 payload is invalid for {sample_id}: "
                f"{semantic_issues}"
            )
        pair_key = _pair_key(materiality, polarity)
        pair_multiplier = _effective_pair_multiplier(
            multiplier_policy=multiplier_policy,
            decision_source=decision_source,
            pair_key=pair_key,
        )
        # UNCLEAR is a scored member of both model axes.  Eligibility does not
        # inspect the resolved label.  The v13-only structural exclusion uses
        # source bytes alone to keep numeric tables out of the trainable view
        # while retaining all 729 rows in the unique audit artifact.
        source_structure = _source_structure_metrics(train_row["content"])
        numeric_source_excluded = (
            numeric_source_exclusion_enabled
            and source_structure["numeric_table_dominated"]
        )
        quality_exclusion = quality_exclusions_by_id.get(sample_id)
        if numeric_source_excluded and quality_exclusion is not None:
            raise ValueError(
                "quality exclusion overlaps numeric source exclusion: "
                f"{sample_id}"
            )
        trainable = not numeric_source_excluded and quality_exclusion is None
        if numeric_source_excluded:
            numeric_exclusion_ids.add(sample_id)
            exclusion_reason = NUMERIC_TABLE_EXCLUSION_REASON
        elif quality_exclusion is not None:
            exclusion_reason = quality_exclusion["reason_code"]
        else:
            exclusion_reason = None
        model_target = {"materiality": materiality, "polarity": polarity}
        metadata = {
            **train_row["base_metadata"],
            "sample_id": sample_id,
            "source_sft_row_sha256": train_row["source_sft_row_sha256"],
            "content_sha256": train_row["content_sha256"],
            "split": "TRAIN",
            "target_contract": TARGET_CONTRACT,
            "model_output_contract": MODEL_OUTPUT_CONTRACT,
            "semantic_target": semantic_target,
            "prompt_version": prompt["version"],
            "prompt_sha256": prompt["sha256"],
            "overlay_contract_version": CONTRACT_VERSION,
            "overlay_view": "UNIQUE_AUDIT",
            "source_dataset_contract": QWEN_WEAK_SUPERVISION_VERSION,
            "label_provenance": LABEL_PROVENANCE,
            "label_classification": LABEL_CLASSIFICATION,
            "human_gold_claimed": False,
            "qwen_prediction_included": False,
            "post_event_market_data_included": False,
            "evidence_state_used_as_model_target": False,
            "original_weak_truth_used": False,
            "source_payload_binding_verified": True,
            "source_payload_sha256": source_binding[
                "raw_source_content_sha256"
            ],
            "source_content_equivalence": {
                "contract_version": (
                    shared.SOURCE_CONTENT_EQUIVALENCE_CONTRACT_VERSION
                ),
                "match_method": source_binding["match_method"],
                "raw_train_content_sha256": source_binding[
                    "raw_train_content_sha256"
                ],
                "raw_source_content_sha256": source_binding[
                    "raw_source_content_sha256"
                ],
                "normalized_content_sha256": source_binding[
                    "normalized_content_sha256"
                ],
                "raw_match_count": raw_match_count,
                "timezone_normalized_match_count": (
                    timezone_normalized_match_count
                ),
            },
            "overlay_input_sha256": dict(input_sha256),
            "review_resolution": {
                "policy": "A_B_CONSENSUS_ELSE_C_ONLY",
                "agreement_basis": "COMPLETE_MATERIALITY_POLARITY_PAIR",
                "decision_source": decision_source,
                "selected_review_slot": selected_slot,
                "a_b_target_pair_agreed": decision_source == "A_B_CONSENSUS",
                "original_review_row_sha256": review_hashes,
                "original_review_file_sha256": {
                    "A": input_sha256["review_a"],
                    "B": input_sha256["review_b"],
                    "C": input_sha256["review_c"],
                },
            },
            "training_eligibility": {
                "eligible": trainable,
                "exclusion_reason": exclusion_reason,
                "labels_rewritten": False,
                "pair_multiplier": pair_multiplier,
            },
            "quality_exclusion": None,
            "source_structure": source_structure,
            "pair_multiplier_policy": dict(policy_summary),
        }
        if quality_exclusion is not None:
            quality_metadata = {
                "contract_version": quality_exclusions_binding[
                    "contract_version"
                ],
                "label_classification": LABEL_CLASSIFICATION,
                "reason_code": quality_exclusion["reason_code"],
                "reason": quality_exclusion["reason"],
                "input_sha256": quality_exclusions_binding["input_file"][
                    "sha256"
                ],
            }
            if "evidence" in quality_exclusion:
                quality_metadata["evidence"] = quality_exclusion["evidence"]
            metadata["quality_exclusion"] = quality_metadata
        unique_rows.append(
            {
                "messages": [
                    train_row["system_message"],
                    train_row["user_message"],
                    {"role": "assistant", "content": stable_json(model_target)},
                ],
                "metadata": metadata,
            }
        )
        resolution_counts[decision_source] += 1
        unique_materiality[materiality] += 1
        unique_polarity[polarity] += 1
        unique_pair[pair_key] += 1
        unique_priority[semantic_target["semantic_priority"]] += 1
        if trainable:
            trainable_unique_materiality[materiality] += 1
            trainable_unique_polarity[polarity] += 1
            trainable_unique_pair[pair_key] += 1
            trainable_unique_priority[semantic_target["semantic_priority"]] += 1
            trainable_resolution_counts[decision_source] += 1
        else:
            assert exclusion_reason is not None
            exclusion_reasons[exclusion_reason] += 1
            excluded_resolution_counts[decision_source] += 1
            excluded_pair_resolution_counts[
                f"{decision_source}::{pair_key}"
            ] += 1
            excluded_effective_replica_count += pair_multiplier

    if len(unique_rows) != EXPECTED_UNIQUE_ROW_COUNT:
        raise RuntimeError("unique overlay row count changed during construction")

    trainable_rows: list[dict[str, Any]] = []
    effective_materiality: Counter[str] = Counter()
    effective_polarity: Counter[str] = Counter()
    effective_pair: Counter[str] = Counter()
    effective_priority: Counter[str] = Counter()
    effective_resolution: Counter[str] = Counter()
    for unique_row in unique_rows:
        unique_metadata = unique_row["metadata"]
        eligibility = unique_metadata["training_eligibility"]
        if not eligibility["eligible"]:
            continue
        multiplier = eligibility["pair_multiplier"]
        assert isinstance(multiplier, int)
        source_sample_id = unique_metadata["sample_id"]
        source_unique_row_sha256 = sha256_bytes(
            stable_json(unique_row).encode("utf-8")
        )
        materiality = unique_metadata["semantic_target"]["materiality"]
        polarity = unique_metadata["semantic_target"]["polarity"]
        pair_key = _pair_key(materiality, polarity)
        for replica_index in range(1, multiplier + 1):
            replica_id = sha256_bytes(
                (
                    f"{multiplier_policy['policy_sha256']}\0{source_sample_id}\0"
                    f"{replica_index}\0{multiplier}"
                ).encode("utf-8")
            )
            replica_metadata = {
                **unique_metadata,
                "overlay_view": "TRAINABLE_BALANCED",
                "training_replica": {
                    "replica_id": replica_id,
                    "source_unique_sample_id": source_sample_id,
                    "source_unique_row_sha256": source_unique_row_sha256,
                    "replica_index": replica_index,
                    "replica_count": multiplier,
                    "labels_rewritten": False,
                },
            }
            trainable_rows.append(
                {
                    "messages": [dict(message) for message in unique_row["messages"]],
                    "metadata": replica_metadata,
                }
            )
            effective_materiality[materiality] += 1
            effective_polarity[polarity] += 1
            effective_pair[pair_key] += 1
            effective_priority[
                unique_metadata["semantic_target"]["semantic_priority"]
            ] += 1
            effective_resolution[
                unique_metadata["review_resolution"]["decision_source"]
            ] += 1
    if not trainable_rows:
        raise ValueError("resolved TRAIN reviews contain no trainable core-axis rows")

    unique_bytes = _output_bytes(unique_rows)
    trainable_bytes = _output_bytes(trainable_rows)
    unique_sha256 = sha256_bytes(unique_bytes)
    trainable_sha256 = sha256_bytes(trainable_bytes)
    unique_sidecar = f"{unique_sha256}  {UNIQUE_OUTPUT_NAME}\n".encode("ascii")
    trainable_sidecar = (
        f"{trainable_sha256}  {TRAINABLE_OUTPUT_NAME}\n"
    ).encode("ascii")
    sorted_disagreements = sorted(disagreement_ids)
    normalized_content_index = [
        {
            "sample_id": sample_id,
            "normalized_content_sha256": source_binding_by_id[sample_id][
                "normalized_content_sha256"
            ],
        }
        for sample_id in ordered_ids
    ]
    trainable_unique_count = sum(trainable_unique_pair.values())
    trainable_sample_ids = {
        row["metadata"]["sample_id"]
        for row in unique_rows
        if row["metadata"]["training_eligibility"]["eligible"]
    }
    quality_exclusion_ids = set(quality_exclusions_by_id)
    excluded_complement_ids = expected_ids - trainable_sample_ids
    if numeric_exclusion_ids & quality_exclusion_ids:
        raise RuntimeError("numeric and quality exclusion membership overlaps")
    if excluded_complement_ids != numeric_exclusion_ids | quality_exclusion_ids:
        raise RuntimeError("training exclusion membership does not close")
    membership_commitment = {
        "contract_version": TRAIN_MEMBERSHIP_COMMITMENT_CONTRACT,
        "original_unique": _membership_binding(expected_ids),
        "trainable_unique": _membership_binding(trainable_sample_ids),
        "excluded_complement": _membership_binding(excluded_complement_ids),
        "numeric_exclusions": _membership_binding(numeric_exclusion_ids),
        "quality_exclusions": _membership_binding(quality_exclusion_ids),
        "exclusion_classes_disjoint": True,
    }
    resolution_multiplier_policy = multiplier_policy[
        "resolution_multiplier_policy"
    ]
    curriculum_version = resolution_multiplier_policy["curriculum_version"]
    effective_distribution = _distribution_payload(
        row_count=len(trainable_rows),
        materiality=effective_materiality,
        polarity=effective_polarity,
        pair=effective_pair,
        semantic_priority=effective_priority,
    )
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contract_version": CONTRACT_VERSION,
        "source_dataset_contract": QWEN_WEAK_SUPERVISION_VERSION,
        "target_contract": TARGET_CONTRACT,
        "model_output_contract": MODEL_OUTPUT_CONTRACT,
        "label_provenance": LABEL_PROVENANCE,
        "label_classification": LABEL_CLASSIFICATION,
        "human_gold_claimed": False,
        "expected_unique_row_count": EXPECTED_UNIQUE_ROW_COUNT,
        "quality_exclusions": quality_exclusions_binding,
        "membership_commitment": membership_commitment,
        "review_input_schema": {
            "fields": sorted(shared.REVIEW_ROW_FIELDS),
            "review_class_by_slot": dict(shared.REVIEW_CLASS_BY_SLOT),
            "semantic_v2_fields_required_or_derived": False,
        },
        "prompt": {
            "version": prompt["version"],
            "sha256": prompt["sha256"],
            "system_message_binding_verified": True,
        },
        "inputs": {
            name: {
                "filename": paths[name].name,
                "sha256": input_sha256[name],
                "row_count": input_rows[name],
            }
            for name in paths
        },
        "coverage": {
            "train_source_a_b_sample_id_coverage_exact": True,
            "review_c_equals_a_b_disagreement_set": True,
            "a_b_disagreement_count": len(disagreement_ids),
            "a_b_disagreement_sample_ids_sha256": sha256_bytes(
                stable_json(sorted_disagreements).encode("utf-8")
            ),
            "source_payload_binding_verified_rows": len(unique_rows),
        },
        "source_content_equivalence": {
            "contract_version": (
                shared.SOURCE_CONTENT_EQUIVALENCE_CONTRACT_VERSION
            ),
            "raw_match_fast_path": True,
            "timezone_normalized_fallback": True,
            "normalized_time_keys": sorted(shared.SOURCE_CONTENT_TIME_KEYS),
            "aware_datetime_canonical_timezone": "UTC",
            "published_at_date_only_preserved": True,
            "naive_or_unparseable_datetime_allowed": False,
            "raw_match_count": raw_match_count,
            "timezone_normalized_match_count": timezone_normalized_match_count,
            "normalized_content_sha256_index_sha256": sha256_bytes(
                stable_json(normalized_content_index).encode("utf-8")
            ),
        },
        "resolution_policy": {
            "name": "A_B_CONSENSUS_ELSE_C_ONLY",
            "agreement_basis": "COMPLETE_MATERIALITY_POLARITY_PAIR",
            "axis_wise_label_mixing_allowed": False,
            "c_used_only_for_a_b_pair_disagreements": True,
        },
        "resolution_counts": dict(sorted(resolution_counts.items())),
        "pair_multiplier_policy": multiplier_policy,
        "curriculum": {
            "enabled": curriculum_version is not None,
            "version": curriculum_version,
            "split_scope": resolution_multiplier_policy["split_scope"],
            "label_classification": LABEL_CLASSIFICATION,
            "unique_source_row_count": len(unique_rows),
            "a_b_clean_consensus": {
                "unique_row_count": resolution_counts["A_B_CONSENSUS"],
                "effective_row_count": effective_resolution["A_B_CONSENSUS"],
                "multiplier_source": resolution_multiplier_policy[
                    "a_b_consensus_multiplier_source"
                ],
                "joint_pair_multipliers": dict(
                    sorted(multiplier_policy["multipliers"].items())
                ),
            },
            "c_arbitration": {
                "unique_row_count": resolution_counts["C_ARBITRATION"],
                "effective_row_count": effective_resolution["C_ARBITRATION"],
                "fixed_multiplier": resolution_multiplier_policy[
                    "c_arbitration_fixed_multiplier"
                ],
            },
            "effective_distribution": effective_distribution,
            "input_isolation": {
                "train_only": True,
                "dev_metrics_read": False,
                "qwen_predictions_read": False,
                "market_results_read": False,
                "sealed_benchmark_read": False,
            },
        },
        "trainability_policy": {
            "materiality_allowed": sorted(TRAINABLE_MATERIALITY),
            "polarity_allowed": sorted(TRAINABLE_POLARITY),
            "unclear_training_enabled": True,
            "unclear_labels_rewritten": False,
            "original_unique_row_count": len(unique_rows),
            "trainable_unique_row_count": trainable_unique_count,
            "excluded_unique_row_count": len(unique_rows) - trainable_unique_count,
            "excluded_effective_replica_count": (
                excluded_effective_replica_count
            ),
            "pre_exclusion_effective_row_count": (
                len(trainable_rows) + excluded_effective_replica_count
            ),
            "trainable_effective_row_count": len(trainable_rows),
            "exclusion_reasons": dict(sorted(exclusion_reasons.items())),
            "trainable_resolution_counts": dict(
                sorted(trainable_resolution_counts.items())
            ),
            "excluded_resolution_counts": dict(
                sorted(excluded_resolution_counts.items())
            ),
            "excluded_pair_resolution_counts": dict(
                sorted(excluded_pair_resolution_counts.items())
            ),
            "source_structure_exclusion": {
                "enabled": numeric_source_exclusion_enabled,
                "reason": NUMERIC_TABLE_EXCLUSION_REASON,
                "stable_json_character_count_min": (
                    NUMERIC_TABLE_MIN_STABLE_JSON_CHARS
                ),
                "digit_character_ratio_min": NUMERIC_TABLE_MIN_DIGIT_RATIO,
                "label_independent": True,
                "applies_to_preset": V13_CURRICULUM_PRESET,
            },
        },
        "distributions": {
            "unique_audit": _distribution_payload(
                row_count=len(unique_rows),
                materiality=unique_materiality,
                polarity=unique_polarity,
                pair=unique_pair,
                semantic_priority=unique_priority,
            ),
            "trainable_unique": _distribution_payload(
                row_count=trainable_unique_count,
                materiality=trainable_unique_materiality,
                polarity=trainable_unique_polarity,
                pair=trainable_unique_pair,
                semantic_priority=trainable_unique_priority,
            ),
            "trainable_effective": effective_distribution,
        },
        "outputs": {
            "unique_audit": {
                "filename": UNIQUE_OUTPUT_NAME,
                "row_count": len(unique_rows),
                "sample_ids_sha256": _sample_ids_sha256(ordered_ids),
                "sha256": unique_sha256,
                "sidecar": UNIQUE_OUTPUT_NAME + ".sha256",
                "sidecar_sha256": sha256_bytes(unique_sidecar),
            },
            "trainable_balanced": {
                "filename": TRAINABLE_OUTPUT_NAME,
                "unique_source_row_count": trainable_unique_count,
                "sample_ids_sha256": _sample_ids_sha256(trainable_sample_ids),
                "row_count": len(trainable_rows),
                "sha256": trainable_sha256,
                "sidecar": TRAINABLE_OUTPUT_NAME + ".sha256",
                "sidecar_sha256": sha256_bytes(trainable_sidecar),
            },
        },
        "isolation": {
            "source_payloads_match_under_declared_equivalence_contract": True,
            "sft_assistant_target_ignored": True,
            "sft_metadata_semantic_target_ignored": True,
            "original_weak_truth_decoded_as_semantic_payload": False,
            "original_weak_truth_used": False,
            "qwen_predictions_read": False,
            "dev_metrics_read": False,
            "market_results_read": False,
            "sealed_benchmark_read": False,
            "external_facts_read": False,
            "frozen_review_rows_rewritten": False,
            "unclear_labels_rewritten": False,
            "input_sha256_embedded_in_each_output_row": True,
        },
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    manifest_sha256 = sha256_bytes(manifest_bytes)
    manifest_sidecar = f"{manifest_sha256}  {MANIFEST_NAME}\n".encode("ascii")
    shared._publish_atomic(
        output_dir,
        {
            UNIQUE_OUTPUT_NAME: unique_bytes,
            UNIQUE_OUTPUT_NAME + ".sha256": unique_sidecar,
            TRAINABLE_OUTPUT_NAME: trainable_bytes,
            TRAINABLE_OUTPUT_NAME + ".sha256": trainable_sidecar,
            MANIFEST_NAME: manifest_bytes,
            MANIFEST_NAME + ".sha256": manifest_sidecar,
        },
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-sft", type=Path, required=True)
    parser.add_argument("--source-only", type=Path, required=True)
    parser.add_argument("--review-a", type=Path, required=True)
    parser.add_argument("--review-b", type=Path, required=True)
    parser.add_argument("--review-c", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--quality-exclusions-json", type=Path)
    policy = parser.add_mutually_exclusive_group(required=True)
    policy.add_argument("--pair-multipliers-json", type=Path)
    policy.add_argument(
        "--pair-multiplier-preset",
        choices=sorted(PAIR_MULTIPLIER_PRESETS),
    )
    args = parser.parse_args(argv)
    manifest = build_overlay(
        train_sft=args.train_sft,
        source_only=args.source_only,
        review_a=args.review_a,
        review_b=args.review_b,
        review_c=args.review_c,
        output_dir=args.output_dir,
        pair_multipliers_json=args.pair_multipliers_json,
        pair_multiplier_preset=args.pair_multiplier_preset,
        quality_exclusions_json=args.quality_exclusions_json,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
