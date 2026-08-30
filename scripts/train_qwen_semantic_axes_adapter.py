#!/usr/bin/env python3
"""Train a reproducible Qwen2.5-1.5B semantic-axes QLoRA adapter.

The driver accepts only the trainable balanced overlay produced from frozen
independent AI reviews.  It validates the overlay manifest, SHA sidecars,
prompt bytes, two-axis target and derived core-v1 target before importing a
training stack.  DEV, predictions, market results and sealed benchmarks are
not accepted inputs.  Evaluation and best-model loading are always disabled.

``--dry-run`` performs every contract, environment, model fingerprint,
adapter and tokenizer-length check without loading model weights.  A real run
trains in a deterministic staging directory and atomically renames it to the
requested output directory only after the final adapter and manifest exist.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
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
    QWEN_WEAK_SYSTEM_PROMPT,
)
from scripts.build_qwen_train_ai_review_overlay import (  # noqa: E402
    BUILDER_RUNTIME_INPUT_ISOLATION,
    EXPLICIT_POLICY_DESIGN_PROVENANCE,
    NEUTRAL_POLICY_DESIGN_PROVENANCE,
    NUMERIC_TABLE_EXCLUSION_REASON,
    NUMERIC_TABLE_MIN_DIGIT_RATIO,
    NUMERIC_TABLE_MIN_STABLE_JSON_CHARS,
    QUALITY_EXCLUSION_REASON_CODES,
    QUALITY_EXCLUSIONS_CONTRACT_V1 as BUILDER_QUALITY_EXCLUSIONS_CONTRACT_V1,
    QUALITY_EXCLUSIONS_CONTRACT_V2 as BUILDER_QUALITY_EXCLUSIONS_CONTRACT_V2,
    SEQUENCE_LENGTH_HARDWARE_EXCLUSION,
    SEQUENCE_LENGTH_HARDWARE_PLAN_CONTRACT,
    TRAIN_MEMBERSHIP_COMMITMENT_CONTRACT,
    V13_CONSENSUS_PAIR_MULTIPLIERS,
    V13_POLICY_DESIGN_PROVENANCE,
)
from scripts.qwen_supervision_leakage_guard import (  # noqa: E402
    post_event_supervision_reasons,
)


DRIVER_VERSION = "qwen-semantic-axes-transformers-qlora-driver-v1"
DATASET_CONTRACT_VERSION = "qwen-core-train-independent-ai-review-overlay-v1"
MEMBERSHIP_COMMITMENT_CONTRACT_VERSION = TRAIN_MEMBERSHIP_COMMITMENT_CONTRACT
QUALITY_EXCLUSIONS_CONTRACT_V1 = BUILDER_QUALITY_EXCLUSIONS_CONTRACT_V1
QUALITY_EXCLUSIONS_CONTRACT_V2 = BUILDER_QUALITY_EXCLUSIONS_CONTRACT_V2
SUPPORTED_QUALITY_EXCLUSION_CONTRACTS = frozenset(
    {QUALITY_EXCLUSIONS_CONTRACT_V1, QUALITY_EXCLUSIONS_CONTRACT_V2}
)
HARDWARE_EXCLUSION_REASON = SEQUENCE_LENGTH_HARDWARE_EXCLUSION
SOURCE_CONFLICT_EXCLUSION_REASON = "SOURCE_FIELD_CONFLICT"
TOKEN_AUDIT_MEASUREMENT_TOOL_VERSION = "qwen-train-token-audit-v2"
HARDWARE_PLAN_CONTRACT_VERSION = SEQUENCE_LENGTH_HARDWARE_PLAN_CONTRACT
TOKENIZER_BUNDLE_FILENAMES = frozenset(
    {
        "tokenizer.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "added_tokens.json",
        "vocab.json",
        "merges.txt",
        "chat_template.jinja",
    }
)
PAIR_MULTIPLIER_CONTRACT_VERSION = "qwen-core-pair-multipliers-v1"
RESOLUTION_MULTIPLIER_CONTRACT_VERSION = (
    "qwen-core-resolution-aware-pair-multipliers-v1"
)
V13_CURRICULUM_PRESET = "v13-train-only-ai-review-curriculum-v1"
V13_CURRICULUM_VERSION = "qwen-semantic-core-v13-train-only-curriculum-v1"
ADAPTER_CONTRACT_VERSION = "qwen-semantic-axes-adapter-contract-v1"
TARGET_CONTRACT = "core-v1"
MODEL_OUTPUT_CONTRACT = "core-axes-v1"
EXPECTED_MODEL_OUTPUT_CONTRACT = QWEN_WEAK_MODEL_OUTPUT_CONTRACT
EXPECTED_UNIQUE_MEMBERS = 729
EXPECTED_BASE_MODEL_ID = "Qwen/Qwen2.5-1.5B-Instruct"
LABEL_PROVENANCE = "INDEPENDENT_AI_REVIEW_CONSENSUS"
LABEL_CLASSIFICATION = "AI_REVIEW_NOT_HUMAN_GOLD"
TRAINING_MANIFEST_NAME = "training_manifest.json"
ADAPTER_CONTRACT_NAME = "adapter_training_contract.json"
FINAL_ADAPTER_DIR = "final_adapter"
BASE_MODEL_FULL_HASH_MAX_BYTES = 8 * 1024 * 1024
BASE_MODEL_SAMPLE_BYTES = 1024 * 1024
MAX_LENGTH_UPPER_BOUND = 8192
MAX_PAIR_MULTIPLIER = 100
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MODULE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
POLICY_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
FORBIDDEN_LEGACY_PATH_COMPONENT_RE = re.compile(
    r"(?:^|[._-])(?:dev|validation|sealed|holdout|benchmark)(?:$|[._-])",
    re.I,
)
EXPECTED_QWEN_PROFILE = {
    "model_type": "qwen2",
    "architecture": "Qwen2ForCausalLM",
    "hidden_size": 1536,
    "intermediate_size": 8960,
    "num_hidden_layers": 28,
    "num_attention_heads": 12,
    "num_key_value_heads": 2,
    "vocab_size": 151936,
}
QWEN_ALL_LINEAR_TARGETS = frozenset(
    {"q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"}
)
LEGAL_PAIR_KEYS = frozenset(
    f"{materiality}|{polarity}"
    for materiality in MATERIALITY
    for polarity in POLARITIES
)
REQUIRED_PACKAGE_VERSIONS = {
    "transformers": "5.15.1",
    "trl": "0.29.1",
    "peft": "0.19.1",
    "bitsandbytes": "0.50.1",
}
RECORDED_PACKAGE_NAMES = (
    "torch",
    "transformers",
    "trl",
    "peft",
    "bitsandbytes",
    "datasets",
    "accelerate",
    "safetensors",
)
PROHIBITED_SOURCE_KEYS = frozenset(
    {
        "adverse_strength",
        "candidate_prediction",
        "expected",
        "expected_output",
        "human_label",
        "label",
        "labels",
        "materiality",
        "model_output",
        "model_prediction",
        "old_label",
        "polarity",
        "qwen_output",
        "qwen_prediction",
        "qwen_predictions",
        "reason_codes",
        "reviewer_label",
        "reviewer_labels",
        "semantic_priority",
        "semantic_target",
        "target",
        "target_label",
        "weak_rule",
        "weak_truth",
    }
)
PROHIBITED_SOURCE_TEXT = (
    re.compile(
        r"\b(?:MATERIAL_ADVERSE|NOT_MATERIAL_ADVERSE|PRIORITY_REVIEW|"
        r"UNDECIDABLE)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:qwen|model|candidate|reviewer)[ _-]prediction\s*[:=]",
        re.I,
    ),
)


@dataclass
class DatasetAudit:
    report: dict[str, Any]
    examples: list[dict[str, Any]]
    hardware_exclusions: dict[str, dict[str, Any]]
    hardware_examples: dict[str, dict[str, Any]]
    unique_rows_by_id: dict[str, dict[str, Any]]


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sample_ids_sha256(sample_ids: set[str]) -> str:
    return sha256_bytes(stable_json(sorted(sample_ids)).encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_loads_strict(text: str, *, label: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{label} is not strict JSON: {exc}") from exc


def _read_utf8(path: Path, *, label: str) -> tuple[str, bytes]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} is missing: {path}")
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig"), raw
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} is not UTF-8: {path}") from exc


def _verify_sha(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} SHA256 is not text")
    normalized = value.strip().lower()
    if not SHA256_RE.fullmatch(normalized):
        raise ValueError(f"{label} SHA256 is invalid")
    return normalized


def _verify_sidecar(path: Path, digest: str) -> dict[str, Any]:
    sidecar = path.with_name(path.name + ".sha256")
    text, raw = _read_utf8(sidecar, label=f"{path.name} SHA256 sidecar")
    expected = f"{digest}  {path.name}\n"
    if text != expected:
        raise ValueError(f"{path.name} SHA256 sidecar mismatch")
    return {
        "filename": sidecar.name,
        "sha256": sha256_bytes(raw),
    }


def _validate_safetensors_file(path: Path, *, label: str) -> dict[str, Any]:
    """Validate a safetensors header without materializing tensor payloads."""

    try:
        from safetensors import safe_open
    except ImportError as exc:
        raise ValueError("safetensors is required for weight preflight") from exc
    try:
        with safe_open(str(path), framework="pt", device="cpu") as handle:
            keys = list(handle.keys())
            metadata = dict(handle.metadata() or {})
    except Exception as exc:
        raise ValueError(f"{label} has an invalid safetensors header") from exc
    if not keys or any(not isinstance(key, str) or not key for key in keys):
        raise ValueError(f"{label} contains no valid tensor names")
    return {
        "filename": path.name,
        "tensor_count": len(keys),
        "metadata": metadata,
    }


def _walk_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key).strip().casefold())
            keys.update(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_walk_keys(child))
    return keys


def _walk_strings(value: Any) -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for child in value.values():
            result.extend(_walk_strings(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_walk_strings(child))
    elif isinstance(value, str):
        result.append(value)
    return result


def _sampled_file_fingerprint(path: Path) -> dict[str, Any]:
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
    if not path.is_dir():
        raise FileNotFoundError(f"base model directory missing: {path}")
    files = sorted(item for item in path.rglob("*") if item.is_file())
    if not files:
        raise ValueError(f"base model directory contains no files: {path}")
    entries = [
        {
            "path": item.relative_to(path).as_posix(),
            **_sampled_file_fingerprint(item),
        }
        for item in files
    ]
    return {
        "scheme": "sha256-directory-manifest-full-small-head-tail-large-v1",
        "full_hash_max_bytes": BASE_MODEL_FULL_HASH_MAX_BYTES,
        "sample_bytes": BASE_MODEL_SAMPLE_BYTES,
        "sha256": sha256_bytes(stable_json(entries).encode("utf-8")),
        "file_count": len(entries),
        "total_bytes": sum(entry["bytes"] for entry in entries),
        "files": entries,
    }


def _base_model_weights_sha256(path: Path) -> str:
    single_weights = path / "model.safetensors"
    if single_weights.is_file():
        return sha256_file(single_weights)
    index_path = path / "model.safetensors.index.json"
    text, _raw = _read_utf8(index_path, label="base model safetensors index")
    index = _json_loads_strict(text, label="base model safetensors index")
    weight_map = index.get("weight_map") if isinstance(index, dict) else None
    if not isinstance(weight_map, dict) or not weight_map:
        raise ValueError("base model safetensors index has no weight_map")
    names = sorted(set(weight_map.values()))
    if any(
        not isinstance(name, str)
        or Path(name).name != name
        or not name.endswith(".safetensors")
        for name in names
    ):
        raise ValueError("base model safetensors index has invalid shard names")
    entries = []
    for name in names:
        shard = path / name
        if not shard.is_file():
            raise FileNotFoundError(f"base model weight shard is missing: {shard}")
        entries.append(
            {
                "path": name,
                "bytes": shard.stat().st_size,
                "sha256": sha256_file(shard),
            }
        )
    return sha256_bytes(stable_json(entries).encode("utf-8"))


def _tokenizer_bundle_fingerprint(path: Path) -> dict[str, Any]:
    entries = []
    for filename in sorted(TOKENIZER_BUNDLE_FILENAMES):
        item = path / filename
        if item.is_file():
            entries.append(
                {
                    "path": filename,
                    "bytes": item.stat().st_size,
                    "sha256": sha256_file(item),
                }
            )
    present = {entry["path"] for entry in entries}
    if not {"tokenizer.json", "tokenizer_config.json"} <= present:
        raise ValueError("tokenizer fingerprint is missing required files")
    return {
        "scheme": "sha256-tokenizer-bundle-allowlist-v1",
        "allowlist": sorted(TOKENIZER_BUNDLE_FILENAMES),
        "sha256": sha256_bytes(stable_json(entries).encode("utf-8")),
        "file_count": len(entries),
        "total_bytes": sum(entry["bytes"] for entry in entries),
        "files": entries,
    }


def _load_base_model_profile(path: Path) -> dict[str, Any]:
    config_path = path / "config.json"
    text, raw = _read_utf8(config_path, label="base model config.json")
    value = _json_loads_strict(text, label="base model config.json")
    if not isinstance(value, dict):
        raise ValueError("base model config.json must be an object")
    architectures = value.get("architectures")
    if isinstance(architectures, str):
        architectures = [architectures]
    if not isinstance(architectures, list):
        raise ValueError("base model architectures are missing")
    actual = {
        "model_type": value.get("model_type"),
        "architecture": EXPECTED_QWEN_PROFILE["architecture"],
        "hidden_size": value.get("hidden_size"),
        "intermediate_size": value.get("intermediate_size"),
        "num_hidden_layers": value.get("num_hidden_layers"),
        "num_attention_heads": value.get("num_attention_heads"),
        "num_key_value_heads": value.get("num_key_value_heads"),
        "vocab_size": value.get("vocab_size"),
    }
    if EXPECTED_QWEN_PROFILE["architecture"] not in architectures:
        actual["architecture"] = None
    if actual != EXPECTED_QWEN_PROFILE:
        raise ValueError(
            "base model config does not match Qwen2.5-1.5B-Instruct profile"
        )
    single_weights = path / "model.safetensors"
    index_path = path / "model.safetensors.index.json"
    if single_weights.is_file():
        weight_validation = {
            "layout": "SINGLE_SAFETENSORS",
            "shards": [
                _validate_safetensors_file(
                    single_weights, label="base model weights"
                )
            ],
        }
    elif index_path.is_file():
        index_text, index_raw = _read_utf8(
            index_path, label="base model safetensors index"
        )
        index = _json_loads_strict(
            index_text, label="base model safetensors index"
        )
        weight_map = index.get("weight_map") if isinstance(index, dict) else None
        if not isinstance(weight_map, dict) or not weight_map:
            raise ValueError("base model safetensors index has no weight_map")
        shard_names = sorted(set(weight_map.values()))
        if any(
            not isinstance(name, str)
            or Path(name).name != name
            or not name.endswith(".safetensors")
            for name in shard_names
        ):
            raise ValueError("base model safetensors index has invalid shard names")
        shards = [
            _validate_safetensors_file(
                path / name, label=f"base model weight shard {name}"
            )
            for name in shard_names
        ]
        weight_validation = {
            "layout": "SHARDED_SAFETENSORS",
            "index_sha256": sha256_bytes(index_raw),
            "tensor_count_declared": len(weight_map),
            "shards": shards,
        }
    else:
        raise ValueError("base model must use safetensors weights for safe preflight")
    for required in ("tokenizer_config.json", "tokenizer.json"):
        if not (path / required).is_file():
            raise ValueError(f"base model is missing {required}")
    return {
        **actual,
        "config_sha256": sha256_bytes(raw),
        "declared_model_id": EXPECTED_BASE_MODEL_ID,
        "weight_validation": weight_validation,
    }


def adapter_fingerprint(path: Path) -> dict[str, Any]:
    if not path.is_dir():
        raise FileNotFoundError(f"initial adapter directory missing: {path}")
    config = path / "adapter_config.json"
    weights = [
        path / name
        for name in ("adapter_model.safetensors", "adapter_model.bin")
        if (path / name).is_file()
    ]
    if not config.is_file() or not weights:
        raise ValueError(
            "initial adapter must contain adapter_config.json and adapter weights"
        )
    files = [config, *weights]
    entries = [
        {
            "path": item.name,
            "bytes": item.stat().st_size,
            "sha256": sha256_file(item),
        }
        for item in files
    ]
    return {
        "scheme": "sha256-peft-adapter-files-v1",
        "sha256": sha256_bytes(stable_json(entries).encode("utf-8")),
        "file_count": len(entries),
        "total_bytes": sum(entry["bytes"] for entry in entries),
        "files": entries,
    }


def _parse_adapter_config(path: Path, base_model: Path) -> dict[str, Any]:
    config_path = path / "adapter_config.json"
    text, raw = _read_utf8(config_path, label="initial adapter config")
    value = _json_loads_strict(text, label="initial adapter config")
    if not isinstance(value, dict):
        raise ValueError("initial adapter config must be an object")
    if str(value.get("peft_type") or "").upper() != "LORA":
        raise ValueError("initial adapter must be a LoRA adapter")
    if str(value.get("task_type") or "").upper() != "CAUSAL_LM":
        raise ValueError("initial adapter task_type must be CAUSAL_LM")
    if value.get("bias") not in {None, "none"}:
        raise ValueError("initial adapter bias mode must be none")
    bound_base = str(value.get("base_model_name_or_path") or "").strip()
    if not bound_base:
        raise ValueError("initial adapter base model binding is missing")
    bound_path = Path(bound_base)
    if bound_path.is_absolute():
        if bound_path.resolve() != base_model.resolve():
            raise ValueError("initial adapter base model path mismatch")
    elif bound_base not in {EXPECTED_BASE_MODEL_ID, base_model.name}:
        raise ValueError("initial adapter base model identity mismatch")
    targets = value.get("target_modules")
    if isinstance(targets, str):
        targets = [targets]
    if not isinstance(targets, list) or not targets or not all(
        isinstance(item, str) and MODULE_NAME_RE.fullmatch(item) for item in targets
    ):
        raise ValueError("initial adapter target_modules are invalid")
    if not set(targets) <= QWEN_ALL_LINEAR_TARGETS:
        raise ValueError("initial adapter targets are not Qwen linear modules")
    r = value.get("r")
    alpha = value.get("lora_alpha")
    dropout = value.get("lora_dropout")
    if isinstance(r, bool) or not isinstance(r, int) or r < 1:
        raise ValueError("initial adapter LoRA rank is invalid")
    if isinstance(alpha, bool) or not isinstance(alpha, int) or alpha < 1:
        raise ValueError("initial adapter LoRA alpha is invalid")
    if isinstance(dropout, bool) or not isinstance(dropout, (int, float)):
        raise ValueError("initial adapter LoRA dropout is invalid")
    dropout = float(dropout)
    if not 0 <= dropout < 1:
        raise ValueError("initial adapter LoRA dropout is invalid")
    safetensors_weights = path / "adapter_model.safetensors"
    if not safetensors_weights.is_file():
        raise ValueError(
            "initial adapter must use safetensors weights for safe preflight"
        )
    weight_validation = _validate_safetensors_file(
        safetensors_weights, label="initial adapter weights"
    )
    return {
        "config_sha256": sha256_bytes(raw),
        "base_model_name_or_path": bound_base,
        "r": r,
        "lora_alpha": alpha,
        "lora_dropout": dropout,
        "target_modules": sorted(set(targets)),
        "use_rslora": bool(value.get("use_rslora", False)),
        "task_type": "CAUSAL_LM",
        "peft_type": "LORA",
        "weight_validation": weight_validation,
    }


def _probe_environment() -> dict[str, Any]:
    packages: dict[str, str | None] = {}
    for name in RECORDED_PACKAGE_NAMES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    mismatches = {
        name: {"expected": expected, "actual": packages.get(name)}
        for name, expected in REQUIRED_PACKAGE_VERSIONS.items()
        if packages.get(name) != expected
    }
    if mismatches:
        raise ValueError(
            "unsupported training package versions: " + stable_json(mismatches)
        )
    for required in ("torch", "datasets", "accelerate", "safetensors"):
        if packages.get(required) is None:
            raise ValueError(f"required training package is missing: {required}")
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("nvidia-smi GPU probe failed") from exc
    if completed.returncode != 0:
        raise ValueError("nvidia-smi GPU probe failed")
    gpus: list[dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",", 3)]
        if len(parts) != 4:
            raise ValueError("nvidia-smi returned an unexpected row")
        try:
            index = int(parts[0])
            memory_mib = int(parts[2])
        except ValueError as exc:
            raise ValueError("nvidia-smi returned invalid numeric fields") from exc
        gpus.append(
            {
                "index": index,
                "name": parts[1],
                "memory_mib": memory_mib,
                "driver_version": parts[3],
            }
        )
    if not gpus:
        raise ValueError("no NVIDIA GPU was reported")
    selected = gpus[0]
    if "RTX 4060" not in selected["name"]:
        raise ValueError("GPU 0 is not an RTX 4060")
    if not 7000 <= selected["memory_mib"] <= 9000:
        raise ValueError("GPU 0 does not match the expected 8GB memory profile")
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "executable": sys.executable,
        "packages": packages,
        "required_exact_versions": dict(REQUIRED_PACKAGE_VERSIONS),
        "nvidia_smi": {"selected_gpu_index": 0, "gpus": gpus},
    }


def _strict_sample_id(value: Any, *, line_number: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"dataset line {line_number} has invalid sample_id")
    if len(value) > 200:
        raise ValueError(f"dataset line {line_number} sample_id is too long")
    return value


def _strict_messages(row: dict[str, Any], *, line_number: int) -> list[dict[str, str]]:
    if set(row) != {"messages", "metadata"}:
        raise ValueError(
            f"dataset line {line_number} must contain only messages and metadata"
        )
    messages = row.get("messages")
    if not isinstance(messages, list) or len(messages) != 3:
        raise ValueError(f"dataset line {line_number} has invalid messages")
    for index, (message, role) in enumerate(
        zip(messages, ("system", "user", "assistant")), start=1
    ):
        if (
            not isinstance(message, dict)
            or set(message) != {"role", "content"}
            or message.get("role") != role
            or not isinstance(message.get("content"), str)
        ):
            raise ValueError(
                f"dataset line {line_number} message {index} is invalid"
            )
    return messages


def _validate_source_content(
    user_text: str, metadata: dict[str, Any], *, line_number: int
) -> None:
    value = _json_loads_strict(
        user_text, label=f"dataset line {line_number} user content"
    )
    if not isinstance(value, dict) or not value:
        raise ValueError(f"dataset line {line_number} user content is invalid")
    prohibited = sorted(_walk_keys(value) & PROHIBITED_SOURCE_KEYS)
    if prohibited:
        raise ValueError(
            f"dataset line {line_number} contains prohibited source keys: "
            + ",".join(prohibited)
        )
    if any(
        pattern.search(text)
        for text in _walk_strings(value)
        for pattern in PROHIBITED_SOURCE_TEXT
    ):
        raise ValueError(
            f"dataset line {line_number} contains prohibited source supervision text"
        )
    outcome_reasons = post_event_supervision_reasons(value)
    if outcome_reasons:
        raise ValueError(
            f"dataset line {line_number} contains prohibited post-event "
            "supervision: " + ",".join(outcome_reasons)
        )
    actual = sha256_bytes(stable_json(value).encode("utf-8"))
    stored = _verify_sha(
        metadata.get("content_sha256"),
        label=f"dataset line {line_number} content",
    )
    if actual != stored:
        raise ValueError(f"dataset line {line_number} content SHA256 mismatch")


def _counter_dict(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items()))


def _validated_count_mapping(
    value: Any,
    *,
    label: str,
    allowed_keys: set[str] | frozenset[str] | None = None,
) -> Counter[str]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    counts: Counter[str] = Counter()
    for key, count in value.items():
        if (
            not isinstance(key, str)
            or not key
            or (allowed_keys is not None and key not in allowed_keys)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
        ):
            raise ValueError(f"{label} contains an invalid count")
        counts[key] = count
    return counts


def _validate_distribution_payload(
    value: Any, *, label: str, expected_row_count: int
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "row_count",
        "materiality",
        "polarity",
        "pair",
        "semantic_priority",
    }:
        raise ValueError(f"{label} is invalid")
    if value.get("row_count") != expected_row_count:
        raise ValueError(f"{label} row count mismatch")
    dimensions = {
        "materiality": frozenset(MATERIALITY),
        "polarity": frozenset(POLARITIES),
        "pair": LEGAL_PAIR_KEYS,
        "semantic_priority": None,
    }
    for field, allowed in dimensions.items():
        counts = _validated_count_mapping(
            value.get(field),
            label=f"{label} {field}",
            allowed_keys=allowed,
        )
        if sum(counts.values()) != expected_row_count:
            raise ValueError(f"{label} {field} counts do not close")
    return value


def _audit_unique_sibling(
    dataset_manifest: Path,
    manifest: dict[str, Any],
    quality_exclusions: dict[str, Any],
) -> dict[str, Any]:
    outputs = manifest.get("outputs")
    binding = outputs.get("unique_audit") if isinstance(outputs, dict) else None
    binding_fields = {
        "filename",
        "row_count",
        "sample_ids_sha256",
        "sha256",
        "sidecar",
        "sidecar_sha256",
    }
    if not isinstance(binding, dict) or set(binding) != binding_fields:
        raise ValueError("dataset manifest unique audit output binding is invalid")
    filename = binding.get("filename")
    if (
        not isinstance(filename, str)
        or not filename
        or Path(filename).name != filename
    ):
        raise ValueError("dataset manifest unique audit filename is invalid")
    unique_path = dataset_manifest.parent / filename
    text, raw = _read_utf8(unique_path, label="unique audit sibling")
    digest = sha256_bytes(raw)
    if digest != _verify_sha(
        binding.get("sha256"), label="dataset manifest unique audit output"
    ):
        raise ValueError("unique audit sibling SHA256 does not match manifest")
    sidecar = _verify_sidecar(unique_path, digest)
    if (
        binding.get("sidecar") != sidecar["filename"]
        or _verify_sha(
            binding.get("sidecar_sha256"),
            label="dataset manifest unique audit sidecar",
        )
        != sidecar["sha256"]
    ):
        raise ValueError("unique audit sibling sidecar binding mismatch")

    inputs = manifest.get("inputs")
    train_sft_input = inputs.get("train_sft") if isinstance(inputs, dict) else None
    if not isinstance(train_sft_input, dict):
        raise ValueError("dataset manifest train_sft input binding is missing")
    train_sft_sha256 = _verify_sha(
        train_sft_input.get("sha256"), label="dataset manifest train_sft input"
    )
    quality_contract = quality_exclusions["contract_version"]
    quality_input = quality_exclusions.get("input_file")
    quality_input_sha256 = (
        _verify_sha(quality_input.get("sha256"), label="quality exclusion input")
        if isinstance(quality_input, dict)
        else None
    )

    rows_by_id: dict[str, dict[str, Any]] = {}
    numeric_ids: set[str] = set()
    quality_ids: set[str] = set()
    hardware_exclusions: dict[str, dict[str, Any]] = {}
    hardware_examples: dict[str, dict[str, Any]] = {}
    materiality_counts: Counter[str] = Counter()
    polarity_counts: Counter[str] = Counter()
    pair_counts: Counter[str] = Counter()
    priority_counts: Counter[str] = Counter()
    resolution_counts: Counter[str] = Counter()
    quality_reason_counts: Counter[str] = Counter()
    row_count = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        row_count += 1
        row = _json_loads_strict(
            line, label=f"unique audit sibling line {line_number}"
        )
        if not isinstance(row, dict):
            raise ValueError(
                f"unique audit sibling line {line_number} is not an object"
            )
        messages = _strict_messages(row, line_number=line_number)
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError(
                f"unique audit sibling line {line_number} metadata is invalid"
            )
        sample_id = _strict_sample_id(
            metadata.get("sample_id"), line_number=line_number
        )
        if sample_id in rows_by_id:
            raise ValueError(f"unique audit sibling duplicate sample_id: {sample_id}")
        required_metadata = {
            "split": "TRAIN",
            "target_contract": TARGET_CONTRACT,
            "model_output_contract": MODEL_OUTPUT_CONTRACT,
            "overlay_contract_version": DATASET_CONTRACT_VERSION,
            "overlay_view": "UNIQUE_AUDIT",
            "label_provenance": LABEL_PROVENANCE,
            "label_classification": LABEL_CLASSIFICATION,
            "human_gold_claimed": False,
            "qwen_prediction_included": False,
            "post_event_market_data_included": False,
            "evidence_state_used_as_model_target": False,
            "original_weak_truth_used": False,
            "source_payload_binding_verified": True,
        }
        for field, expected in required_metadata.items():
            if metadata.get(field) != expected:
                raise ValueError(
                    f"unique audit sibling line {line_number} metadata mismatch: "
                    f"{field}"
                )
        if (
            metadata.get("prompt_version") != QWEN_WEAK_PROMPT_VERSION
            or metadata.get("prompt_sha256") != QWEN_WEAK_PROMPT_SHA256
            or messages[0]["content"] != QWEN_WEAK_SYSTEM_PROMPT
            or sha256_bytes(messages[0]["content"].encode("utf-8"))
            != QWEN_WEAK_PROMPT_SHA256
        ):
            raise ValueError(
                f"unique audit sibling line {line_number} prompt binding mismatch"
            )
        _validate_source_content(
            messages[1]["content"], metadata, line_number=line_number
        )
        target = _json_loads_strict(
            messages[2]["content"],
            label=f"unique audit sibling line {line_number} assistant target",
        )
        if not isinstance(target, dict) or set(target) != {
            "materiality",
            "polarity",
        }:
            raise ValueError(
                f"unique audit sibling line {line_number} target is invalid"
            )
        materiality = target.get("materiality")
        polarity = target.get("polarity")
        if materiality not in MATERIALITY or polarity not in POLARITIES:
            raise ValueError(
                f"unique audit sibling line {line_number} axes are invalid"
            )
        semantic_target = expected_semantic_payload(materiality, polarity)
        if metadata.get("semantic_target") != semantic_target:
            raise ValueError(
                f"unique audit sibling line {line_number} semantic_target mismatch"
            )
        review_resolution = metadata.get("review_resolution")
        decision_source = (
            review_resolution.get("decision_source")
            if isinstance(review_resolution, dict)
            else None
        )
        if decision_source not in {"A_B_CONSENSUS", "C_ARBITRATION"}:
            raise ValueError(
                f"unique audit sibling line {line_number} review resolution is invalid"
            )
        eligibility = metadata.get("training_eligibility")
        if not isinstance(eligibility, dict) or set(eligibility) != {
            "eligible",
            "exclusion_reason",
            "labels_rewritten",
            "pair_multiplier",
        }:
            raise ValueError(
                f"unique audit sibling line {line_number} eligibility is invalid"
            )
        eligible = eligibility.get("eligible")
        exclusion_reason = eligibility.get("exclusion_reason")
        multiplier = eligibility.get("pair_multiplier")
        if (
            not isinstance(eligible, bool)
            or eligibility.get("labels_rewritten") is not False
            or isinstance(multiplier, bool)
            or not isinstance(multiplier, int)
            or multiplier < 1
            or (eligible and exclusion_reason is not None)
            or (
                not eligible
                and exclusion_reason
                not in {
                    NUMERIC_TABLE_EXCLUSION_REASON,
                    SOURCE_CONFLICT_EXCLUSION_REASON,
                    HARDWARE_EXCLUSION_REASON,
                }
            )
        ):
            raise ValueError(
                f"unique audit sibling line {line_number} eligibility is inconsistent"
            )
        overlay_inputs = metadata.get("overlay_input_sha256")
        if (
            not isinstance(overlay_inputs, dict)
            or _verify_sha(
                overlay_inputs.get("train_sft"),
                label=f"unique audit sibling line {line_number} train_sft input",
            )
            != train_sft_sha256
        ):
            raise ValueError(
                f"unique audit sibling line {line_number} train_sft binding mismatch"
            )

        quality = metadata.get("quality_exclusion")
        if quality is None:
            if exclusion_reason in {
                SOURCE_CONFLICT_EXCLUSION_REASON,
                HARDWARE_EXCLUSION_REASON,
            }:
                raise ValueError(
                    f"unique audit sibling line {line_number} quality evidence is missing"
                )
        else:
            if not isinstance(quality, dict):
                raise ValueError(
                    f"unique audit sibling line {line_number} quality exclusion is invalid"
                )
            reason_code = quality.get("reason_code")
            base_fields = {
                "contract_version",
                "label_classification",
                "reason_code",
                "reason",
                "input_sha256",
            }
            expected_fields = (
                base_fields | {"evidence"}
                if reason_code == HARDWARE_EXCLUSION_REASON
                else base_fields
            )
            reason = quality.get("reason")
            if (
                set(quality) != expected_fields
                or quality.get("contract_version") != quality_contract
                or quality.get("label_classification") != LABEL_CLASSIFICATION
                or reason_code
                not in {SOURCE_CONFLICT_EXCLUSION_REASON, HARDWARE_EXCLUSION_REASON}
                or reason_code != exclusion_reason
                or not isinstance(reason, str)
                or not reason
                or reason != reason.strip()
                or len(reason) > 1000
                or quality_input_sha256 is None
                or _verify_sha(
                    quality.get("input_sha256"),
                    label=f"unique audit sibling line {line_number} quality input",
                )
                != quality_input_sha256
            ):
                raise ValueError(
                    f"unique audit sibling line {line_number} quality exclusion is inconsistent"
                )
            if reason_code == HARDWARE_EXCLUSION_REASON:
                if quality_contract != QUALITY_EXCLUSIONS_CONTRACT_V2:
                    raise ValueError(
                        "v1 quality exclusions cannot carry hardware evidence"
                    )
                if not isinstance(quality.get("evidence"), dict):
                    raise ValueError(
                        f"unique audit sibling line {line_number} hardware evidence is invalid"
                    )
                hardware_exclusions[sample_id] = dict(quality["evidence"])
                hardware_examples[sample_id] = {
                    "prompt": [dict(messages[0]), dict(messages[1])],
                    "completion": [dict(messages[2])],
                }
            quality_ids.add(sample_id)
            quality_reason_counts[reason_code] += 1
        if exclusion_reason == NUMERIC_TABLE_EXCLUSION_REASON:
            if quality is not None:
                raise ValueError("numeric and quality exclusions overlap")
            source_structure = metadata.get("source_structure")
            if (
                not isinstance(source_structure, dict)
                or source_structure.get("numeric_table_dominated") is not True
            ):
                raise ValueError(
                    f"unique audit sibling line {line_number} numeric exclusion is unproven"
                )
            numeric_ids.add(sample_id)

        row_sha256 = sha256_bytes(stable_json(row).encode("utf-8"))
        source_sft_row_sha256 = metadata.get("source_sft_row_sha256")
        if source_sft_row_sha256 is not None:
            source_sft_row_sha256 = _verify_sha(
                source_sft_row_sha256,
                label=f"unique audit sibling line {line_number} source SFT row",
            )
        rows_by_id[sample_id] = {
            "row_sha256": row_sha256,
            "messages_sha256": sha256_bytes(
                stable_json(messages).encode("utf-8")
            ),
            "eligible": eligible,
            "exclusion_reason": exclusion_reason,
            "pair_multiplier": multiplier,
            "decision_source": decision_source,
            "pair": f"{materiality}|{polarity}",
            "source_sft_row_sha256": source_sft_row_sha256,
            "train_sft_sha256": train_sft_sha256,
        }
        materiality_counts[materiality] += 1
        polarity_counts[polarity] += 1
        pair_counts[f"{materiality}|{polarity}"] += 1
        priority_counts[semantic_target["semantic_priority"]] += 1
        resolution_counts[decision_source] += 1

    ids = set(rows_by_id)
    if row_count != EXPECTED_UNIQUE_MEMBERS or binding.get("row_count") != row_count:
        raise ValueError("unique audit sibling original row count mismatch")
    ids_sha256 = _sample_ids_sha256(ids)
    if ids_sha256 != _verify_sha(
        binding.get("sample_ids_sha256"),
        label="dataset manifest unique audit sample IDs",
    ):
        raise ValueError("unique audit sibling sample ID membership mismatch")
    if numeric_ids & quality_ids:
        raise ValueError("numeric and quality exclusion members overlap")
    if quality_ids != {
        sample_id
        for sample_id, row_info in rows_by_id.items()
        if row_info["exclusion_reason"]
        in {SOURCE_CONFLICT_EXCLUSION_REASON, HARDWARE_EXCLUSION_REASON}
    }:
        raise ValueError("unique audit quality exclusion membership is inconsistent")
    if quality_ids != set(hardware_exclusions) | {
        sample_id
        for sample_id, row_info in rows_by_id.items()
        if row_info["exclusion_reason"] == SOURCE_CONFLICT_EXCLUSION_REASON
    }:
        raise ValueError("unique audit quality evidence membership is incomplete")
    if len(quality_ids) != quality_exclusions["entry_count"]:
        raise ValueError("unique audit quality exclusion count mismatch")
    if _sample_ids_sha256(quality_ids) != quality_exclusions["sample_ids_sha256"]:
        raise ValueError("unique audit quality exclusion member hash mismatch")
    if quality_reason_counts != Counter(quality_exclusions["reason_code_counts"]):
        raise ValueError("unique audit quality exclusion reason counts mismatch")
    distribution = {
        "row_count": row_count,
        "materiality": _counter_dict(materiality_counts),
        "polarity": _counter_dict(polarity_counts),
        "pair": _counter_dict(pair_counts),
        "semantic_priority": _counter_dict(priority_counts),
    }
    return {
        "path": str(unique_path.resolve()),
        "filename": unique_path.name,
        "sha256": digest,
        "sidecar": sidecar,
        "row_count": row_count,
        "sample_ids_sha256": ids_sha256,
        "rows_by_id": rows_by_id,
        "sample_ids": ids,
        "numeric_ids": numeric_ids,
        "quality_ids": quality_ids,
        "hardware_exclusions": hardware_exclusions,
        "hardware_examples": hardware_examples,
        "distribution": distribution,
        "resolution_counts": _counter_dict(resolution_counts),
        "train_sft_sha256": train_sft_sha256,
    }


def _validate_membership_commitment(
    value: Any,
    *,
    original_ids: set[str],
    trainable_ids: set[str],
    excluded_ids: set[str],
    numeric_ids: set[str],
    quality_ids: set[str],
    quality_contract: str,
) -> dict[str, Any]:
    actual = {
        "original_unique": original_ids,
        "trainable_unique": trainable_ids,
        "excluded_complement": excluded_ids,
        "numeric_exclusions": numeric_ids,
        "quality_exclusions": quality_ids,
    }
    if value is None:
        if quality_contract != QUALITY_EXCLUSIONS_CONTRACT_V1:
            raise ValueError(
                "v2 quality exclusions require membership_commitment v2"
            )
        return {
            "validation_method": "LEGACY_UNIQUE_AUDIT_COMPLEMENT_RECOMPUTED_V1",
            "contract_version": None,
            "exclusion_classes_disjoint": not bool(numeric_ids & quality_ids),
            **{
                name: {
                    "count": len(ids),
                    "sample_ids_sha256": _sample_ids_sha256(ids),
                }
                for name, ids in actual.items()
            },
        }
    expected_fields = {
        "contract_version",
        *actual,
        "exclusion_classes_disjoint",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected_fields
        or value.get("contract_version")
        != MEMBERSHIP_COMMITMENT_CONTRACT_VERSION
        or value.get("exclusion_classes_disjoint") is not True
    ):
        raise ValueError("dataset manifest membership commitment is invalid")
    for name, ids in actual.items():
        commitment = value.get(name)
        if (
            not isinstance(commitment, dict)
            or set(commitment) != {"count", "sample_ids_sha256"}
            or commitment.get("count") != len(ids)
            or _verify_sha(
                commitment.get("sample_ids_sha256"),
                label=f"dataset manifest membership {name}",
            )
            != _sample_ids_sha256(ids)
        ):
            raise ValueError(
                f"dataset manifest membership commitment mismatch: {name}"
            )
    return {
        "validation_method": "MEMBERSHIP_COMMITMENT_V2_RECOMPUTED",
        **value,
    }


def _audit_training_dataset(
    dataset: Path,
    dataset_manifest: Path,
    *,
    expected_dataset_sha256: str | None,
    expected_manifest_sha256: str | None,
) -> DatasetAudit:
    manifest_text, manifest_raw = _read_utf8(
        dataset_manifest, label="dataset manifest"
    )
    manifest_sha256 = sha256_bytes(manifest_raw)
    if expected_manifest_sha256 is not None and manifest_sha256 != _verify_sha(
        expected_manifest_sha256, label="expected dataset manifest"
    ):
        raise ValueError("dataset manifest explicit SHA256 mismatch")
    manifest_sidecar = _verify_sidecar(dataset_manifest, manifest_sha256)
    manifest = _json_loads_strict(manifest_text, label="dataset manifest")
    if not isinstance(manifest, dict):
        raise ValueError("dataset manifest must be an object")
    if manifest.get("contract_version") != DATASET_CONTRACT_VERSION:
        raise ValueError("dataset manifest contract_version mismatch")
    if manifest.get("target_contract") != TARGET_CONTRACT:
        raise ValueError("dataset manifest target_contract mismatch")
    if manifest.get("model_output_contract") != MODEL_OUTPUT_CONTRACT:
        raise ValueError("dataset manifest model_output_contract mismatch")
    if manifest.get("label_provenance") != LABEL_PROVENANCE:
        raise ValueError("dataset manifest label_provenance mismatch")
    if manifest.get("label_classification") != LABEL_CLASSIFICATION:
        raise ValueError("dataset manifest label_classification mismatch")
    if manifest.get("human_gold_claimed") is not False:
        raise ValueError("dataset manifest must not claim human gold")
    if manifest.get("expected_unique_row_count") != EXPECTED_UNIQUE_MEMBERS:
        raise ValueError("dataset manifest original membership count mismatch")
    quality_exclusions = manifest.get("quality_exclusions")
    quality_exclusion_fields = {
        "enabled",
        "contract_version",
        "label_classification",
        "input_file",
        "entry_count",
        "sample_ids_sha256",
        "reason_code_counts",
    }
    if (
        not isinstance(quality_exclusions, dict)
        or set(quality_exclusions) != quality_exclusion_fields
        or quality_exclusions.get("contract_version")
        not in SUPPORTED_QUALITY_EXCLUSION_CONTRACTS
        or quality_exclusions.get("label_classification")
        != LABEL_CLASSIFICATION
        or not isinstance(quality_exclusions.get("enabled"), bool)
    ):
        raise ValueError("dataset manifest quality exclusions contract is invalid")
    quality_exclusion_count = quality_exclusions.get("entry_count")
    if (
        isinstance(quality_exclusion_count, bool)
        or not isinstance(quality_exclusion_count, int)
        or not 0 <= quality_exclusion_count <= EXPECTED_UNIQUE_MEMBERS
    ):
        raise ValueError("dataset manifest quality exclusion count is invalid")
    _verify_sha(
        quality_exclusions.get("sample_ids_sha256"),
        label="dataset manifest quality exclusion sample IDs",
    )
    quality_reason_counts = _validated_count_mapping(
        quality_exclusions.get("reason_code_counts"),
        label="dataset manifest quality exclusion reason codes",
        allowed_keys=frozenset(
            {SOURCE_CONFLICT_EXCLUSION_REASON, HARDWARE_EXCLUSION_REASON}
        ),
    )
    if sum(quality_reason_counts.values()) != quality_exclusion_count:
        raise ValueError(
            "dataset manifest quality exclusion reason counts do not close"
        )
    if (
        quality_exclusions["contract_version"] == QUALITY_EXCLUSIONS_CONTRACT_V1
        and quality_reason_counts[HARDWARE_EXCLUSION_REASON]
    ):
        raise ValueError("v1 quality exclusions cannot contain hardware exclusions")
    quality_input_file = quality_exclusions.get("input_file")
    if quality_exclusions["enabled"]:
        if (
            not isinstance(quality_input_file, dict)
            or set(quality_input_file) != {"filename", "sha256"}
            or not isinstance(quality_input_file.get("filename"), str)
            or not quality_input_file["filename"]
        ):
            raise ValueError(
                "dataset manifest quality exclusion input binding is invalid"
            )
        _verify_sha(
            quality_input_file.get("sha256"),
            label="dataset manifest quality exclusion input",
        )
    elif (
        quality_input_file is not None
        or quality_exclusion_count != 0
        or quality_reason_counts
        or quality_exclusions["sample_ids_sha256"] != _sample_ids_sha256(set())
    ):
        raise ValueError(
            "disabled dataset quality exclusions must have an empty binding"
        )
    prompt = manifest.get("prompt")
    if not isinstance(prompt, dict) or (
        prompt.get("version") != QWEN_WEAK_PROMPT_VERSION
        or prompt.get("sha256") != QWEN_WEAK_PROMPT_SHA256
        or prompt.get("system_message_binding_verified") is not True
    ):
        raise ValueError("dataset manifest prompt binding mismatch")
    isolation = manifest.get("isolation")
    required_false = (
        "original_weak_truth_used",
        "qwen_predictions_read",
        "dev_metrics_read",
        "market_results_read",
        "sealed_benchmark_read",
        "external_facts_read",
        "unclear_labels_rewritten",
    )
    if not isinstance(isolation, dict) or any(
        isolation.get(field) is not False for field in required_false
    ):
        raise ValueError("dataset manifest isolation boundary mismatch")
    outputs = manifest.get("outputs")
    output = outputs.get("trainable_balanced") if isinstance(outputs, dict) else None
    if not isinstance(output, dict):
        raise ValueError("dataset manifest trainable output binding missing")
    unique_audit = _audit_unique_sibling(
        dataset_manifest, manifest, quality_exclusions
    )

    dataset_text, dataset_raw = _read_utf8(dataset, label="training dataset")
    dataset_sha256 = sha256_bytes(dataset_raw)
    if expected_dataset_sha256 is not None and dataset_sha256 != _verify_sha(
        expected_dataset_sha256, label="expected dataset"
    ):
        raise ValueError("training dataset explicit SHA256 mismatch")
    if dataset.name != output.get("filename"):
        raise ValueError("training dataset filename does not match manifest")
    if dataset_sha256 != _verify_sha(
        output.get("sha256"), label="dataset manifest training output"
    ):
        raise ValueError("training dataset SHA256 does not match manifest")
    dataset_sidecar = _verify_sidecar(dataset, dataset_sha256)

    examples: list[dict[str, Any]] = []
    replica_ids: set[str] = set()
    grouped_replicas: dict[str, list[dict[str, Any]]] = defaultdict(list)
    materiality_counts: Counter[str] = Counter()
    polarity_counts: Counter[str] = Counter()
    pair_counts: Counter[str] = Counter()
    priority_counts: Counter[str] = Counter()
    resolution_effective_counts: Counter[str] = Counter()
    common_policy: str | None = None
    row_count = 0
    for line_number, line in enumerate(dataset_text.splitlines(), start=1):
        if not line.strip():
            continue
        row_count += 1
        row = _json_loads_strict(line, label=f"dataset line {line_number}")
        if not isinstance(row, dict):
            raise ValueError(f"dataset line {line_number} is not an object")
        messages = _strict_messages(row, line_number=line_number)
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError(f"dataset line {line_number} metadata is invalid")
        sample_id = _strict_sample_id(
            metadata.get("sample_id"), line_number=line_number
        )
        required_metadata = {
            "split": "TRAIN",
            "target_contract": TARGET_CONTRACT,
            "model_output_contract": MODEL_OUTPUT_CONTRACT,
            "overlay_contract_version": DATASET_CONTRACT_VERSION,
            "overlay_view": "TRAINABLE_BALANCED",
            "label_provenance": LABEL_PROVENANCE,
            "label_classification": LABEL_CLASSIFICATION,
            "human_gold_claimed": False,
            "qwen_prediction_included": False,
            "post_event_market_data_included": False,
            "evidence_state_used_as_model_target": False,
            "original_weak_truth_used": False,
            "source_payload_binding_verified": True,
            "quality_exclusion": None,
        }
        for field, expected in required_metadata.items():
            if metadata.get(field) != expected:
                raise ValueError(
                    f"dataset line {line_number} metadata mismatch: {field}"
                )
        if (
            metadata.get("prompt_version") != QWEN_WEAK_PROMPT_VERSION
            or metadata.get("prompt_sha256") != QWEN_WEAK_PROMPT_SHA256
            or messages[0]["content"] != QWEN_WEAK_SYSTEM_PROMPT
            or sha256_bytes(messages[0]["content"].encode("utf-8"))
            != QWEN_WEAK_PROMPT_SHA256
        ):
            raise ValueError(f"dataset line {line_number} prompt binding mismatch")
        _validate_source_content(
            messages[1]["content"], metadata, line_number=line_number
        )

        target = _json_loads_strict(
            messages[2]["content"],
            label=f"dataset line {line_number} assistant target",
        )
        if not isinstance(target, dict) or set(target) != {
            "materiality",
            "polarity",
        }:
            raise ValueError(
                f"dataset line {line_number} assistant target must be core-axes-v1"
            )
        materiality = target.get("materiality")
        polarity = target.get("polarity")
        if (
            not isinstance(materiality, str)
            or materiality not in MATERIALITY
        ):
            raise ValueError(f"dataset line {line_number} has invalid materiality")
        if (
            not isinstance(polarity, str)
            or polarity not in POLARITIES
        ):
            raise ValueError(f"dataset line {line_number} has invalid polarity")
        semantic_target = expected_semantic_payload(materiality, polarity)
        if validate_semantic_payload(semantic_target):
            raise RuntimeError("expected_semantic_payload returned an invalid payload")
        if metadata.get("semantic_target") != semantic_target:
            raise ValueError(
                f"dataset line {line_number} semantic_target is inconsistent"
            )

        review_resolution = metadata.get("review_resolution")
        if not isinstance(review_resolution, dict):
            raise ValueError(
                f"dataset line {line_number} review resolution is missing"
            )
        decision_source = review_resolution.get("decision_source")
        if decision_source not in {"A_B_CONSENSUS", "C_ARBITRATION"}:
            raise ValueError(
                f"dataset line {line_number} review decision_source is invalid"
            )

        eligibility = metadata.get("training_eligibility")
        if (
            not isinstance(eligibility, dict)
            or eligibility.get("eligible") is not True
            or eligibility.get("exclusion_reason") is not None
            or eligibility.get("labels_rewritten") is not False
        ):
            raise ValueError(
                f"dataset line {line_number} training eligibility is invalid"
            )
        multiplier = eligibility.get("pair_multiplier")
        if isinstance(multiplier, bool) or not isinstance(multiplier, int) or multiplier < 1:
            raise ValueError(f"dataset line {line_number} pair multiplier is invalid")
        replica = metadata.get("training_replica")
        if not isinstance(replica, dict) or set(replica) != {
            "replica_id",
            "source_unique_sample_id",
            "source_unique_row_sha256",
            "replica_index",
            "replica_count",
            "labels_rewritten",
        }:
            raise ValueError(f"dataset line {line_number} replica metadata is invalid")
        replica_id = _verify_sha(
            replica.get("replica_id"), label=f"dataset line {line_number} replica"
        )
        if replica_id in replica_ids:
            raise ValueError(f"dataset line {line_number} duplicate replica_id")
        replica_ids.add(replica_id)
        source_id = replica.get("source_unique_sample_id")
        if source_id != sample_id:
            raise ValueError(f"dataset line {line_number} replica source mismatch")
        _verify_sha(
            replica.get("source_unique_row_sha256"),
            label=f"dataset line {line_number} unique source row",
        )
        replica_index = replica.get("replica_index")
        replica_count = replica.get("replica_count")
        if (
            isinstance(replica_index, bool)
            or not isinstance(replica_index, int)
            or isinstance(replica_count, bool)
            or not isinstance(replica_count, int)
            or not 1 <= replica_index <= replica_count
            or replica_count != multiplier
            or replica.get("labels_rewritten") is not False
        ):
            raise ValueError(f"dataset line {line_number} replica index is invalid")
        policy = metadata.get("pair_multiplier_policy")
        if not isinstance(policy, dict):
            raise ValueError(f"dataset line {line_number} multiplier policy is missing")
        policy_sha = _verify_sha(
            policy.get("policy_sha256"),
            label=f"dataset line {line_number} multiplier policy",
        )
        expected_replica_id = sha256_bytes(
            (
                f"{policy_sha}\0{sample_id}\0{replica_index}\0{replica_count}"
            ).encode("utf-8")
        )
        if replica_id != expected_replica_id:
            raise ValueError(
                f"dataset line {line_number} replica_id formula mismatch"
            )
        if common_policy is None:
            common_policy = stable_json(policy)
        elif stable_json(policy) != common_policy:
            raise ValueError("dataset contains mixed pair multiplier policies")
        grouped_replicas[sample_id].append(
            {
                "index": replica_index,
                "count": replica_count,
                "messages_sha256": sha256_bytes(
                    stable_json(messages).encode("utf-8")
                ),
                "source_row_sha256": replica["source_unique_row_sha256"],
                "policy_sha256": policy_sha,
                "pair": f"{materiality}|{polarity}",
                "materiality": materiality,
                "polarity": polarity,
                "semantic_priority": semantic_target["semantic_priority"],
                "decision_source": decision_source,
            }
        )
        materiality_counts[materiality] += 1
        polarity_counts[polarity] += 1
        pair_counts[f"{materiality}|{polarity}"] += 1
        priority_counts[semantic_target["semantic_priority"]] += 1
        resolution_effective_counts[decision_source] += 1
        examples.append(
            {
                "prompt": [dict(messages[0]), dict(messages[1])],
                "completion": [dict(messages[2])],
            }
        )

    if row_count == 0:
        raise ValueError("training dataset is empty")
    if row_count != output.get("row_count"):
        raise ValueError("training dataset row count does not match manifest")
    unique_source_count = len(grouped_replicas)
    if unique_source_count != output.get("unique_source_row_count"):
        raise ValueError("training unique source count does not match manifest")
    actual_trainable_sample_ids_sha256 = _sample_ids_sha256(
        set(grouped_replicas)
    )
    if actual_trainable_sample_ids_sha256 != _verify_sha(
        output.get("sample_ids_sha256"),
        label="dataset manifest trainable sample IDs",
    ):
        raise ValueError("training sample ID membership does not match manifest")
    for sample_id, replicas in grouped_replicas.items():
        expected_count = replicas[0]["count"]
        if len(replicas) != expected_count or sorted(
            item["index"] for item in replicas
        ) != list(range(1, expected_count + 1)):
            raise ValueError(f"training replicas are incomplete for {sample_id}")
        for field in (
            "count",
            "messages_sha256",
            "source_row_sha256",
            "policy_sha256",
            "pair",
            "materiality",
            "polarity",
            "semantic_priority",
            "decision_source",
        ):
            if len({item[field] for item in replicas}) != 1:
                raise ValueError(
                    f"training replicas change {field} for {sample_id}"
                )
    trainable_ids = set(grouped_replicas)
    original_ids = set(unique_audit["sample_ids"])
    if not trainable_ids <= original_ids:
        raise ValueError("training sample IDs are outside unique audit membership")
    excluded_ids = original_ids - trainable_ids
    numeric_ids = set(unique_audit["numeric_ids"])
    quality_ids = set(unique_audit["quality_ids"])
    if excluded_ids != numeric_ids | quality_ids:
        raise ValueError(
            "unique audit complement does not equal numeric plus quality exclusions"
        )
    if numeric_ids & quality_ids:
        raise ValueError("numeric and quality exclusion commitments overlap")
    for sample_id, replicas in grouped_replicas.items():
        unique_row = unique_audit["rows_by_id"][sample_id]
        if unique_row["eligible"] is not True:
            raise ValueError(
                f"training member is excluded by unique audit: {sample_id}"
            )
        if replicas[0]["source_row_sha256"] != unique_row["row_sha256"]:
            raise ValueError(
                f"training replica unique row SHA256 mismatch: {sample_id}"
            )
        if replicas[0]["messages_sha256"] != unique_row["messages_sha256"]:
            raise ValueError(
                f"training replica messages differ from unique audit: {sample_id}"
            )
        if replicas[0]["count"] != unique_row["pair_multiplier"]:
            raise ValueError(
                f"training replica multiplier differs from unique audit: {sample_id}"
            )
    for sample_id in excluded_ids:
        if unique_audit["rows_by_id"][sample_id]["eligible"] is not False:
            raise ValueError(
                f"unique audit complement member is marked trainable: {sample_id}"
            )
    membership_report = _validate_membership_commitment(
        manifest.get("membership_commitment"),
        original_ids=original_ids,
        trainable_ids=trainable_ids,
        excluded_ids=excluded_ids,
        numeric_ids=numeric_ids,
        quality_ids=quality_ids,
        quality_contract=quality_exclusions["contract_version"],
    )

    manifest_policy = manifest.get("pair_multiplier_policy")
    policy_fields = {
        "contract_version",
        "policy_version",
        "multipliers",
        "resolution_multiplier_policy",
        "source",
        "preset",
        "policy_sha256",
        "input_file",
        "policy_design_provenance",
        "builder_runtime_input_isolation",
    }
    if not isinstance(manifest_policy, dict) or set(manifest_policy) != policy_fields:
        raise ValueError("dataset manifest multiplier policy is missing")
    if (
        manifest_policy.get("contract_version")
        != PAIR_MULTIPLIER_CONTRACT_VERSION
    ):
        raise ValueError("dataset manifest multiplier policy contract mismatch")
    policy_version = manifest_policy.get("policy_version")
    if not isinstance(policy_version, str) or not POLICY_VERSION_RE.fullmatch(
        policy_version
    ):
        raise ValueError("dataset manifest multiplier policy_version is invalid")
    policy_source = manifest_policy.get("source")
    if policy_source not in {"VERSIONED_PRESET", "EXPLICIT_JSON_FILE"}:
        raise ValueError("dataset manifest multiplier source is invalid")
    if policy_source == "VERSIONED_PRESET":
        if (
            not isinstance(manifest_policy.get("preset"), str)
            or manifest_policy.get("input_file") is not None
        ):
            raise ValueError("dataset manifest preset multiplier provenance is invalid")
    else:
        input_file = manifest_policy.get("input_file")
        if (
            manifest_policy.get("preset") is not None
            or not isinstance(input_file, dict)
            or set(input_file) != {"filename", "sha256"}
            or not isinstance(input_file.get("filename"), str)
            or not input_file["filename"]
        ):
            raise ValueError("dataset manifest file multiplier provenance is invalid")
        _verify_sha(
            input_file.get("sha256"), label="dataset manifest multiplier input"
        )
    preset = manifest_policy.get("preset")
    if policy_source == "EXPLICIT_JSON_FILE":
        expected_design_provenance = EXPLICIT_POLICY_DESIGN_PROVENANCE
    elif preset == V13_CURRICULUM_PRESET:
        expected_design_provenance = V13_POLICY_DESIGN_PROVENANCE
    elif preset == "neutral-1x-v1":
        expected_design_provenance = NEUTRAL_POLICY_DESIGN_PROVENANCE
    else:
        raise ValueError("dataset manifest preset is not a supported policy")
    if (
        manifest_policy.get("policy_design_provenance")
        != expected_design_provenance
    ):
        raise ValueError("dataset manifest policy design provenance mismatch")
    if (
        manifest_policy.get("builder_runtime_input_isolation")
        != BUILDER_RUNTIME_INPUT_ISOLATION
    ):
        raise ValueError("dataset manifest builder runtime isolation mismatch")
    multipliers = manifest_policy.get("multipliers")
    if not isinstance(multipliers, dict) or set(multipliers) != LEGAL_PAIR_KEYS:
        raise ValueError("dataset manifest multiplier mapping is missing")
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_PAIR_MULTIPLIER
        for value in multipliers.values()
    ):
        raise ValueError("dataset manifest multiplier value is invalid")
    if manifest_policy.get("preset") == V13_CURRICULUM_PRESET and (
        multipliers != V13_CONSENSUS_PAIR_MULTIPLIERS
    ):
        raise ValueError("dataset manifest v13 pair multiplier preset mismatch")
    if manifest_policy.get("preset") == "neutral-1x-v1" and set(
        multipliers.values()
    ) != {1}:
        raise ValueError("dataset manifest neutral pair multiplier preset mismatch")
    resolution_policy = manifest_policy.get("resolution_multiplier_policy")
    resolution_policy_fields = {
        "contract_version",
        "curriculum_version",
        "split_scope",
        "a_b_consensus_multiplier_source",
        "c_arbitration_fixed_multiplier",
    }
    if (
        not isinstance(resolution_policy, dict)
        or set(resolution_policy) != resolution_policy_fields
    ):
        raise ValueError("dataset manifest resolution multiplier policy is invalid")
    if (
        resolution_policy.get("contract_version")
        != RESOLUTION_MULTIPLIER_CONTRACT_VERSION
    ):
        raise ValueError("dataset manifest resolution multiplier contract mismatch")
    if resolution_policy.get("split_scope") != "TRAIN_ONLY":
        raise ValueError("dataset manifest resolution multiplier split scope mismatch")
    if (
        resolution_policy.get("a_b_consensus_multiplier_source")
        != "JOINT_PAIR_POLICY"
    ):
        raise ValueError("dataset manifest A/B multiplier source mismatch")
    curriculum_version = resolution_policy.get("curriculum_version")
    c_fixed_multiplier = resolution_policy.get("c_arbitration_fixed_multiplier")
    if curriculum_version is None:
        if c_fixed_multiplier is not None:
            raise ValueError("legacy resolution policy must not fix C multiplier")
    elif curriculum_version == V13_CURRICULUM_VERSION:
        if c_fixed_multiplier != 1:
            raise ValueError("v13 resolution policy must fix C multiplier at one")
    else:
        raise ValueError("dataset manifest resolution curriculum_version is invalid")
    if (
        curriculum_version == V13_CURRICULUM_VERSION
        and (
            policy_source != "VERSIONED_PRESET"
            or manifest_policy.get("preset") != V13_CURRICULUM_PRESET
        )
    ):
        raise ValueError("v13 resolution policy preset binding mismatch")
    if (
        curriculum_version is None
        and manifest_policy.get("preset") == V13_CURRICULUM_PRESET
    ):
        raise ValueError("v13 preset is missing its resolution curriculum")
    policy_sha256 = _verify_sha(
        manifest_policy.get("policy_sha256"), label="dataset manifest policy"
    )
    canonical_pair_policy = {
        key: manifest_policy.get(key)
        for key in ("contract_version", "policy_version", "multipliers")
    }
    canonical_policy = {
        "pair_multiplier_policy": canonical_pair_policy,
        "resolution_multiplier_policy": resolution_policy,
    }
    if sha256_bytes(stable_json(canonical_policy).encode("utf-8")) != policy_sha256:
        raise ValueError("dataset manifest multiplier policy SHA256 mismatch")
    row_policy = _json_loads_strict(
        common_policy or "{}", label="dataset row multiplier policy"
    )
    expected_row_policy = {
        key: value for key, value in manifest_policy.items() if key != "multipliers"
    }
    if row_policy != expected_row_policy:
        raise ValueError("dataset row multiplier policy does not match manifest")
    for sample_id, replicas in grouped_replicas.items():
        pair = replicas[0]["pair"]
        decision_source = replicas[0]["decision_source"]
        expected_multiplier = multipliers.get(pair)
        if decision_source == "C_ARBITRATION" and c_fixed_multiplier is not None:
            expected_multiplier = c_fixed_multiplier
        if expected_multiplier != replicas[0]["count"]:
            raise ValueError(f"dataset pair multiplier mismatch for {sample_id}")

    actual_distribution = {
        "row_count": row_count,
        "materiality": _counter_dict(materiality_counts),
        "polarity": _counter_dict(polarity_counts),
        "pair": _counter_dict(pair_counts),
        "semantic_priority": _counter_dict(priority_counts),
    }
    distributions = manifest.get("distributions")
    if not isinstance(distributions, dict) or set(distributions) != {
        "unique_audit",
        "trainable_unique",
        "trainable_effective",
    }:
        raise ValueError("dataset manifest distributions are invalid")
    expected_distribution = _validate_distribution_payload(
        distributions.get("trainable_effective"),
        label="dataset manifest trainable effective distribution",
        expected_row_count=row_count,
    )
    if actual_distribution != expected_distribution:
        raise ValueError("training label distribution does not match manifest")

    trainable_unique_materiality = Counter(
        replicas[0]["materiality"] for replicas in grouped_replicas.values()
    )
    trainable_unique_polarity = Counter(
        replicas[0]["polarity"] for replicas in grouped_replicas.values()
    )
    trainable_unique_pair = Counter(
        replicas[0]["pair"] for replicas in grouped_replicas.values()
    )
    trainable_unique_priority = Counter(
        replicas[0]["semantic_priority"] for replicas in grouped_replicas.values()
    )
    actual_trainable_unique_distribution = {
        "row_count": unique_source_count,
        "materiality": _counter_dict(trainable_unique_materiality),
        "polarity": _counter_dict(trainable_unique_polarity),
        "pair": _counter_dict(trainable_unique_pair),
        "semantic_priority": _counter_dict(trainable_unique_priority),
    }
    expected_trainable_unique_distribution = _validate_distribution_payload(
        distributions.get("trainable_unique"),
        label="dataset manifest trainable unique distribution",
        expected_row_count=unique_source_count,
    )
    if actual_trainable_unique_distribution != expected_trainable_unique_distribution:
        raise ValueError("training unique label distribution does not match manifest")
    unique_audit_distribution = _validate_distribution_payload(
        distributions.get("unique_audit"),
        label="dataset manifest unique audit distribution",
        expected_row_count=EXPECTED_UNIQUE_MEMBERS,
    )
    if unique_audit["distribution"] != unique_audit_distribution:
        raise ValueError("unique audit sibling distribution does not match manifest")
    excluded_unique_count = EXPECTED_UNIQUE_MEMBERS - unique_source_count
    if excluded_unique_count < 0:
        raise ValueError("training unique source count exceeds original membership")
    for field in ("materiality", "polarity", "pair", "semantic_priority"):
        audit_counts = Counter(unique_audit_distribution[field])
        trainable_counts = Counter(actual_trainable_unique_distribution[field])
        if any(trainable_counts[key] > audit_counts[key] for key in trainable_counts):
            raise ValueError(
                f"dataset manifest {field} trainable distribution exceeds audit"
            )
        if sum((audit_counts - trainable_counts).values()) != excluded_unique_count:
            raise ValueError(
                f"dataset manifest {field} exclusion distribution does not close"
            )

    resolution_unique_counts = Counter(
        replicas[0]["decision_source"] for replicas in grouped_replicas.values()
    )
    resolution_keys = frozenset({"A_B_CONSENSUS", "C_ARBITRATION"})
    original_resolution_counts = _validated_count_mapping(
        manifest.get("resolution_counts"),
        label="dataset manifest original resolution counts",
        allowed_keys=resolution_keys,
    )
    if sum(original_resolution_counts.values()) != EXPECTED_UNIQUE_MEMBERS:
        raise ValueError("dataset manifest original resolution counts do not close")
    if Counter(unique_audit["resolution_counts"]) != original_resolution_counts:
        raise ValueError("unique audit sibling resolution counts do not match manifest")

    trainability = manifest.get("trainability_policy")
    trainability_fields = {
        "materiality_allowed",
        "polarity_allowed",
        "unclear_training_enabled",
        "unclear_labels_rewritten",
        "original_unique_row_count",
        "trainable_unique_row_count",
        "excluded_unique_row_count",
        "excluded_effective_replica_count",
        "pre_exclusion_effective_row_count",
        "trainable_effective_row_count",
        "exclusion_reasons",
        "trainable_resolution_counts",
        "excluded_resolution_counts",
        "excluded_pair_resolution_counts",
        "source_structure_exclusion",
    }
    if not isinstance(trainability, dict) or set(trainability) != trainability_fields:
        raise ValueError("dataset manifest trainability policy is invalid")
    if (
        trainability.get("materiality_allowed") != sorted(MATERIALITY)
        or trainability.get("polarity_allowed") != sorted(POLARITIES)
        or trainability.get("unclear_training_enabled") is not True
        or trainability.get("unclear_labels_rewritten") is not False
        or trainability.get("original_unique_row_count")
        != EXPECTED_UNIQUE_MEMBERS
        or trainability.get("trainable_unique_row_count") != unique_source_count
        or trainability.get("excluded_unique_row_count") != excluded_unique_count
        or trainability.get("trainable_effective_row_count") != row_count
    ):
        raise ValueError("dataset manifest trainability counts mismatch")
    excluded_effective_count = trainability.get(
        "excluded_effective_replica_count"
    )
    pre_exclusion_effective_count = trainability.get(
        "pre_exclusion_effective_row_count"
    )
    if (
        isinstance(excluded_effective_count, bool)
        or not isinstance(excluded_effective_count, int)
        or excluded_effective_count < excluded_unique_count
        or excluded_effective_count
        > excluded_unique_count * max(multipliers.values())
        or isinstance(pre_exclusion_effective_count, bool)
        or not isinstance(pre_exclusion_effective_count, int)
        or pre_exclusion_effective_count != row_count + excluded_effective_count
    ):
        raise ValueError("dataset manifest excluded effective counts do not close")
    exclusion_reason_counts = _validated_count_mapping(
        trainability.get("exclusion_reasons"),
        label="dataset manifest exclusion reasons",
        allowed_keys=frozenset(
            {NUMERIC_TABLE_EXCLUSION_REASON, *QUALITY_EXCLUSION_REASON_CODES}
        ),
    )
    if sum(exclusion_reason_counts.values()) != excluded_unique_count:
        raise ValueError("dataset manifest exclusion reason counts do not close")
    derived_exclusion_reasons = Counter(
        unique_audit["rows_by_id"][sample_id]["exclusion_reason"]
        for sample_id in excluded_ids
    )
    if derived_exclusion_reasons != exclusion_reason_counts:
        raise ValueError(
            "unique audit exclusion reasons do not match trainability manifest"
        )
    manifested_quality_reason_counts = Counter(
        {
            reason_code: exclusion_reason_counts[reason_code]
            for reason_code in QUALITY_EXCLUSION_REASON_CODES
            if exclusion_reason_counts[reason_code]
        }
    )
    if manifested_quality_reason_counts != quality_reason_counts:
        raise ValueError(
            "dataset manifest quality exclusions do not match trainability reasons"
        )
    trainable_resolution_manifest = _validated_count_mapping(
        trainability.get("trainable_resolution_counts"),
        label="dataset manifest trainable resolution counts",
        allowed_keys=resolution_keys,
    )
    excluded_resolution_manifest = _validated_count_mapping(
        trainability.get("excluded_resolution_counts"),
        label="dataset manifest excluded resolution counts",
        allowed_keys=resolution_keys,
    )
    if trainable_resolution_manifest != resolution_unique_counts:
        raise ValueError("training review resolution counts do not match manifest")
    if sum(excluded_resolution_manifest.values()) != excluded_unique_count:
        raise ValueError("dataset manifest excluded resolution counts do not close")
    for key in resolution_keys:
        if (
            trainable_resolution_manifest[key]
            + excluded_resolution_manifest[key]
            != original_resolution_counts[key]
        ):
            raise ValueError("dataset manifest resolution exclusions do not close")
    allowed_pair_resolution_keys = frozenset(
        f"{decision_source}::{pair_key}"
        for decision_source in resolution_keys
        for pair_key in LEGAL_PAIR_KEYS
    )
    excluded_pair_resolution_manifest = _validated_count_mapping(
        trainability.get("excluded_pair_resolution_counts"),
        label="dataset manifest excluded pair-resolution counts",
        allowed_keys=allowed_pair_resolution_keys,
    )
    if sum(excluded_pair_resolution_manifest.values()) != excluded_unique_count:
        raise ValueError(
            "dataset manifest excluded pair-resolution counts do not close"
        )
    actual_excluded_resolution = Counter(
        unique_audit["rows_by_id"][sample_id]["decision_source"]
        for sample_id in excluded_ids
    )
    actual_excluded_pair_resolution = Counter(
        (
            f"{unique_audit['rows_by_id'][sample_id]['decision_source']}::"
            f"{unique_audit['rows_by_id'][sample_id]['pair']}"
        )
        for sample_id in excluded_ids
    )
    if actual_excluded_resolution != excluded_resolution_manifest:
        raise ValueError(
            "unique audit excluded resolution members do not match manifest"
        )
    if actual_excluded_pair_resolution != excluded_pair_resolution_manifest:
        raise ValueError(
            "unique audit excluded pair-resolution members do not match manifest"
        )
    derived_excluded_resolution: Counter[str] = Counter()
    derived_excluded_pair: Counter[str] = Counter()
    expected_excluded_effective_count = 0
    for pair_resolution_key, count in excluded_pair_resolution_manifest.items():
        decision_source, pair_key = pair_resolution_key.split("::", maxsplit=1)
        derived_excluded_resolution[decision_source] += count
        derived_excluded_pair[pair_key] += count
        multiplier = multipliers[pair_key]
        if decision_source == "C_ARBITRATION" and c_fixed_multiplier is not None:
            multiplier = c_fixed_multiplier
        expected_excluded_effective_count += count * multiplier
    expected_excluded_pair = Counter(unique_audit_distribution["pair"])
    expected_excluded_pair.subtract(actual_trainable_unique_distribution["pair"])
    if derived_excluded_resolution != excluded_resolution_manifest:
        raise ValueError(
            "dataset manifest excluded pair-resolution projection does not "
            "match resolution counts"
        )
    if derived_excluded_pair != expected_excluded_pair:
        raise ValueError(
            "dataset manifest excluded pair-resolution projection does not "
            "match pair counts"
        )
    if expected_excluded_effective_count != excluded_effective_count:
        raise ValueError(
            "dataset manifest excluded effective replica count does not match "
            "pair-resolution multipliers"
        )
    source_structure_exclusion = trainability.get("source_structure_exclusion")
    expected_source_structure_exclusion = {
        "enabled": manifest_policy.get("preset") == V13_CURRICULUM_PRESET,
        "reason": NUMERIC_TABLE_EXCLUSION_REASON,
        "stable_json_character_count_min": NUMERIC_TABLE_MIN_STABLE_JSON_CHARS,
        "digit_character_ratio_min": NUMERIC_TABLE_MIN_DIGIT_RATIO,
        "label_independent": True,
        "applies_to_preset": V13_CURRICULUM_PRESET,
    }
    if source_structure_exclusion != expected_source_structure_exclusion:
        raise ValueError("dataset manifest source structure exclusion mismatch")
    numeric_exclusion_count = exclusion_reason_counts[
        NUMERIC_TABLE_EXCLUSION_REASON
    ]
    if (
        not expected_source_structure_exclusion["enabled"]
        and numeric_exclusion_count != 0
    ):
        raise ValueError("numeric source exclusions require the v13 preset")

    curriculum = manifest.get("curriculum")
    curriculum_fields = {
        "enabled",
        "version",
        "split_scope",
        "label_classification",
        "unique_source_row_count",
        "a_b_clean_consensus",
        "c_arbitration",
        "effective_distribution",
        "input_isolation",
    }
    if not isinstance(curriculum, dict) or set(curriculum) != curriculum_fields:
        raise ValueError("dataset manifest curriculum is invalid")
    expected_curriculum = {
        "enabled": curriculum_version is not None,
        "version": curriculum_version,
        "split_scope": resolution_policy["split_scope"],
        "label_classification": LABEL_CLASSIFICATION,
        "unique_source_row_count": EXPECTED_UNIQUE_MEMBERS,
        "a_b_clean_consensus": {
            "unique_row_count": original_resolution_counts["A_B_CONSENSUS"],
            "effective_row_count": resolution_effective_counts["A_B_CONSENSUS"],
            "multiplier_source": resolution_policy[
                "a_b_consensus_multiplier_source"
            ],
            "joint_pair_multipliers": dict(sorted(multipliers.items())),
        },
        "c_arbitration": {
            "unique_row_count": original_resolution_counts["C_ARBITRATION"],
            "effective_row_count": resolution_effective_counts["C_ARBITRATION"],
            "fixed_multiplier": c_fixed_multiplier,
        },
        "effective_distribution": actual_distribution,
        "input_isolation": {
            "train_only": True,
            "dev_metrics_read": False,
            "qwen_predictions_read": False,
            "market_results_read": False,
            "sealed_benchmark_read": False,
        },
    }
    if curriculum != expected_curriculum:
        raise ValueError("dataset manifest curriculum does not match dataset")
    return DatasetAudit(
        report={
            "path": str(dataset.resolve()),
            "filename": dataset.name,
            "sha256": dataset_sha256,
            "sidecar": dataset_sidecar,
            "row_count": row_count,
            "unique_source_row_count": unique_source_count,
            "original_unique_row_count": EXPECTED_UNIQUE_MEMBERS,
            "excluded_unique_row_count": excluded_unique_count,
            "excluded_effective_replica_count": excluded_effective_count,
            "quality_exclusions": quality_exclusions,
            "unique_audit": {
                key: value
                for key, value in unique_audit.items()
                if key
                not in {
                    "rows_by_id",
                    "sample_ids",
                    "numeric_ids",
                    "quality_ids",
                    "hardware_exclusions",
                    "hardware_examples",
                }
            },
            "membership_closure": membership_report,
            "manifest": {
                "path": str(dataset_manifest.resolve()),
                "sha256": manifest_sha256,
                "sidecar": manifest_sidecar,
            },
            "contract_version": DATASET_CONTRACT_VERSION,
            "target_contract": TARGET_CONTRACT,
            "model_output_contract": MODEL_OUTPUT_CONTRACT,
            "prompt_version": QWEN_WEAK_PROMPT_VERSION,
            "prompt_sha256": QWEN_WEAK_PROMPT_SHA256,
            "label_provenance": LABEL_PROVENANCE,
            "label_classification": LABEL_CLASSIFICATION,
            "pair_multiplier_policy_sha256": policy_sha256,
            "policy_design_provenance": expected_design_provenance,
            "builder_runtime_input_isolation": dict(
                BUILDER_RUNTIME_INPUT_ISOLATION
            ),
            "resolution_multiplier_policy": resolution_policy,
            "curriculum": curriculum,
            "distribution": actual_distribution,
            "trainable_unique_distribution": (
                actual_trainable_unique_distribution
            ),
        },
        examples=examples,
        hardware_exclusions=unique_audit["hardware_exclusions"],
        hardware_examples=unique_audit["hardware_examples"],
        unique_rows_by_id=unique_audit["rows_by_id"],
    )


def _audit_contract_rows_only(path: Path) -> dict[str, Any]:
    lowered = path.name.casefold()
    if any(token in lowered for token in ("dev", "validation", "test", "sealed", "holdout", "benchmark")):
        raise ValueError("legacy adapter training dataset path is not TRAIN-only")
    if any(
        FORBIDDEN_LEGACY_PATH_COMPONENT_RE.search(component)
        for component in path.parts[:-1]
    ):
        raise ValueError("legacy adapter training dataset path crosses a forbidden split")
    text, raw = _read_utf8(path, label="legacy adapter training dataset")
    row_count = 0
    binding: dict[str, str] | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        row_count += 1
        row = _json_loads_strict(
            line, label=f"legacy adapter dataset line {line_number}"
        )
        if not isinstance(row, dict):
            raise ValueError("legacy adapter dataset row is not an object")
        messages = _strict_messages(row, line_number=line_number)
        metadata = row.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("split") != "TRAIN":
            raise ValueError("legacy adapter provenance dataset is not TRAIN-only")
        for field in (
            "qwen_prediction_included",
            "post_event_market_data_included",
            "evidence_state_used_as_model_target",
            "human_gold_claimed",
        ):
            if metadata.get(field) is not False:
                raise ValueError(
                    f"legacy adapter provenance boundary mismatch: {field}"
                )
        _validate_source_content(
            messages[1]["content"], metadata, line_number=line_number
        )
        current = {
            "target_contract": str(metadata.get("target_contract") or ""),
            "model_output_contract": str(
                metadata.get("model_output_contract") or ""
            ),
            "prompt_version": str(metadata.get("prompt_version") or ""),
            "prompt_sha256": str(metadata.get("prompt_sha256") or "").lower(),
        }
        if current != {
            "target_contract": TARGET_CONTRACT,
            "model_output_contract": MODEL_OUTPUT_CONTRACT,
            "prompt_version": QWEN_WEAK_PROMPT_VERSION,
            "prompt_sha256": QWEN_WEAK_PROMPT_SHA256,
        }:
            raise ValueError("legacy adapter provenance contract mismatch")
        if messages[0]["content"] != QWEN_WEAK_SYSTEM_PROMPT:
            raise ValueError("legacy adapter provenance prompt text mismatch")
        target = _json_loads_strict(
            messages[2]["content"], label="legacy adapter target"
        )
        if not isinstance(target, dict) or set(target) != {
            "materiality",
            "polarity",
        }:
            raise ValueError("legacy adapter target is not core-axes-v1")
        semantic = expected_semantic_payload(
            str(target.get("materiality") or ""), str(target.get("polarity") or "")
        )
        if metadata.get("semantic_target") != semantic:
            raise ValueError("legacy adapter semantic target mismatch")
        if binding is None:
            binding = current
        elif binding != current:
            raise ValueError("legacy adapter provenance dataset mixes contracts")
    if row_count == 0 or binding is None:
        raise ValueError("legacy adapter provenance dataset is empty")
    return {
        **binding,
        "path": str(path.resolve()),
        "sha256": sha256_bytes(raw),
        "row_count": row_count,
    }


def _audit_init_adapter_contract(
    adapter: Path,
    *,
    base_model: Path,
    base_fingerprint_sha256: str,
    adapter_fingerprint_sha256: str,
) -> dict[str, Any]:
    contract_path = adapter / ADAPTER_CONTRACT_NAME
    if contract_path.is_file():
        text, raw = _read_utf8(contract_path, label="initial adapter contract")
        contract_sha256 = sha256_bytes(raw)
        sidecar = _verify_sidecar(contract_path, contract_sha256)
        value = _json_loads_strict(text, label="initial adapter contract")
        if not isinstance(value, dict):
            raise ValueError("initial adapter contract must be an object")
        expected = {
            "schema_version": 1,
            "contract_version": ADAPTER_CONTRACT_VERSION,
            "training_driver_version": DRIVER_VERSION,
            "target_contract": TARGET_CONTRACT,
            "model_output_contract": MODEL_OUTPUT_CONTRACT,
            "prompt_version": QWEN_WEAK_PROMPT_VERSION,
            "prompt_sha256": QWEN_WEAK_PROMPT_SHA256,
            "base_model_fingerprint_sha256": base_fingerprint_sha256,
            "adapter_fingerprint_sha256": adapter_fingerprint_sha256,
            "label_classification": LABEL_CLASSIFICATION,
            "human_gold": False,
        }
        for field, expected_value in expected.items():
            if value.get(field) != expected_value:
                raise ValueError(f"initial adapter contract mismatch: {field}")
        _verify_sha(
            value.get("training_dataset_sha256"),
            label="initial adapter training dataset",
        )
        if value.get("training_mode") not in {
            "NEW_LORA",
            "RESUME_TRAINABLE_ADAPTER",
        }:
            raise ValueError("initial adapter contract has invalid training_mode")
        parent_fingerprint = value.get("initial_adapter_fingerprint_sha256")
        if parent_fingerprint is not None:
            _verify_sha(
                parent_fingerprint, label="initial adapter parent fingerprint"
            )
        return {
            "binding_method": "ADAPTER_TRAINING_CONTRACT_V1",
            "path": str(contract_path.resolve()),
            "sha256": contract_sha256,
            "sidecar": sidecar,
            "binding": expected,
        }

    args_path = adapter / "args.json"
    text, raw = _read_utf8(args_path, label="legacy initial adapter args.json")
    args_value = _json_loads_strict(text, label="legacy initial adapter args.json")
    if not isinstance(args_value, dict):
        raise ValueError("legacy initial adapter args.json must be an object")
    dataset_value = args_value.get("dataset")
    if isinstance(dataset_value, list):
        if len(dataset_value) != 1:
            raise ValueError(
                "legacy initial adapter must bind exactly one TRAIN dataset"
            )
        dataset_value = dataset_value[0]
    if not isinstance(dataset_value, str) or not dataset_value.strip():
        raise ValueError("legacy initial adapter TRAIN dataset binding is missing")
    declared_base = str(args_value.get("model") or "").strip()
    if not declared_base:
        raise ValueError("legacy initial adapter base model binding is missing")
    declared_path = Path(declared_base)
    if declared_path.is_absolute() and declared_path.resolve() != base_model.resolve():
        raise ValueError("legacy initial adapter base model mismatch")
    if not declared_path.is_absolute() and declared_base != EXPECTED_BASE_MODEL_ID:
        raise ValueError("legacy initial adapter base model identity mismatch")
    provenance_path = Path(dataset_value)
    if not provenance_path.is_absolute():
        provenance_path = args_path.parent / provenance_path
    provenance = _audit_contract_rows_only(provenance_path.resolve())
    return {
        "binding_method": "LEGACY_ARGS_TRAIN_DATASET_REVALIDATED",
        "args_path": str(args_path.resolve()),
        "args_sha256": sha256_bytes(raw),
        "train_dataset": provenance,
        "binding": {
            "target_contract": provenance["target_contract"],
            "model_output_contract": provenance["model_output_contract"],
            "prompt_version": provenance["prompt_version"],
            "prompt_sha256": provenance["prompt_sha256"],
            "base_model_fingerprint_sha256": base_fingerprint_sha256,
        },
    }


def _parse_target_modules(value: str | None) -> str | list[str] | None:
    if value is None:
        return None
    if value != value.strip() or not value:
        raise ValueError("target_modules is invalid")
    if value == "all-linear":
        return value
    items = [item.strip() for item in value.split(",")]
    if not items or any(
        not item or not MODULE_NAME_RE.fullmatch(item) for item in items
    ):
        raise ValueError("target_modules must be all-linear or comma-separated names")
    if len(set(items)) != len(items):
        raise ValueError("target_modules contains duplicates")
    if not set(items) <= QWEN_ALL_LINEAR_TARGETS:
        raise ValueError("target_modules contains an unknown Qwen module")
    return sorted(items)


def _resolve_hyperparameters(
    *,
    adapter_config: dict[str, Any] | None,
    seed: int,
    lora_r: int | None,
    lora_alpha: int | None,
    lora_dropout: float | None,
    target_modules: str | None,
    epochs: float,
    learning_rate: float,
    warmup_ratio: float,
    weight_decay: float,
    gradient_accumulation_steps: int,
    max_length: int,
    save_steps: int,
    save_total_limit: int,
    logging_steps: int,
    lr_scheduler_type: str,
    max_grad_norm: float,
    compute_dtype: str,
    optimizer: str,
) -> dict[str, Any]:
    numeric_ints = {
        "seed": (seed, 0, 2**32 - 1),
        "gradient_accumulation_steps": (gradient_accumulation_steps, 1, 4096),
        "max_length": (max_length, 128, MAX_LENGTH_UPPER_BOUND),
        "save_steps": (save_steps, 1, 10**9),
        "save_total_limit": (save_total_limit, 1, 10**6),
        "logging_steps": (logging_steps, 1, 10**9),
    }
    for name, (value, minimum, maximum) in numeric_ints.items():
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not minimum <= value <= maximum
        ):
            raise ValueError(f"{name} is outside its supported integer range")
    numeric_floats = {
        "epochs": (epochs, 0.0, 1000.0, False),
        "learning_rate": (learning_rate, 0.0, 1.0, False),
        "warmup_ratio": (warmup_ratio, 0.0, 1.0, True),
        "weight_decay": (weight_decay, 0.0, 100.0, True),
        "max_grad_norm": (max_grad_norm, 0.0, 1000.0, False),
    }
    normalized_floats: dict[str, float] = {}
    for name, (value, minimum, maximum, inclusive_minimum) in numeric_floats.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be numeric")
        normalized = float(value)
        minimum_ok = normalized >= minimum if inclusive_minimum else normalized > minimum
        if not math.isfinite(normalized) or not minimum_ok or normalized >= maximum:
            raise ValueError(f"{name} is outside its supported range")
        normalized_floats[name] = normalized
    if lr_scheduler_type not in {"cosine", "linear", "constant", "constant_with_warmup"}:
        raise ValueError("unsupported lr_scheduler_type")
    if compute_dtype not in {"float16", "bfloat16", "float32"}:
        raise ValueError("unsupported compute_dtype")
    if optimizer not in {"adamw_torch_fused", "paged_adamw_8bit"}:
        raise ValueError("unsupported optimizer")

    requested_targets = _parse_target_modules(target_modules)
    if adapter_config is None:
        resolved_r = 8 if lora_r is None else lora_r
        resolved_alpha = 32 if lora_alpha is None else lora_alpha
        resolved_dropout = 0.05 if lora_dropout is None else lora_dropout
        resolved_targets: str | list[str] = (
            "all-linear" if requested_targets is None else requested_targets
        )
    else:
        resolved_r = adapter_config["r"]
        resolved_alpha = adapter_config["lora_alpha"]
        resolved_dropout = adapter_config["lora_dropout"]
        resolved_targets = adapter_config["target_modules"]
        if lora_r is not None and lora_r != resolved_r:
            raise ValueError("requested LoRA rank mismatches initial adapter")
        if lora_alpha is not None and lora_alpha != resolved_alpha:
            raise ValueError("requested LoRA alpha mismatches initial adapter")
        if lora_dropout is not None and not math.isclose(
            float(lora_dropout), resolved_dropout, abs_tol=1e-12
        ):
            raise ValueError("requested LoRA dropout mismatches initial adapter")
        if requested_targets == "all-linear":
            if set(resolved_targets) != QWEN_ALL_LINEAR_TARGETS:
                raise ValueError(
                    "initial adapter targets do not match Qwen all-linear modules"
                )
        elif requested_targets is not None and set(requested_targets) != set(
            resolved_targets
        ):
            raise ValueError("requested target_modules mismatch initial adapter")

    if (
        isinstance(resolved_r, bool)
        or not isinstance(resolved_r, int)
        or not 1 <= resolved_r <= 1024
    ):
        raise ValueError("lora_r is outside its supported range")
    if (
        isinstance(resolved_alpha, bool)
        or not isinstance(resolved_alpha, int)
        or not 1 <= resolved_alpha <= 65536
    ):
        raise ValueError("lora_alpha is outside its supported range")
    if (
        isinstance(resolved_dropout, bool)
        or not isinstance(resolved_dropout, (int, float))
        or not math.isfinite(float(resolved_dropout))
        or not 0 <= float(resolved_dropout) < 1
    ):
        raise ValueError("lora_dropout is outside its supported range")
    return {
        "seed": seed,
        "data_seed": seed,
        "quantization": {
            "load_in_4bit": True,
            "bnb_4bit_quant_type": "nf4",
            "bnb_4bit_use_double_quant": True,
            "bnb_4bit_compute_dtype": compute_dtype,
            "bnb_4bit_quant_storage": "uint8",
        },
        "lora": {
            "r": resolved_r,
            "lora_alpha": resolved_alpha,
            "lora_dropout": float(resolved_dropout),
            "target_modules": resolved_targets,
            "use_rslora": (
                adapter_config["use_rslora"] if adapter_config is not None else False
            ),
            "bias": "none",
            "task_type": "CAUSAL_LM",
        },
        "training": {
            "num_train_epochs": normalized_floats["epochs"],
            "learning_rate": normalized_floats["learning_rate"],
            "lr_scheduler_type": lr_scheduler_type,
            "warmup_ratio": normalized_floats["warmup_ratio"],
            "weight_decay": normalized_floats["weight_decay"],
            "max_grad_norm": normalized_floats["max_grad_norm"],
            "per_device_train_batch_size": 1,
            "gradient_accumulation_steps": gradient_accumulation_steps,
            "max_length": max_length,
            "save_strategy": "steps",
            "save_steps": save_steps,
            "save_total_limit": save_total_limit,
            "logging_steps": logging_steps,
            "optimizer": optimizer,
            "gradient_checkpointing": True,
            "gradient_checkpointing_kwargs": {"use_reentrant": False},
            "packing": False,
            "completion_only_loss": True,
            "shuffle_dataset": True,
            "dataloader_num_workers": 0,
            "report_to": "none",
            "full_determinism": True,
            "fp16": compute_dtype == "float16",
            "bf16": compute_dtype == "bfloat16",
            "tf32": False,
            "use_cache": False,
        },
        "evaluation": {
            "eval_dataset_supplied": False,
            "eval_strategy": "no",
            "do_eval": False,
            "eval_on_start": False,
            "load_best_model_at_end": False,
            "metric_for_best_model": None,
        },
        "selection_basis": (
            "RUN_ARGUMENTS_FIXED_BEFORE_THIS_PROCESS_NO_IN_RUN_TUNING"
        ),
    }


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def _chat_template_token_ids(value: Any, *, label: str) -> list[int]:
    """Normalize Transformers 4.x lists and 5.x BatchEncoding results."""

    if isinstance(value, Mapping):
        value = value.get("input_ids")
    if not isinstance(value, list) or any(
        isinstance(token_id, bool)
        or not isinstance(token_id, int)
        or token_id < 0
        for token_id in value
    ):
        raise ValueError(f"tokenizer {label} returned invalid input_ids")
    return value


def _measure_example_tokens(
    tokenizer: Any, example: dict[str, Any], *, label: str
) -> dict[str, int]:
    prompt_ids = _chat_template_token_ids(
        tokenizer.apply_chat_template(
            example["prompt"],
            add_generation_prompt=True,
            tokenize=True,
        ),
        label=f"{label} prompt chat template",
    )
    full_ids = _chat_template_token_ids(
        tokenizer.apply_chat_template(
            example["prompt"] + example["completion"],
            tokenize=True,
        ),
        label=f"{label} full chat template",
    )
    if full_ids[: len(prompt_ids)] != prompt_ids:
        raise ValueError(f"tokenizer prompt/completion prefix mismatch: {label}")
    completion_length = len(full_ids) - len(prompt_ids)
    if completion_length <= 0:
        raise ValueError(f"tokenized row has no completion: {label}")
    return {
        "prompt": len(prompt_ids),
        "full": len(full_ids),
        "completion": completion_length,
    }


def _load_tokenizer_and_measure(
    base_model: Path,
    examples: list[dict[str, Any]],
    *,
    max_length: int,
) -> tuple[Any, dict[str, Any]]:
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(
        str(base_model),
        trust_remote_code=False,
        use_fast=True,
        local_files_only=True,
    )
    eos_token_id = tokenizer.eos_token_id
    if isinstance(eos_token_id, bool) or not isinstance(eos_token_id, int):
        raise ValueError("tokenizer eos_token_id is invalid")
    pad_was_missing = tokenizer.pad_token_id is None
    if pad_was_missing:
        if not tokenizer.eos_token:
            raise ValueError("tokenizer cannot derive a pad token from EOS")
        tokenizer.pad_token = tokenizer.eos_token
    if isinstance(tokenizer.pad_token_id, bool) or not isinstance(
        tokenizer.pad_token_id, int
    ):
        raise ValueError("tokenizer pad_token_id is invalid")
    tokenizer.padding_side = "right"
    full_lengths: list[int] = []
    prompt_lengths: list[int] = []
    completion_lengths: list[int] = []
    for index, example in enumerate(examples, start=1):
        lengths = _measure_example_tokens(
            tokenizer, example, label=f"training row {index}"
        )
        if lengths["full"] > max_length:
            raise ValueError(
                f"tokenized training row {index} exceeds max_length: "
                f"{lengths['full']} > {max_length}"
            )
        prompt_lengths.append(lengths["prompt"])
        full_lengths.append(lengths["full"])
        completion_lengths.append(lengths["completion"])
    chat_template = str(getattr(tokenizer, "chat_template", "") or "")
    if not chat_template:
        raise ValueError("tokenizer chat_template is missing")
    return tokenizer, {
        "class": tokenizer.__class__.__name__,
        "chat_template_sha256": sha256_bytes(chat_template.encode("utf-8")),
        "eos_token_id": eos_token_id,
        "pad_token_id": tokenizer.pad_token_id,
        "pad_token_derived_from_eos": pad_was_missing,
        "padding_side": "right",
        "row_count": len(full_lengths),
        "max_length_limit": max_length,
        "rows_exceeding_max_length": 0,
        "full_tokens": {
            "min": min(full_lengths),
            "p50": _percentile(full_lengths, 0.50),
            "p95": _percentile(full_lengths, 0.95),
            "max": max(full_lengths),
        },
        "prompt_tokens": {
            "min": min(prompt_lengths),
            "max": max(prompt_lengths),
        },
        "completion_tokens": {
            "min": min(completion_lengths),
            "max": max(completion_lengths),
        },
    }


def _resolved_hardware_target_modules(hyperparameters: dict[str, Any]) -> list[str]:
    targets = hyperparameters["lora"]["target_modules"]
    if targets == "all-linear":
        return sorted(QWEN_ALL_LINEAR_TARGETS)
    if not isinstance(targets, list):
        raise ValueError("resolved LoRA target modules are invalid")
    return sorted(set(targets))


def _expected_hardware_plan(hyperparameters: dict[str, Any]) -> dict[str, Any]:
    targets = _resolved_hardware_target_modules(hyperparameters)
    return {
        "contract_version": HARDWARE_PLAN_CONTRACT_VERSION,
        "quantization": dict(hyperparameters["quantization"]),
        "lora": {
            "r": hyperparameters["lora"]["r"],
            "lora_alpha": hyperparameters["lora"]["lora_alpha"],
            "lora_dropout": hyperparameters["lora"]["lora_dropout"],
            "target_modules": targets,
        },
        "training": {
            "per_device_train_batch_size": hyperparameters["training"][
                "per_device_train_batch_size"
            ],
            "gradient_accumulation_steps": hyperparameters["training"][
                "gradient_accumulation_steps"
            ],
            "max_length": hyperparameters["training"]["max_length"],
            "optimizer": hyperparameters["training"]["optimizer"],
            "gradient_checkpointing": hyperparameters["training"][
                "gradient_checkpointing"
            ],
        },
    }


def _validate_quality_hardware_evidence(
    audit: DatasetAudit,
    *,
    base_model: Path,
    tokenizer: Any,
    tokenizer_report: dict[str, Any],
    tokenizer_bundle: dict[str, Any],
    hyperparameters: dict[str, Any],
) -> dict[str, Any]:
    hardware = audit.hardware_exclusions
    if not hardware:
        return {
            "enabled": False,
            "contract_version": QUALITY_EXCLUSIONS_CONTRACT_V2,
            "reason_code": HARDWARE_EXCLUSION_REASON,
            "entry_count": 0,
            "sample_ids_sha256": _sample_ids_sha256(set()),
            "current_hardware_plan_sha256": sha256_bytes(
                stable_json(_expected_hardware_plan(hyperparameters)).encode("utf-8")
            ),
            "all_receipts_recomputed": True,
        }
    if (
        audit.report["quality_exclusions"]["contract_version"]
        != QUALITY_EXCLUSIONS_CONTRACT_V2
    ):
        raise ValueError("hardware exclusions require quality contract v2")
    expected_fields = {
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
    plan = _expected_hardware_plan(hyperparameters)
    plan_sha256 = sha256_bytes(stable_json(plan).encode("utf-8"))
    target_modules = _resolved_hardware_target_modules(hyperparameters)
    base_weights_sha256 = _base_model_weights_sha256(base_model)
    chat_template_sha256 = _verify_sha(
        tokenizer_report.get("chat_template_sha256"),
        label="current tokenizer chat template",
    )
    max_length = hyperparameters["training"]["max_length"]
    for sample_id, evidence in hardware.items():
        if set(evidence) != expected_fields:
            raise ValueError(f"hardware evidence schema is invalid: {sample_id}")
        measured = evidence.get("measured_full_tokens")
        if (
            isinstance(measured, bool)
            or not isinstance(measured, int)
            or measured <= max_length
            or evidence.get("max_length") != max_length
        ):
            raise ValueError(
                f"hardware exclusion threshold is invalid: {sample_id}"
            )
        actual_lengths = _measure_example_tokens(
            tokenizer,
            audit.hardware_examples[sample_id],
            label=f"hardware exclusion {sample_id}",
        )
        if actual_lengths["full"] != measured:
            raise ValueError(
                f"hardware exclusion token measurement mismatch: {sample_id}"
            )
        row_info = audit.unique_rows_by_id[sample_id]
        source_sft_row_sha256 = row_info.get("source_sft_row_sha256")
        if source_sft_row_sha256 is None or _verify_sha(
            evidence.get("source_unique_row_sha256"),
            label=f"hardware evidence source row {sample_id}",
        ) != source_sft_row_sha256:
            raise ValueError(
                f"hardware exclusion source row binding mismatch: {sample_id}"
            )
        if _verify_sha(
            evidence.get("unique_dataset_sha256"),
            label=f"hardware evidence unique dataset {sample_id}",
        ) != audit.report["unique_audit"]["train_sft_sha256"]:
            raise ValueError(
                f"hardware exclusion input dataset binding mismatch: {sample_id}"
            )
        bindings = {
            "base_model_weights_sha256": base_weights_sha256,
            "tokenizer_bundle_sha256": tokenizer_bundle["sha256"],
            "chat_template_sha256": chat_template_sha256,
        }
        for field, expected in bindings.items():
            if _verify_sha(
                evidence.get(field), label=f"hardware evidence {field} {sample_id}"
            ) != expected:
                raise ValueError(
                    f"hardware exclusion {field} mismatch: {sample_id}"
                )
        if evidence.get("measurement_tool_version") != TOKEN_AUDIT_MEASUREMENT_TOOL_VERSION:
            raise ValueError(
                f"hardware exclusion measurement tool mismatch: {sample_id}"
            )
        if evidence.get("target_modules") != target_modules:
            raise ValueError(
                f"hardware exclusion target modules mismatch: {sample_id}"
            )
        if evidence.get("hardware_plan") != plan:
            raise ValueError(f"hardware exclusion plan mismatch: {sample_id}")
        if _verify_sha(
            evidence.get("hardware_plan_sha256"),
            label=f"hardware evidence plan {sample_id}",
        ) != plan_sha256:
            raise ValueError(f"hardware exclusion plan SHA256 mismatch: {sample_id}")
        receipt_payload = {
            "sample_id": sample_id,
            "reason_code": HARDWARE_EXCLUSION_REASON,
            **{
                field: evidence[field]
                for field in sorted(expected_fields - {"token_audit_receipt_sha256"})
            },
        }
        receipt_sha256 = sha256_bytes(
            stable_json(receipt_payload).encode("utf-8")
        )
        if _verify_sha(
            evidence.get("token_audit_receipt_sha256"),
            label=f"hardware evidence receipt {sample_id}",
        ) != receipt_sha256:
            raise ValueError(
                f"hardware exclusion receipt mismatch: {sample_id}"
            )
    return {
        "enabled": True,
        "contract_version": QUALITY_EXCLUSIONS_CONTRACT_V2,
        "reason_code": HARDWARE_EXCLUSION_REASON,
        "entry_count": len(hardware),
        "sample_ids_sha256": _sample_ids_sha256(set(hardware)),
        "measurement_tool_version": TOKEN_AUDIT_MEASUREMENT_TOOL_VERSION,
        "base_model_weights_sha256": base_weights_sha256,
        "tokenizer_bundle_sha256": tokenizer_bundle["sha256"],
        "chat_template_sha256": chat_template_sha256,
        "current_hardware_plan_sha256": plan_sha256,
        "all_receipts_recomputed": True,
    }


def _load_training_stack() -> dict[str, Any]:
    import bitsandbytes
    import peft
    import torch
    import transformers
    import trl
    from datasets import Dataset
    from peft import LoraConfig, PeftModel, prepare_model_for_kbit_training
    from transformers import (
        AutoModelForCausalLM,
        BitsAndBytesConfig,
        set_seed,
    )
    from trl import SFTConfig, SFTTrainer

    imported = {
        "transformers": transformers.__version__,
        "trl": trl.__version__,
        "peft": peft.__version__,
        "bitsandbytes": bitsandbytes.__version__,
    }
    if imported != REQUIRED_PACKAGE_VERSIONS:
        raise RuntimeError(
            "imported training stack version mismatch: " + stable_json(imported)
        )
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable")
    properties = torch.cuda.get_device_properties(0)
    if "RTX 4060" not in properties.name:
        raise RuntimeError("CUDA device 0 is not an RTX 4060")
    if not 7 * 1024**3 <= properties.total_memory <= 9 * 1024**3:
        raise RuntimeError("CUDA device 0 does not match the 8GB memory profile")
    return {
        "torch": torch,
        "Dataset": Dataset,
        "LoraConfig": LoraConfig,
        "PeftModel": PeftModel,
        "prepare_model_for_kbit_training": prepare_model_for_kbit_training,
        "AutoModelForCausalLM": AutoModelForCausalLM,
        "BitsAndBytesConfig": BitsAndBytesConfig,
        "set_seed": set_seed,
        "SFTConfig": SFTConfig,
        "SFTTrainer": SFTTrainer,
        "runtime": {
            "imported_versions": imported,
            "torch_version": torch.__version__,
            "cuda_version": torch.version.cuda,
            "gpu": {
                "index": 0,
                "name": properties.name,
                "total_memory_bytes": properties.total_memory,
            },
        },
    }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    return str(value)


def _normalize_trainable_parameter_dtypes(
    model: Any, *, float32_dtype: Any
) -> dict[str, Any]:
    """Keep LoRA optimizer state in FP32 while the frozen 4-bit base stays quantized.

    PEFT preserves the dtype stored in a resumed adapter.  The v11 adapter was
    saved as BF16, which is incompatible with the FP16 GradScaler used by this
    constrained RTX 4060 training profile.  Only parameters already marked as
    trainable are converted; frozen base-model and quantization tensors are
    never touched.
    """

    before: Counter[str] = Counter()
    after: Counter[str] = Counter()
    parameter_tensor_count = 0
    parameter_numel = 0
    converted_tensor_count = 0
    converted_numel = 0
    target_dtype = str(float32_dtype)

    for _name, parameter in model.named_parameters():
        if not bool(getattr(parameter, "requires_grad", False)):
            continue
        tensor_numel = int(parameter.numel())
        parameter_tensor_count += 1
        parameter_numel += tensor_numel
        before[str(parameter.dtype)] += tensor_numel
        if parameter.dtype != float32_dtype:
            parameter.data = parameter.data.to(dtype=float32_dtype)
            converted_tensor_count += 1
            converted_numel += tensor_numel
        after[str(parameter.dtype)] += tensor_numel

    if parameter_tensor_count == 0:
        raise RuntimeError("training model has no trainable parameters")
    if set(after) != {target_dtype}:
        raise RuntimeError(
            "trainable parameter dtype normalization failed: "
            + stable_json(dict(sorted(after.items())))
        )
    return {
        "target_dtype": target_dtype,
        "parameter_tensor_count": parameter_tensor_count,
        "parameter_numel": parameter_numel,
        "converted_tensor_count": converted_tensor_count,
        "converted_numel": converted_numel,
        "before_numel_by_dtype": dict(sorted(before.items())),
        "after_numel_by_dtype": dict(sorted(after.items())),
        "frozen_parameters_untouched": True,
    }


def _execute_training(
    *,
    stack: dict[str, Any],
    tokenizer: Any,
    examples: list[dict[str, Any]],
    base_model: Path,
    init_adapter: Path | None,
    stage: Path,
    hyperparameters: dict[str, Any],
    adapter_contract: dict[str, Any],
) -> dict[str, Any]:
    torch = stack["torch"]
    quant = hyperparameters["quantization"]
    compute_dtype = {
        "float16": torch.float16,
        "bfloat16": torch.bfloat16,
        "float32": torch.float32,
    }[quant["bnb_4bit_compute_dtype"]]
    quantization_config = stack["BitsAndBytesConfig"](
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=compute_dtype,
        bnb_4bit_quant_storage=torch.uint8,
    )
    stack["set_seed"](hyperparameters["seed"], deterministic=True)
    model = stack["AutoModelForCausalLM"].from_pretrained(
        str(base_model),
        quantization_config=quantization_config,
        torch_dtype=compute_dtype,
        device_map={"": 0},
        trust_remote_code=False,
        local_files_only=True,
        low_cpu_mem_usage=True,
        attn_implementation="sdpa",
        use_cache=False,
    )
    model.config.use_cache = False
    training = hyperparameters["training"]
    model = stack["prepare_model_for_kbit_training"](
        model,
        use_gradient_checkpointing=True,
        gradient_checkpointing_kwargs=training[
            "gradient_checkpointing_kwargs"
        ],
    )
    if init_adapter is None:
        lora = hyperparameters["lora"]
        peft_config = stack["LoraConfig"](
            task_type="CAUSAL_LM",
            inference_mode=False,
            r=lora["r"],
            lora_alpha=lora["lora_alpha"],
            lora_dropout=lora["lora_dropout"],
            target_modules=lora["target_modules"],
            use_rslora=lora["use_rslora"],
            bias="none",
        )
    else:
        model = stack["PeftModel"].from_pretrained(
            model,
            str(init_adapter),
            is_trainable=True,
        )
        peft_config = None

    dataset = stack["Dataset"].from_list(examples)
    sft_config = stack["SFTConfig"](
        output_dir=str(stage),
        per_device_train_batch_size=1,
        per_device_eval_batch_size=1,
        num_train_epochs=training["num_train_epochs"],
        learning_rate=training["learning_rate"],
        lr_scheduler_type=training["lr_scheduler_type"],
        # Transformers 5.15 names this field ``warmup_steps`` but explicitly
        # interprets values below 1 as a fraction of total training steps.
        warmup_steps=training["warmup_ratio"],
        optim=training["optimizer"],
        weight_decay=training["weight_decay"],
        gradient_accumulation_steps=training["gradient_accumulation_steps"],
        max_grad_norm=training["max_grad_norm"],
        fp16=training["fp16"],
        bf16=training["bf16"],
        tf32=training["tf32"],
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs=training[
            "gradient_checkpointing_kwargs"
        ],
        use_cache=False,
        full_determinism=True,
        seed=hyperparameters["seed"],
        data_seed=hyperparameters["data_seed"],
        eval_strategy="no",
        do_train=True,
        do_eval=False,
        eval_on_start=False,
        load_best_model_at_end=False,
        metric_for_best_model=None,
        save_strategy="steps",
        save_steps=training["save_steps"],
        save_total_limit=training["save_total_limit"],
        logging_steps=training["logging_steps"],
        report_to="none",
        push_to_hub=False,
        dataloader_num_workers=0,
        packing=False,
        padding_free=False,
        max_length=training["max_length"],
        shuffle_dataset=True,
        completion_only_loss=True,
        assistant_only_loss=False,
    )
    trainer = stack["SFTTrainer"](
        model=model,
        args=sft_config,
        train_dataset=dataset,
        eval_dataset=None,
        processing_class=tokenizer,
        peft_config=peft_config,
    )
    trainable_dtype_normalization = _normalize_trainable_parameter_dtypes(
        trainer.model,
        float32_dtype=torch.float32,
    )
    train_result = trainer.train()
    trainer.save_state()
    final_adapter = stage / FINAL_ADAPTER_DIR
    if final_adapter.exists():
        raise FileExistsError(f"final adapter path already exists: {final_adapter}")
    trainer.save_model(str(final_adapter))
    tokenizer.save_pretrained(str(final_adapter))
    saved_adapter_config = _parse_adapter_config(final_adapter, base_model)
    expected_targets = hyperparameters["lora"]["target_modules"]
    if expected_targets == "all-linear":
        expected_targets = sorted(QWEN_ALL_LINEAR_TARGETS)
    expected_saved_lora = {
        "r": hyperparameters["lora"]["r"],
        "lora_alpha": hyperparameters["lora"]["lora_alpha"],
        "lora_dropout": hyperparameters["lora"]["lora_dropout"],
        "target_modules": sorted(expected_targets),
        "use_rslora": hyperparameters["lora"]["use_rslora"],
    }
    if any(
        saved_adapter_config[field] != expected
        for field, expected in expected_saved_lora.items()
    ):
        raise RuntimeError("saved adapter config does not match requested LoRA")
    final_fingerprint = adapter_fingerprint(final_adapter)
    final_contract = {
        **adapter_contract,
        "adapter_fingerprint_sha256": final_fingerprint["sha256"],
    }
    contract_bytes = (
        json.dumps(final_contract, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    _write_new_file(final_adapter / ADAPTER_CONTRACT_NAME, contract_bytes)
    contract_sha256 = sha256_bytes(contract_bytes)
    _write_new_file(
        final_adapter / (ADAPTER_CONTRACT_NAME + ".sha256"),
        f"{contract_sha256}  {ADAPTER_CONTRACT_NAME}\n".encode("ascii"),
    )
    contract_sidecar = _verify_sidecar(
        final_adapter / ADAPTER_CONTRACT_NAME, contract_sha256
    )
    checkpoints = sorted(
        item.name
        for item in stage.iterdir()
        if item.is_dir() and re.fullmatch(r"checkpoint-\d+", item.name)
    )
    return {
        "runtime": stack["runtime"],
        "trainable_dtype_normalization": trainable_dtype_normalization,
        "train_metrics": _json_safe(getattr(train_result, "metrics", {})),
        "global_step": int(getattr(trainer.state, "global_step", 0)),
        "checkpoints": checkpoints,
        "final_adapter": {
            "directory": FINAL_ADAPTER_DIR,
            "fingerprint": final_fingerprint,
            "config": saved_adapter_config,
            "contract_filename": ADAPTER_CONTRACT_NAME,
            "contract_sha256": contract_sha256,
            "contract_sidecar": contract_sidecar,
        },
        "adapter_output_contract": final_contract,
    }


def _write_new_file(path: Path, raw: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _stage_output(output_dir: Path) -> tuple[Path, Path]:
    output_dir = output_dir.resolve()
    if os.path.lexists(output_dir):
        raise FileExistsError(f"output directory already exists: {output_dir}")
    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = parent / f".{output_dir.name}.tmp"
    if os.path.lexists(stage):
        raise FileExistsError(f"training staging directory already exists: {stage}")
    stage.mkdir(exist_ok=False)
    return output_dir, stage


def _cleanup_stage(stage: Path, output_dir: Path) -> None:
    resolved_stage = stage.resolve()
    expected = output_dir.parent.resolve() / f".{output_dir.name}.tmp"
    if resolved_stage != expected:
        raise RuntimeError("refusing to clean an unexpected staging directory")
    if resolved_stage.is_dir():
        shutil.rmtree(resolved_stage)


def _publish_stage(stage: Path, output_dir: Path) -> None:
    if os.path.lexists(output_dir):
        raise FileExistsError(f"output directory appeared during run: {output_dir}")
    stage.rename(output_dir)


def run_training_driver(
    *,
    dataset: Path,
    dataset_manifest: Path,
    base_model: Path,
    output_dir: Path,
    init_adapter: Path | None = None,
    expected_dataset_sha256: str | None = None,
    expected_manifest_sha256: str | None = None,
    dry_run: bool = False,
    seed: int = 42,
    lora_r: int | None = None,
    lora_alpha: int | None = None,
    lora_dropout: float | None = None,
    target_modules: str | None = None,
    epochs: float = 2.0,
    learning_rate: float = 2e-5,
    warmup_ratio: float = 0.08,
    weight_decay: float = 0.1,
    gradient_accumulation_steps: int = 16,
    max_length: int = 2048,
    save_steps: int = 20,
    save_total_limit: int = 8,
    logging_steps: int = 5,
    lr_scheduler_type: str = "cosine",
    max_grad_norm: float = 1.0,
    compute_dtype: str = "float16",
    optimizer: str = "adamw_torch_fused",
) -> dict[str, Any]:
    """Preflight and optionally train without reading any evaluation dataset."""

    if not isinstance(dry_run, bool):
        raise ValueError("dry_run must be boolean")
    if not dry_run and (
        expected_dataset_sha256 is None or expected_manifest_sha256 is None
    ):
        raise ValueError(
            "real training requires explicit expected dataset and manifest SHA256"
        )
    if EXPECTED_MODEL_OUTPUT_CONTRACT != MODEL_OUTPUT_CONTRACT:
        raise RuntimeError("weak-supervision model output contract drifted")
    output_dir = output_dir.resolve()
    if os.path.lexists(output_dir):
        raise FileExistsError(f"output directory already exists: {output_dir}")
    dataset = dataset.resolve()
    dataset_manifest = dataset_manifest.resolve()
    base_model = base_model.resolve()
    resolved_init_adapter = init_adapter.resolve() if init_adapter else None
    protected_directories = [base_model]
    if resolved_init_adapter is not None:
        protected_directories.append(resolved_init_adapter)
    for protected in protected_directories:
        if output_dir == protected or output_dir.is_relative_to(protected):
            raise ValueError(
                f"output directory must not overlap protected input: {protected}"
            )

    audit = _audit_training_dataset(
        dataset,
        dataset_manifest,
        expected_dataset_sha256=expected_dataset_sha256,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    model_profile = _load_base_model_profile(base_model)
    model_fingerprint = base_model_fingerprint(base_model)
    environment = _probe_environment()

    init_report: dict[str, Any] | None = None
    adapter_config: dict[str, Any] | None = None
    if resolved_init_adapter is not None:
        adapter_config = _parse_adapter_config(resolved_init_adapter, base_model)
        initial_fingerprint = adapter_fingerprint(resolved_init_adapter)
        init_report = {
            "path": str(resolved_init_adapter),
            "fingerprint": initial_fingerprint,
            "config": adapter_config,
            "target_contract_binding": _audit_init_adapter_contract(
                resolved_init_adapter,
                base_model=base_model,
                base_fingerprint_sha256=model_fingerprint["sha256"],
                adapter_fingerprint_sha256=initial_fingerprint["sha256"],
            ),
        }
    hyperparameters = _resolve_hyperparameters(
        adapter_config=adapter_config,
        seed=seed,
        lora_r=lora_r,
        lora_alpha=lora_alpha,
        lora_dropout=lora_dropout,
        target_modules=target_modules,
        epochs=epochs,
        learning_rate=learning_rate,
        warmup_ratio=warmup_ratio,
        weight_decay=weight_decay,
        gradient_accumulation_steps=gradient_accumulation_steps,
        max_length=max_length,
        save_steps=save_steps,
        save_total_limit=save_total_limit,
        logging_steps=logging_steps,
        lr_scheduler_type=lr_scheduler_type,
        max_grad_norm=max_grad_norm,
        compute_dtype=compute_dtype,
        optimizer=optimizer,
    )
    tokenizer, tokenizer_report = _load_tokenizer_and_measure(
        base_model,
        audit.examples,
        max_length=hyperparameters["training"]["max_length"],
    )
    tokenizer_bundle = _tokenizer_bundle_fingerprint(base_model)
    tokenizer_report["bundle_fingerprint"] = tokenizer_bundle
    audit.report["hardware_exclusion_validation"] = (
        _validate_quality_hardware_evidence(
            audit,
            base_model=base_model,
            tokenizer=tokenizer,
            tokenizer_report=tokenizer_report,
            tokenizer_bundle=tokenizer_bundle,
            hyperparameters=hyperparameters,
        )
    )
    mode = "RESUME_TRAINABLE_ADAPTER" if init_report else "NEW_LORA"
    adapter_contract = {
        "schema_version": 1,
        "contract_version": ADAPTER_CONTRACT_VERSION,
        "training_driver_version": DRIVER_VERSION,
        "target_contract": TARGET_CONTRACT,
        "model_output_contract": MODEL_OUTPUT_CONTRACT,
        "prompt_version": QWEN_WEAK_PROMPT_VERSION,
        "prompt_sha256": QWEN_WEAK_PROMPT_SHA256,
        "base_model_fingerprint_sha256": model_fingerprint["sha256"],
        "training_dataset_sha256": audit.report["sha256"],
        "training_mode": mode,
        "initial_adapter_fingerprint_sha256": (
            init_report["fingerprint"]["sha256"] if init_report else None
        ),
        "adapter_fingerprint_sha256": None,
        "label_classification": LABEL_CLASSIFICATION,
        "human_gold": False,
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "driver_version": DRIVER_VERSION,
        "status": "DRY_RUN_VALIDATED" if dry_run else "TRAINING_PENDING",
        "execution_mode": "DRY_RUN" if dry_run else "TRAIN",
        "training_mode": mode,
        "training_started": False,
        "training_completed": False,
        "model_weights_loaded": False,
        "dataset": audit.report,
        "target_contract": TARGET_CONTRACT,
        "model_output_contract": MODEL_OUTPUT_CONTRACT,
        "prompt": {
            "version": QWEN_WEAK_PROMPT_VERSION,
            "sha256": QWEN_WEAK_PROMPT_SHA256,
        },
        "base_model": {
            "path": str(base_model),
            "profile": model_profile,
            "fingerprint": model_fingerprint,
        },
        "initial_adapter": init_report,
        "tokenizer": tokenizer_report,
        "environment": environment,
        "hyperparameters": hyperparameters,
        "adapter_output_contract": adapter_contract,
        "driver": {
            "path": str(Path(__file__).resolve()),
            "sha256": sha256_file(Path(__file__).resolve()),
        },
        "boundaries": {
            "eval_dataset_read": False,
            "predictions_read": False,
            "sealed_benchmark_read": False,
            "market_results_read": False,
            "runtime_eval_guided_tuning": False,
            "curriculum_policy_design_provenance": audit.report[
                "policy_design_provenance"
            ],
            "adaptive_dev_informed_curriculum": (
                audit.report["policy_design_provenance"]
                == V13_POLICY_DESIGN_PROVENANCE
            ),
            "evaluation_enabled": False,
            "load_best_model_at_end": False,
            "output_directory_overwrite_allowed": False,
            "atomic_directory_publish": True,
        },
        "result": None,
    }

    output_dir, stage = _stage_output(output_dir)
    try:
        if not dry_run:
            stack = _load_training_stack()
            manifest["training_started"] = True
            manifest["model_weights_loaded"] = True
            result = _execute_training(
                stack=stack,
                tokenizer=tokenizer,
                examples=audit.examples,
                base_model=base_model,
                init_adapter=resolved_init_adapter,
                stage=stage,
                hyperparameters=hyperparameters,
                adapter_contract=adapter_contract,
            )
            manifest["status"] = "COMPLETED"
            manifest["training_completed"] = True
            manifest["result"] = result
            manifest["adapter_output_contract"] = result[
                "adapter_output_contract"
            ]
        manifest_bytes = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n"
        ).encode("utf-8")
        manifest_sha256 = sha256_bytes(manifest_bytes)
        _write_new_file(stage / TRAINING_MANIFEST_NAME, manifest_bytes)
        _write_new_file(
            stage / (TRAINING_MANIFEST_NAME + ".sha256"),
            f"{manifest_sha256}  {TRAINING_MANIFEST_NAME}\n".encode("ascii"),
        )
        _publish_stage(stage, output_dir)
    except Exception:
        _cleanup_stage(stage, output_dir)
        raise
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--dataset-manifest", type=Path, required=True)
    parser.add_argument("--base-model", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--init-adapter", type=Path)
    parser.add_argument("--expected-dataset-sha256")
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--lora-r", type=int)
    parser.add_argument("--lora-alpha", type=int)
    parser.add_argument("--lora-dropout", type=float)
    parser.add_argument("--target-modules")
    parser.add_argument("--epochs", type=float, default=2.0)
    parser.add_argument("--learning-rate", type=float, default=2e-5)
    parser.add_argument("--warmup-ratio", type=float, default=0.08)
    parser.add_argument("--weight-decay", type=float, default=0.1)
    parser.add_argument("--gradient-accumulation-steps", type=int, default=16)
    parser.add_argument("--max-length", type=int, default=2048)
    parser.add_argument("--save-steps", type=int, default=20)
    parser.add_argument("--save-total-limit", type=int, default=8)
    parser.add_argument("--logging-steps", type=int, default=5)
    parser.add_argument(
        "--lr-scheduler-type",
        choices=("cosine", "linear", "constant", "constant_with_warmup"),
        default="cosine",
    )
    parser.add_argument("--max-grad-norm", type=float, default=1.0)
    parser.add_argument(
        "--compute-dtype",
        choices=("float16", "bfloat16", "float32"),
        default="float16",
    )
    parser.add_argument(
        "--optimizer",
        choices=("adamw_torch_fused", "paged_adamw_8bit"),
        default="adamw_torch_fused",
    )
    args = parser.parse_args(argv)
    manifest = run_training_driver(
        dataset=args.dataset,
        dataset_manifest=args.dataset_manifest,
        base_model=args.base_model,
        output_dir=args.output_dir,
        init_adapter=args.init_adapter,
        expected_dataset_sha256=args.expected_dataset_sha256,
        expected_manifest_sha256=args.expected_manifest_sha256,
        dry_run=args.dry_run,
        seed=args.seed,
        lora_r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        target_modules=args.target_modules,
        epochs=args.epochs,
        learning_rate=args.learning_rate,
        warmup_ratio=args.warmup_ratio,
        weight_decay=args.weight_decay,
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        max_length=args.max_length,
        save_steps=args.save_steps,
        save_total_limit=args.save_total_limit,
        logging_steps=args.logging_steps,
        lr_scheduler_type=args.lr_scheduler_type,
        max_grad_norm=args.max_grad_norm,
        compute_dtype=args.compute_dtype,
        optimizer=args.optimizer,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
