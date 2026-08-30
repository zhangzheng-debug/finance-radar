from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.qwen_risk_contract import expected_semantic_payload
from app.models.risk_label_contract import MATERIALITY, POLARITIES
from app.models.qwen_weak_supervision_contract import (
    QWEN_WEAK_PROMPT_SHA256,
    QWEN_WEAK_PROMPT_VERSION,
    QWEN_WEAK_SYSTEM_PROMPT,
)
from scripts import train_qwen_semantic_axes_adapter as driver


TRAINABLE_PAIRS = tuple(
    sorted(
        f"{materiality}|{polarity}"
        for materiality in MATERIALITY
        for polarity in POLARITIES
    )
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _bound_write(path: Path, raw: bytes) -> str:
    path.write_bytes(raw)
    digest = _sha(raw)
    path.with_name(path.name + ".sha256").write_bytes(
        f"{digest}  {path.name}\n".encode("ascii")
    )
    return digest


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return b"".join(
        (driver.stable_json(row) + "\n").encode("utf-8") for row in rows
    )


def _base_model(tmp_path: Path) -> Path:
    path = tmp_path / "Qwen2.5-1.5B-Instruct"
    path.mkdir()
    config = {
        "architectures": ["Qwen2ForCausalLM"],
        "model_type": "qwen2",
        "hidden_size": 1536,
        "intermediate_size": 8960,
        "num_hidden_layers": 28,
        "num_attention_heads": 12,
        "num_key_value_heads": 2,
        "vocab_size": 151936,
    }
    (path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    (path / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    (path / "tokenizer.json").write_text("{}", encoding="utf-8")
    (path / "model.safetensors").write_bytes(b"fake-local-qwen-weights")
    return path


def _policy(
    *, v13: bool = False, neutral_preset: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    if v13 and neutral_preset:
        raise ValueError("test policy mode is ambiguous")
    multipliers = (
        dict(driver.V13_CONSENSUS_PAIR_MULTIPLIERS)
        if v13
        else {pair: 1 for pair in TRAINABLE_PAIRS}
    )
    if not v13 and not neutral_preset:
        multipliers["MATERIAL_ADVERSE|ADVERSE"] = 2
    preset = (
        driver.V13_CURRICULUM_PRESET
        if v13
        else "neutral-1x-v1" if neutral_preset else None
    )
    pair_policy = {
        "contract_version": driver.PAIR_MULTIPLIER_CONTRACT_VERSION,
        "policy_version": (
            driver.V13_CURRICULUM_PRESET
            if v13
            else "neutral-1x-v1" if neutral_preset else "unit-test-pair-policy-v1"
        ),
        "multipliers": multipliers,
    }
    resolution_policy = {
        "contract_version": driver.RESOLUTION_MULTIPLIER_CONTRACT_VERSION,
        "curriculum_version": driver.V13_CURRICULUM_VERSION if v13 else None,
        "split_scope": "TRAIN_ONLY",
        "a_b_consensus_multiplier_source": "JOINT_PAIR_POLICY",
        "c_arbitration_fixed_multiplier": 1 if v13 else None,
    }
    canonical = {
        "pair_multiplier_policy": pair_policy,
        "resolution_multiplier_policy": resolution_policy,
    }
    full = {
        **pair_policy,
        "resolution_multiplier_policy": resolution_policy,
        "source": (
            "VERSIONED_PRESET" if v13 or neutral_preset else "EXPLICIT_JSON_FILE"
        ),
        "preset": preset,
        "policy_sha256": _sha(driver.stable_json(canonical).encode("utf-8")),
        "input_file": None if v13 or neutral_preset else {
            "filename": "unit-test-pair-policy.json",
            "sha256": "1" * 64,
        },
        "policy_design_provenance": (
            driver.V13_POLICY_DESIGN_PROVENANCE
            if v13
            else driver.NEUTRAL_POLICY_DESIGN_PROVENANCE
            if neutral_preset
            else driver.EXPLICIT_POLICY_DESIGN_PROVENANCE
        ),
        "builder_runtime_input_isolation": dict(
            driver.BUILDER_RUNTIME_INPUT_ISOLATION
        ),
    }
    summary = {key: value for key, value in full.items() if key != "multipliers"}
    return full, summary


def _distribution(rows: list[dict[str, Any]]) -> dict[str, Any]:
    materiality: Counter[str] = Counter()
    polarity: Counter[str] = Counter()
    pair: Counter[str] = Counter()
    priority: Counter[str] = Counter()
    for row in rows:
        target = json.loads(row["messages"][2]["content"])
        semantic = row["metadata"]["semantic_target"]
        materiality[target["materiality"]] += 1
        polarity[target["polarity"]] += 1
        pair[f"{target['materiality']}|{target['polarity']}"] += 1
        priority[semantic["semantic_priority"]] += 1
    return {
        "row_count": len(rows),
        "materiality": dict(sorted(materiality.items())),
        "polarity": dict(sorted(polarity.items())),
        "pair": dict(sorted(pair.items())),
        "semantic_priority": dict(sorted(priority.items())),
    }


def _training_fixture(
    tmp_path: Path,
    *,
    v13: bool = False,
    neutral_preset: bool = False,
    excluded_indices: set[int] | None = None,
    quality_excluded_indices: set[int] | None = None,
    hardware_excluded_indices: set[int] | None = None,
    quality_contract: str = driver.QUALITY_EXCLUSIONS_CONTRACT_V1,
    legacy_membership: bool = False,
) -> dict[str, Any]:
    excluded_indices = set() if excluded_indices is None else excluded_indices
    quality_excluded_indices = (
        set() if quality_excluded_indices is None else quality_excluded_indices
    )
    hardware_excluded_indices = (
        set() if hardware_excluded_indices is None else hardware_excluded_indices
    )
    if excluded_indices and not v13:
        raise ValueError("test source exclusions are v13-only")
    all_quality_indices = quality_excluded_indices | hardware_excluded_indices
    if excluded_indices & all_quality_indices:
        raise ValueError("test exclusion classes must be disjoint")
    if quality_excluded_indices & hardware_excluded_indices:
        raise ValueError("test quality exclusions must be unique")
    all_excluded_indices = excluded_indices | all_quality_indices
    tmp_path.mkdir(parents=True, exist_ok=True)
    dataset = tmp_path / "trainable-balanced.jsonl"
    manifest_path = tmp_path / "manifest.json"
    unique_path = tmp_path / "unique-audit.jsonl"
    train_sft_sha256 = _sha(b"unit-test-original-train-sft-input")
    policy, row_policy = _policy(v13=v13, neutral_preset=neutral_preset)
    rows: list[dict[str, Any]] = []
    source_specs: list[tuple[str, str, str, int]] = []
    for index in range(driver.EXPECTED_UNIQUE_MEMBERS):
        if index in {1, 3}:
            pair = ("MATERIAL_ADVERSE", "ADVERSE", 2)
        elif index == 2:
            pair = ("UNCLEAR", "UNCLEAR", 1)
        else:
            pair = ("NOT_MATERIAL_ADVERSE", "NEUTRAL", 1)
        source_specs.append((f"train-unit-{index:03d}", *pair))
    resolution_unique: Counter[str] = Counter()
    resolution_trainable: Counter[str] = Counter()
    resolution_excluded: Counter[str] = Counter()
    excluded_pair_resolution: Counter[str] = Counter()
    resolution_effective: Counter[str] = Counter()
    excluded_effective_count = 0
    unique_audit_rows: list[dict[str, Any]] = []
    trainable_unique_rows: list[dict[str, Any]] = []
    for source_index, (sample_id, materiality, polarity, _pair_multiplier) in enumerate(
        source_specs
    ):
        decision_source = "C_ARBITRATION" if source_index == 1 else "A_B_CONSENSUS"
        pair_multiplier = policy["multipliers"][f"{materiality}|{polarity}"]
        multiplier = (
            1
            if v13 and decision_source == "C_ARBITRATION"
            else pair_multiplier
        )
        resolution_unique[decision_source] += 1
        content = {
            "as_of": "2026-08-30T00:00:00+00:00",
            "event_date": "2026-08-30",
            "headline": f"Source update for {sample_id}",
            "summary": "Contemporaneous issuer source text.",
            "passages": [
                {
                    "document_type": "8-K",
                    "item_section": "1.01",
                    "published_at": "2026-08-30T01:00:00+00:00",
                    "passage": "The issuer published a current business update.",
                }
            ],
        }
        content_text = driver.stable_json(content)
        target = {"materiality": materiality, "polarity": polarity}
        messages = [
            {"role": "system", "content": QWEN_WEAK_SYSTEM_PROMPT},
            {"role": "user", "content": content_text},
            {"role": "assistant", "content": driver.stable_json(target)},
        ]
        source_sft_row = {
            "messages": messages,
            "metadata": {"sample_id": sample_id, "split": "TRAIN"},
        }
        source_sft_row_sha256 = _sha(
            driver.stable_json(source_sft_row).encode("utf-8")
        )
        if source_index in excluded_indices:
            exclusion_reason = driver.NUMERIC_TABLE_EXCLUSION_REASON
        elif source_index in quality_excluded_indices:
            exclusion_reason = driver.SOURCE_CONFLICT_EXCLUSION_REASON
        elif source_index in hardware_excluded_indices:
            exclusion_reason = driver.HARDWARE_EXCLUSION_REASON
        else:
            exclusion_reason = None
        quality_exclusion = None
        if source_index in all_quality_indices:
            quality_exclusion = {
                "contract_version": quality_contract,
                "label_classification": driver.LABEL_CLASSIFICATION,
                "reason_code": exclusion_reason,
                "reason": "Independent TRAIN source quality exclusion.",
                "input_sha256": _sha(b"quality-exclusions-fixture"),
            }
            if source_index in hardware_excluded_indices:
                quality_exclusion["evidence"] = {}
        unique_metadata = {
            "sample_id": sample_id,
            "split": "TRAIN",
            "target_contract": driver.TARGET_CONTRACT,
            "model_output_contract": driver.MODEL_OUTPUT_CONTRACT,
            "overlay_contract_version": driver.DATASET_CONTRACT_VERSION,
            "overlay_view": "UNIQUE_AUDIT",
            "label_provenance": driver.LABEL_PROVENANCE,
            "label_classification": driver.LABEL_CLASSIFICATION,
            "human_gold_claimed": False,
            "qwen_prediction_included": False,
            "post_event_market_data_included": False,
            "evidence_state_used_as_model_target": False,
            "original_weak_truth_used": False,
            "source_payload_binding_verified": True,
            "quality_exclusion": quality_exclusion,
            "prompt_version": QWEN_WEAK_PROMPT_VERSION,
            "prompt_sha256": QWEN_WEAK_PROMPT_SHA256,
            "content_sha256": _sha(content_text.encode("utf-8")),
            "semantic_target": expected_semantic_payload(materiality, polarity),
            "review_resolution": {"decision_source": decision_source},
            "training_eligibility": {
                "eligible": exclusion_reason is None,
                "exclusion_reason": exclusion_reason,
                "labels_rewritten": False,
                "pair_multiplier": multiplier,
            },
            "source_structure": {
                "numeric_table_dominated": source_index in excluded_indices,
            },
            "pair_multiplier_policy": row_policy,
            "overlay_input_sha256": {"train_sft": train_sft_sha256},
            "source_sft_row_sha256": source_sft_row_sha256,
        }
        unique_row = {"messages": messages, "metadata": unique_metadata}
        unique_audit_rows.append(unique_row)
        if source_index in all_excluded_indices:
            resolution_excluded[decision_source] += 1
            excluded_pair_resolution[
                f"{decision_source}::{materiality}|{polarity}"
            ] += 1
            excluded_effective_count += multiplier
            continue
        trainable_unique_rows.append(unique_row)
        resolution_trainable[decision_source] += 1
        resolution_effective[decision_source] += multiplier
        source_row_sha = _sha(driver.stable_json(unique_row).encode("utf-8"))
        for replica_index in range(1, multiplier + 1):
            replica_metadata = {
                **unique_metadata,
                "overlay_view": "TRAINABLE_BALANCED",
                "training_replica": {
                    "replica_id": _sha(
                        (
                            f"{policy['policy_sha256']}\0{sample_id}\0"
                            f"{replica_index}\0{multiplier}"
                        ).encode("utf-8")
                    ),
                    "source_unique_sample_id": sample_id,
                    "source_unique_row_sha256": source_row_sha,
                    "replica_index": replica_index,
                    "replica_count": multiplier,
                    "labels_rewritten": False,
                },
            }
            rows.append(
                {
                    "messages": messages,
                    "metadata": replica_metadata,
                }
            )
    unique_sha = _bound_write(unique_path, _jsonl_bytes(unique_audit_rows))
    dataset_sha = _bound_write(dataset, _jsonl_bytes(rows))
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contract_version": driver.DATASET_CONTRACT_VERSION,
        "target_contract": driver.TARGET_CONTRACT,
        "model_output_contract": driver.MODEL_OUTPUT_CONTRACT,
        "label_provenance": driver.LABEL_PROVENANCE,
        "label_classification": driver.LABEL_CLASSIFICATION,
        "human_gold_claimed": False,
        "expected_unique_row_count": driver.EXPECTED_UNIQUE_MEMBERS,
        "quality_exclusions": {
            "enabled": bool(all_quality_indices),
            "contract_version": quality_contract,
            "label_classification": driver.LABEL_CLASSIFICATION,
            "input_file": (
                {
                    "filename": "quality-exclusions.json",
                    "sha256": _sha(b"quality-exclusions-fixture"),
                }
                if all_quality_indices
                else None
            ),
            "entry_count": len(all_quality_indices),
            "sample_ids_sha256": _sha(
                driver.stable_json(
                    sorted(
                        source_specs[index][0]
                        for index in all_quality_indices
                    )
                ).encode("utf-8")
            ),
            "reason_code_counts": {
                **(
                    {
                        driver.SOURCE_CONFLICT_EXCLUSION_REASON: len(
                            quality_excluded_indices
                        )
                    }
                    if quality_excluded_indices
                    else {}
                ),
                **(
                    {
                        driver.HARDWARE_EXCLUSION_REASON: len(
                            hardware_excluded_indices
                        )
                    }
                    if hardware_excluded_indices
                    else {}
                ),
            },
        },
        "inputs": {
            "train_sft": {
                "filename": "original-train-sft.jsonl",
                "sha256": train_sft_sha256,
                "row_count": driver.EXPECTED_UNIQUE_MEMBERS,
            }
        },
        "prompt": {
            "version": QWEN_WEAK_PROMPT_VERSION,
            "sha256": QWEN_WEAK_PROMPT_SHA256,
            "system_message_binding_verified": True,
        },
        "isolation": {
            "original_weak_truth_used": False,
            "qwen_predictions_read": False,
            "dev_metrics_read": False,
            "market_results_read": False,
            "sealed_benchmark_read": False,
            "external_facts_read": False,
            "unclear_labels_rewritten": False,
        },
        "pair_multiplier_policy": policy,
        "resolution_counts": dict(sorted(resolution_unique.items())),
        "trainability_policy": {
            "materiality_allowed": sorted(MATERIALITY),
            "polarity_allowed": sorted(POLARITIES),
            "unclear_training_enabled": True,
            "unclear_labels_rewritten": False,
            "original_unique_row_count": len(source_specs),
            "trainable_unique_row_count": len(trainable_unique_rows),
            "excluded_unique_row_count": len(all_excluded_indices),
            "excluded_effective_replica_count": excluded_effective_count,
            "pre_exclusion_effective_row_count": (
                len(rows) + excluded_effective_count
            ),
            "trainable_effective_row_count": len(rows),
            "exclusion_reasons": {
                **(
                    {driver.NUMERIC_TABLE_EXCLUSION_REASON: len(excluded_indices)}
                    if excluded_indices
                    else {}
                ),
                **(
                    {
                        driver.SOURCE_CONFLICT_EXCLUSION_REASON: len(
                            quality_excluded_indices
                        )
                    }
                    if quality_excluded_indices
                    else {}
                ),
                **(
                    {
                        driver.HARDWARE_EXCLUSION_REASON: len(
                            hardware_excluded_indices
                        )
                    }
                    if hardware_excluded_indices
                    else {}
                ),
            },
            "trainable_resolution_counts": dict(
                sorted(resolution_trainable.items())
            ),
            "excluded_resolution_counts": dict(
                sorted(resolution_excluded.items())
            ),
            "excluded_pair_resolution_counts": dict(
                sorted(excluded_pair_resolution.items())
            ),
            "source_structure_exclusion": {
                "enabled": v13,
                "reason": driver.NUMERIC_TABLE_EXCLUSION_REASON,
                "stable_json_character_count_min": (
                    driver.NUMERIC_TABLE_MIN_STABLE_JSON_CHARS
                ),
                "digit_character_ratio_min": (
                    driver.NUMERIC_TABLE_MIN_DIGIT_RATIO
                ),
                "label_independent": True,
                "applies_to_preset": driver.V13_CURRICULUM_PRESET,
            },
        },
        "distributions": {
            "unique_audit": _distribution(unique_audit_rows),
            "trainable_unique": _distribution(trainable_unique_rows),
            "trainable_effective": _distribution(rows),
        },
        "outputs": {
            "unique_audit": {
                "filename": unique_path.name,
                "row_count": len(unique_audit_rows),
                "sample_ids_sha256": driver._sample_ids_sha256(
                    {sample_id for sample_id, *_rest in source_specs}
                ),
                "sha256": unique_sha,
                "sidecar": unique_path.name + ".sha256",
                "sidecar_sha256": _sha(
                    f"{unique_sha}  {unique_path.name}\n".encode("ascii")
                ),
            },
            "trainable_balanced": {
                "filename": dataset.name,
                "unique_source_row_count": len(trainable_unique_rows),
                "sample_ids_sha256": _sha(
                    driver.stable_json(
                        sorted(
                            sample_id
                            for index, (sample_id, *_rest) in enumerate(
                                source_specs
                            )
                            if index not in all_excluded_indices
                        )
                    ).encode("utf-8")
                ),
                "row_count": len(rows),
                "sha256": dataset_sha,
                "sidecar": dataset.name + ".sha256",
                "sidecar_sha256": _sha(
                    f"{dataset_sha}  {dataset.name}\n".encode("ascii")
                ),
            }
        },
    }
    if not legacy_membership:
        original_ids = {sample_id for sample_id, *_rest in source_specs}
        trainable_ids = {
            sample_id
            for index, (sample_id, *_rest) in enumerate(source_specs)
            if index not in all_excluded_indices
        }
        excluded_ids = original_ids - trainable_ids
        numeric_ids = {source_specs[index][0] for index in excluded_indices}
        quality_ids = {source_specs[index][0] for index in all_quality_indices}
        manifest["membership_commitment"] = {
            "contract_version": driver.MEMBERSHIP_COMMITMENT_CONTRACT_VERSION,
            "original_unique": {
                "count": len(original_ids),
                "sample_ids_sha256": driver._sample_ids_sha256(original_ids),
            },
            "trainable_unique": {
                "count": len(trainable_ids),
                "sample_ids_sha256": driver._sample_ids_sha256(trainable_ids),
            },
            "excluded_complement": {
                "count": len(excluded_ids),
                "sample_ids_sha256": driver._sample_ids_sha256(excluded_ids),
            },
            "numeric_exclusions": {
                "count": len(numeric_ids),
                "sample_ids_sha256": driver._sample_ids_sha256(numeric_ids),
            },
            "quality_exclusions": {
                "count": len(quality_ids),
                "sample_ids_sha256": driver._sample_ids_sha256(quality_ids),
            },
            "exclusion_classes_disjoint": True,
        }
    resolution_policy = policy["resolution_multiplier_policy"]
    manifest["curriculum"] = {
        "enabled": v13,
        "version": resolution_policy["curriculum_version"],
        "split_scope": "TRAIN_ONLY",
        "label_classification": driver.LABEL_CLASSIFICATION,
        "unique_source_row_count": len(source_specs),
        "a_b_clean_consensus": {
            "unique_row_count": resolution_unique["A_B_CONSENSUS"],
            "effective_row_count": resolution_effective["A_B_CONSENSUS"],
            "multiplier_source": "JOINT_PAIR_POLICY",
            "joint_pair_multipliers": dict(sorted(policy["multipliers"].items())),
        },
        "c_arbitration": {
            "unique_row_count": resolution_unique["C_ARBITRATION"],
            "effective_row_count": resolution_effective["C_ARBITRATION"],
            "fixed_multiplier": resolution_policy["c_arbitration_fixed_multiplier"],
        },
        "effective_distribution": _distribution(rows),
        "input_isolation": {
            "train_only": True,
            "dev_metrics_read": False,
            "qwen_predictions_read": False,
            "market_results_read": False,
            "sealed_benchmark_read": False,
        },
    }
    manifest_sha = _bound_write(manifest_path, _json_bytes(manifest))
    return {
        "dataset": dataset,
        "manifest_path": manifest_path,
        "manifest": manifest,
        "manifest_sha": manifest_sha,
        "dataset_sha": dataset_sha,
        "rows": rows,
        "unique_path": unique_path,
        "unique_rows": unique_audit_rows,
        "train_sft_sha256": train_sft_sha256,
    }


class _FakeTokenizer:
    def apply_chat_template(
        self,
        messages: list[dict[str, str]],
        *,
        tokenize: bool,
        add_generation_prompt: bool = False,
    ) -> list[int]:
        assert tokenize is True
        return [1] * (1200 if add_generation_prompt else 1301)

    def save_pretrained(self, path: str) -> None:
        target = Path(path)
        target.mkdir(parents=True, exist_ok=True)
        (target / "tokenizer_config.json").write_text("{}", encoding="utf-8")


def _fake_tokenizer_measure(
    _base_model: Path,
    examples: list[dict[str, Any]],
    *,
    max_length: int,
) -> tuple[_FakeTokenizer, dict[str, Any]]:
    return _FakeTokenizer(), {
        "class": "FakeTokenizer",
        "chat_template_sha256": "2" * 64,
        "eos_token_id": 1,
        "pad_token_id": 1,
        "pad_token_derived_from_eos": False,
        "padding_side": "right",
        "row_count": len(examples),
        "max_length_limit": max_length,
        "rows_exceeding_max_length": 0,
        "full_tokens": {"min": 30, "p50": 30, "p95": 31, "max": 31},
        "prompt_tokens": {"min": 25, "max": 26},
        "completion_tokens": {"min": 5, "max": 5},
    }


def _republish_manifest(fixture: dict[str, Any]) -> None:
    fixture["manifest_sha"] = _bound_write(
        fixture["manifest_path"], _json_bytes(fixture["manifest"])
    )


def _republish_unique(fixture: dict[str, Any]) -> None:
    digest = _bound_write(
        fixture["unique_path"], _jsonl_bytes(fixture["unique_rows"])
    )
    binding = fixture["manifest"]["outputs"]["unique_audit"]
    binding["sha256"] = digest
    binding["sidecar_sha256"] = _sha(
        f"{digest}  {fixture['unique_path'].name}\n".encode("ascii")
    )
    _republish_manifest(fixture)


def _bind_hardware_evidence(
    fixture: dict[str, Any],
    base_model: Path,
    *,
    max_length: int = 1280,
    optimizer: str = "paged_adamw_8bit",
) -> None:
    hyperparameters = driver._resolve_hyperparameters(
        adapter_config=None,
        seed=42,
        lora_r=None,
        lora_alpha=None,
        lora_dropout=None,
        target_modules=None,
        epochs=2.0,
        learning_rate=2e-5,
        warmup_ratio=0.08,
        weight_decay=0.1,
        gradient_accumulation_steps=16,
        max_length=max_length,
        save_steps=20,
        save_total_limit=8,
        logging_steps=5,
        lr_scheduler_type="cosine",
        max_grad_norm=1.0,
        compute_dtype="float16",
        optimizer=optimizer,
    )
    plan = driver._expected_hardware_plan(hyperparameters)
    plan_sha256 = _sha(driver.stable_json(plan).encode("utf-8"))
    tokenizer_sha256 = driver._tokenizer_bundle_fingerprint(base_model)["sha256"]
    base_weights_sha256 = driver._base_model_weights_sha256(base_model)
    target_modules = sorted(driver.QWEN_ALL_LINEAR_TARGETS)
    for row in fixture["unique_rows"]:
        metadata = row["metadata"]
        quality = metadata.get("quality_exclusion")
        if (
            not isinstance(quality, dict)
            or quality.get("reason_code") != driver.HARDWARE_EXCLUSION_REASON
        ):
            continue
        evidence = {
            "measured_full_tokens": 1301,
            "max_length": max_length,
            "source_unique_row_sha256": metadata["source_sft_row_sha256"],
            "unique_dataset_sha256": fixture["train_sft_sha256"],
            "base_model_weights_sha256": base_weights_sha256,
            "tokenizer_bundle_sha256": tokenizer_sha256,
            "chat_template_sha256": "2" * 64,
            "measurement_tool_version": driver.TOKEN_AUDIT_MEASUREMENT_TOOL_VERSION,
            "target_modules": target_modules,
            "hardware_plan": plan,
            "hardware_plan_sha256": plan_sha256,
        }
        receipt = {
            "sample_id": metadata["sample_id"],
            "reason_code": driver.HARDWARE_EXCLUSION_REASON,
            **evidence,
        }
        evidence["token_audit_receipt_sha256"] = _sha(
            driver.stable_json(receipt).encode("utf-8")
        )
        quality["evidence"] = evidence
    _republish_unique(fixture)


def _fake_environment() -> dict[str, Any]:
    return {
        "python": "3.12.2",
        "platform": "unit-test",
        "executable": "python",
        "packages": {
            **driver.REQUIRED_PACKAGE_VERSIONS,
            "torch": "2.11.0+cu128",
            "datasets": "4.8.4",
            "accelerate": "test",
            "safetensors": "test",
        },
        "required_exact_versions": dict(driver.REQUIRED_PACKAGE_VERSIONS),
        "nvidia_smi": {
            "selected_gpu_index": 0,
            "gpus": [
                {
                    "index": 0,
                    "name": "NVIDIA GeForce RTX 4060 Laptop GPU",
                    "memory_mib": 8188,
                    "driver_version": "test",
                }
            ],
        },
    }


def _patch_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(driver, "_probe_environment", _fake_environment)
    monkeypatch.setattr(
        driver,
        "_validate_safetensors_file",
        lambda path, *, label: {
            "filename": path.name,
            "tensor_count": 1,
            "metadata": {"format": "pt"},
        },
    )
    monkeypatch.setattr(
        driver, "_load_tokenizer_and_measure", _fake_tokenizer_measure
    )


def _init_adapter(tmp_path: Path, base_model: Path) -> Path:
    adapter = tmp_path / "initial-adapter"
    adapter.mkdir()
    config = {
        "base_model_name_or_path": str(base_model.resolve()),
        "peft_type": "LORA",
        "task_type": "CAUSAL_LM",
        "bias": "none",
        "r": 8,
        "lora_alpha": 32,
        "lora_dropout": 0.05,
        "target_modules": sorted(driver.QWEN_ALL_LINEAR_TARGETS),
        "use_rslora": False,
    }
    (adapter / "adapter_config.json").write_text(
        json.dumps(config), encoding="utf-8"
    )
    (adapter / "adapter_model.safetensors").write_bytes(b"fake-adapter-weights")
    fingerprint = driver.adapter_fingerprint(adapter)
    contract = {
        "schema_version": 1,
        "contract_version": driver.ADAPTER_CONTRACT_VERSION,
        "training_driver_version": driver.DRIVER_VERSION,
        "target_contract": driver.TARGET_CONTRACT,
        "model_output_contract": driver.MODEL_OUTPUT_CONTRACT,
        "prompt_version": QWEN_WEAK_PROMPT_VERSION,
        "prompt_sha256": QWEN_WEAK_PROMPT_SHA256,
        "base_model_fingerprint_sha256": driver.base_model_fingerprint(base_model)[
            "sha256"
        ],
        "adapter_fingerprint_sha256": fingerprint["sha256"],
        "training_dataset_sha256": "3" * 64,
        "training_mode": "NEW_LORA",
        "initial_adapter_fingerprint_sha256": None,
        "label_classification": driver.LABEL_CLASSIFICATION,
        "human_gold": False,
    }
    _bound_write(adapter / driver.ADAPTER_CONTRACT_NAME, _json_bytes(contract))
    return adapter


def _run(
    fixture: dict[str, Any],
    base_model: Path,
    output_dir: Path,
    **kwargs: Any,
) -> dict[str, Any]:
    return driver.run_training_driver(
        dataset=fixture["dataset"],
        dataset_manifest=fixture["manifest_path"],
        base_model=base_model,
        output_dir=output_dir,
        expected_dataset_sha256=fixture["dataset_sha"],
        expected_manifest_sha256=fixture["manifest_sha"],
        **kwargs,
    )


def test_dry_run_new_lora_validates_without_loading_model_and_refuses_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(tmp_path)
    base_model = _base_model(tmp_path)
    output = tmp_path / "dry-new"
    _patch_preflight(monkeypatch)

    def forbidden_stack_load() -> dict[str, Any]:
        raise AssertionError("dry-run must not import or load the model stack")

    monkeypatch.setattr(driver, "_load_training_stack", forbidden_stack_load)
    manifest = _run(fixture, base_model, output, dry_run=True)

    assert manifest["status"] == "DRY_RUN_VALIDATED"
    assert manifest["training_mode"] == "NEW_LORA"
    assert manifest["model_weights_loaded"] is False
    assert manifest["hyperparameters"]["quantization"] == {
        "load_in_4bit": True,
        "bnb_4bit_quant_type": "nf4",
        "bnb_4bit_use_double_quant": True,
        "bnb_4bit_compute_dtype": "float16",
        "bnb_4bit_quant_storage": "uint8",
    }
    assert manifest["hyperparameters"]["evaluation"]["eval_dataset_supplied"] is False
    assert manifest["dataset"]["distribution"]["materiality"]["UNCLEAR"] == 1
    assert manifest["dataset"]["distribution"]["polarity"]["UNCLEAR"] == 1
    assert manifest["dataset"]["resolution_multiplier_policy"][
        "c_arbitration_fixed_multiplier"
    ] is None
    assert manifest["dataset"]["curriculum"]["enabled"] is False
    persisted = json.loads((output / driver.TRAINING_MANIFEST_NAME).read_text("utf-8"))
    assert persisted == manifest
    assert not (output / driver.FINAL_ADAPTER_DIR).exists()
    with pytest.raises(ValueError, match="must not overlap protected input"):
        _run(
            fixture,
            base_model,
            base_model / "adapter-output",
            dry_run=True,
        )
    with pytest.raises(ValueError, match="unknown Qwen module"):
        _run(
            fixture,
            base_model,
            tmp_path / "bad-target-module",
            dry_run=True,
            target_modules="does_not_exist",
        )
    with pytest.raises(FileExistsError, match="already exists"):
        _run(fixture, base_model, output, dry_run=True)


def test_v13_resolution_curriculum_uses_pair_weights_only_for_a_b_consensus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(tmp_path, v13=True)
    base_model = _base_model(tmp_path)
    _patch_preflight(monkeypatch)
    monkeypatch.setattr(
        driver,
        "_load_training_stack",
        lambda: (_ for _ in ()).throw(AssertionError("must not load model")),
    )

    manifest = _run(fixture, base_model, tmp_path / "v13-dry", dry_run=True)

    rows_by_sample: Counter[str] = Counter(
        row["metadata"]["sample_id"] for row in fixture["rows"]
    )
    assert rows_by_sample["train-unit-003"] == 2
    assert rows_by_sample["train-unit-001"] == 1
    dataset_report = manifest["dataset"]
    assert dataset_report["resolution_multiplier_policy"] == {
        "contract_version": driver.RESOLUTION_MULTIPLIER_CONTRACT_VERSION,
        "curriculum_version": driver.V13_CURRICULUM_VERSION,
        "split_scope": "TRAIN_ONLY",
        "a_b_consensus_multiplier_source": "JOINT_PAIR_POLICY",
        "c_arbitration_fixed_multiplier": 1,
    }
    assert dataset_report["curriculum"]["enabled"] is True
    assert dataset_report["curriculum"]["version"] == driver.V13_CURRICULUM_VERSION
    assert dataset_report["curriculum"]["c_arbitration"]["effective_row_count"] == 1
    assert dataset_report["policy_design_provenance"] == (
        driver.V13_POLICY_DESIGN_PROVENANCE
    )
    assert dataset_report["builder_runtime_input_isolation"] == (
        driver.BUILDER_RUNTIME_INPUT_ISOLATION
    )
    assert manifest["boundaries"]["adaptive_dev_informed_curriculum"] is True
    assert manifest["boundaries"]["runtime_eval_guided_tuning"] is False


def test_v13_driver_accepts_manifest_bound_trainable_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(
        tmp_path,
        v13=True,
        excluded_indices={3, 4},
    )
    base_model = _base_model(tmp_path)
    _patch_preflight(monkeypatch)
    monkeypatch.setattr(
        driver,
        "_load_training_stack",
        lambda: (_ for _ in ()).throw(AssertionError("must not load model")),
    )

    manifest = _run(
        fixture,
        base_model,
        tmp_path / "v13-excluded-dry",
        dry_run=True,
    )

    report = manifest["dataset"]
    assert report["original_unique_row_count"] == 729
    assert report["unique_source_row_count"] == 727
    assert report["excluded_unique_row_count"] == 2
    assert report["excluded_effective_replica_count"] == 4
    assert report["curriculum"]["unique_source_row_count"] == 729


def test_driver_accepts_manifest_bound_quality_exclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(
        tmp_path,
        v13=True,
        quality_excluded_indices={5},
    )
    base_model = _base_model(tmp_path)
    _patch_preflight(monkeypatch)
    monkeypatch.setattr(
        driver,
        "_load_training_stack",
        lambda: (_ for _ in ()).throw(AssertionError("must not load model")),
    )

    manifest = _run(
        fixture,
        base_model,
        tmp_path / "v13-quality-excluded-dry",
        dry_run=True,
    )

    report = manifest["dataset"]
    assert report["original_unique_row_count"] == 729
    assert report["unique_source_row_count"] == 728
    assert report["excluded_unique_row_count"] == 1
    assert report["excluded_effective_replica_count"] == 2
    assert report["quality_exclusions"]["reason_code_counts"] == {
        "SOURCE_FIELD_CONFLICT": 1
    }
    assert report["membership_closure"]["validation_method"] == (
        "MEMBERSHIP_COMMITMENT_V2_RECOMPUTED"
    )


def test_legacy_v1_manifest_recomputes_exact_unique_complement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(
        tmp_path,
        v13=True,
        quality_excluded_indices={5},
        quality_contract=driver.QUALITY_EXCLUSIONS_CONTRACT_V1,
        legacy_membership=True,
    )
    base_model = _base_model(tmp_path)
    _patch_preflight(monkeypatch)

    manifest = _run(
        fixture, base_model, tmp_path / "legacy-membership-dry", dry_run=True
    )

    closure = manifest["dataset"]["membership_closure"]
    assert closure["validation_method"] == (
        "LEGACY_UNIQUE_AUDIT_COMPLEMENT_RECOMPUTED_V1"
    )
    assert closure["excluded_complement"]["count"] == 1
    assert closure["quality_exclusions"]["count"] == 1


def test_membership_commitment_rejects_numeric_quality_member_hash_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(
        tmp_path,
        v13=True,
        excluded_indices={4},
        quality_excluded_indices={5},
    )
    base_model = _base_model(tmp_path)
    _patch_preflight(monkeypatch)
    commitment = fixture["manifest"]["membership_commitment"]
    numeric_hash = commitment["numeric_exclusions"]["sample_ids_sha256"]
    commitment["numeric_exclusions"]["sample_ids_sha256"] = commitment[
        "quality_exclusions"
    ]["sample_ids_sha256"]
    commitment["quality_exclusions"]["sample_ids_sha256"] = numeric_hash
    _republish_manifest(fixture)

    with pytest.raises(ValueError, match="membership commitment mismatch"):
        _run(fixture, base_model, tmp_path / "member-hash-swap", dry_run=True)


@pytest.mark.parametrize("tamper", ["sidecar", "duplicate_member"])
def test_unique_audit_sibling_integrity_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    fixture = _training_fixture(tmp_path)
    base_model = _base_model(tmp_path)
    _patch_preflight(monkeypatch)
    if tamper == "sidecar":
        fixture["unique_path"].with_name(
            fixture["unique_path"].name + ".sha256"
        ).write_text("0" * 64 + "  wrong.jsonl\n", encoding="ascii")
    else:
        fixture["unique_rows"][1]["metadata"]["sample_id"] = fixture[
            "unique_rows"
        ][0]["metadata"]["sample_id"]
        _republish_unique(fixture)

    expected = "sidecar mismatch" if tamper == "sidecar" else "duplicate sample_id"
    with pytest.raises(ValueError, match=expected):
        _run(fixture, base_model, tmp_path / f"unique-{tamper}", dry_run=True)


def test_v2_hardware_exclusion_revalidates_current_receipt_and_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(
        tmp_path,
        v13=True,
        hardware_excluded_indices={5},
        quality_contract=driver.QUALITY_EXCLUSIONS_CONTRACT_V2,
    )
    base_model = _base_model(tmp_path)
    _bind_hardware_evidence(fixture, base_model)
    _patch_preflight(monkeypatch)

    manifest = _run(
        fixture,
        base_model,
        tmp_path / "hardware-v2-dry",
        dry_run=True,
        max_length=1280,
        optimizer="paged_adamw_8bit",
    )

    validation = manifest["dataset"]["hardware_exclusion_validation"]
    assert validation["enabled"] is True
    assert validation["entry_count"] == 1
    assert validation["all_receipts_recomputed"] is True


def test_v1_hardware_exclusion_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(
        tmp_path,
        v13=True,
        hardware_excluded_indices={5},
        quality_contract=driver.QUALITY_EXCLUSIONS_CONTRACT_V1,
    )
    base_model = _base_model(tmp_path)
    _patch_preflight(monkeypatch)

    with pytest.raises(ValueError, match="v1 quality exclusions cannot contain"):
        _run(fixture, base_model, tmp_path / "hardware-v1", dry_run=True)


def test_v2_quality_contract_requires_membership_commitment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(
        tmp_path,
        v13=True,
        hardware_excluded_indices={5},
        quality_contract=driver.QUALITY_EXCLUSIONS_CONTRACT_V2,
        legacy_membership=True,
    )
    base_model = _base_model(tmp_path)
    _bind_hardware_evidence(fixture, base_model)
    _patch_preflight(monkeypatch)

    with pytest.raises(ValueError, match="require membership_commitment"):
        _run(fixture, base_model, tmp_path / "hardware-v2-no-membership", dry_run=True)


@pytest.mark.parametrize("tamper", ["threshold", "plan", "receipt"])
def test_hardware_exclusion_rejects_threshold_plan_or_receipt_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    fixture = _training_fixture(
        tmp_path,
        v13=True,
        hardware_excluded_indices={5},
        quality_contract=driver.QUALITY_EXCLUSIONS_CONTRACT_V2,
    )
    base_model = _base_model(tmp_path)
    _bind_hardware_evidence(fixture, base_model)
    evidence = next(
        row["metadata"]["quality_exclusion"]["evidence"]
        for row in fixture["unique_rows"]
        if row["metadata"].get("quality_exclusion") is not None
    )
    if tamper == "threshold":
        evidence["max_length"] = 1279
    elif tamper == "plan":
        evidence["hardware_plan"]["lora"]["r"] = 16
    else:
        evidence["token_audit_receipt_sha256"] = "f" * 64
    _republish_unique(fixture)
    _patch_preflight(monkeypatch)

    expected = (
        "threshold"
        if tamper == "threshold"
        else "plan mismatch"
        if tamper == "plan"
        else "receipt mismatch"
    )
    with pytest.raises(ValueError, match=expected):
        _run(
            fixture,
            base_model,
            tmp_path / f"hardware-{tamper}-tamper",
            dry_run=True,
            max_length=1280,
            optimizer="paged_adamw_8bit",
        )


def test_neutral_preset_with_null_c_override_remains_compatible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(tmp_path, neutral_preset=True)
    base_model = _base_model(tmp_path)
    _patch_preflight(monkeypatch)
    monkeypatch.setattr(
        driver,
        "_load_training_stack",
        lambda: (_ for _ in ()).throw(AssertionError("must not load model")),
    )

    manifest = _run(fixture, base_model, tmp_path / "neutral-dry", dry_run=True)

    assert manifest["dataset"]["row_count"] == driver.EXPECTED_UNIQUE_MEMBERS
    assert manifest["dataset"]["curriculum"]["enabled"] is False
    assert manifest["dataset"]["resolution_multiplier_policy"][
        "c_arbitration_fixed_multiplier"
    ] is None


@pytest.mark.parametrize(
    "case",
    ["extra_field", "bad_contract", "bad_version", "bad_scope", "bad_ab_source", "bad_c"],
)
def test_resolution_multiplier_policy_contract_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    fixture = _training_fixture(tmp_path, v13=True)
    base_model = _base_model(tmp_path)
    _patch_preflight(monkeypatch)
    resolution = fixture["manifest"]["pair_multiplier_policy"][
        "resolution_multiplier_policy"
    ]
    if case == "extra_field":
        resolution["unexpected"] = True
    elif case == "bad_contract":
        resolution["contract_version"] = "wrong-contract"
    elif case == "bad_version":
        resolution["curriculum_version"] = "wrong-curriculum"
    elif case == "bad_scope":
        resolution["split_scope"] = "DEV"
    elif case == "bad_ab_source":
        resolution["a_b_consensus_multiplier_source"] = "PAIR_GUESS"
    else:
        resolution["c_arbitration_fixed_multiplier"] = 2
    fixture["manifest_sha"] = _bound_write(
        fixture["manifest_path"], _json_bytes(fixture["manifest"])
    )

    with pytest.raises(ValueError, match="resolution|v13|A/B"):
        _run(fixture, base_model, tmp_path / f"bad-resolution-{case}", dry_run=True)


def test_resolution_policy_is_bound_into_policy_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(tmp_path)
    base_model = _base_model(tmp_path)
    _patch_preflight(monkeypatch)
    policy = fixture["manifest"]["pair_multiplier_policy"]
    policy["policy_version"] = driver.V13_CURRICULUM_PRESET
    policy["source"] = "VERSIONED_PRESET"
    policy["preset"] = driver.V13_CURRICULUM_PRESET
    policy["input_file"] = None
    policy["policy_design_provenance"] = (
        driver.V13_POLICY_DESIGN_PROVENANCE
    )
    policy["multipliers"] = dict(driver.V13_CONSENSUS_PAIR_MULTIPLIERS)
    policy["resolution_multiplier_policy"] = {
        "contract_version": driver.RESOLUTION_MULTIPLIER_CONTRACT_VERSION,
        "curriculum_version": driver.V13_CURRICULUM_VERSION,
        "split_scope": "TRAIN_ONLY",
        "a_b_consensus_multiplier_source": "JOINT_PAIR_POLICY",
        "c_arbitration_fixed_multiplier": 1,
    }
    fixture["manifest_sha"] = _bound_write(
        fixture["manifest_path"], _json_bytes(fixture["manifest"])
    )

    with pytest.raises(ValueError, match="policy SHA256 mismatch"):
        _run(fixture, base_model, tmp_path / "unbound-resolution", dry_run=True)


def test_row_review_resolution_decision_source_is_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(tmp_path)
    base_model = _base_model(tmp_path)
    _patch_preflight(monkeypatch)
    fixture["rows"][0]["metadata"]["review_resolution"][
        "decision_source"
    ] = "UNKNOWN"
    dataset_sha = _bound_write(fixture["dataset"], _jsonl_bytes(fixture["rows"]))
    fixture["dataset_sha"] = dataset_sha
    fixture["manifest"]["outputs"]["trainable_balanced"]["sha256"] = dataset_sha
    fixture["manifest_sha"] = _bound_write(
        fixture["manifest_path"], _json_bytes(fixture["manifest"])
    )

    with pytest.raises(ValueError, match="decision_source"):
        _run(fixture, base_model, tmp_path / "bad-decision-source", dry_run=True)


def test_replica_id_must_match_builder_formula(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(tmp_path)
    base_model = _base_model(tmp_path)
    _patch_preflight(monkeypatch)
    fixture["rows"][0]["metadata"]["training_replica"]["replica_id"] = (
        "0" * 64
    )
    dataset_sha = _bound_write(fixture["dataset"], _jsonl_bytes(fixture["rows"]))
    fixture["dataset_sha"] = dataset_sha
    fixture["manifest"]["outputs"]["trainable_balanced"]["sha256"] = (
        dataset_sha
    )
    fixture["manifest_sha"] = _bound_write(
        fixture["manifest_path"], _json_bytes(fixture["manifest"])
    )

    with pytest.raises(ValueError, match="replica_id formula mismatch"):
        _run(fixture, base_model, tmp_path / "bad-replica-id", dry_run=True)


def test_real_training_requires_both_expected_hashes_before_preflight(
    tmp_path: Path,
) -> None:
    fixture = _training_fixture(tmp_path)
    base_model = _base_model(tmp_path)

    with pytest.raises(ValueError, match="requires explicit expected dataset"):
        driver.run_training_driver(
            dataset=fixture["dataset"],
            dataset_manifest=fixture["manifest_path"],
            base_model=base_model,
            output_dir=tmp_path / "real-without-pins",
            dry_run=False,
        )
    assert not (tmp_path / "real-without-pins").exists()


def test_dry_run_init_adapter_validates_fingerprint_contract_and_lora_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(tmp_path)
    base_model = _base_model(tmp_path)
    adapter = _init_adapter(tmp_path, base_model)
    output = tmp_path / "dry-resume"
    _patch_preflight(monkeypatch)
    monkeypatch.setattr(
        driver,
        "_load_training_stack",
        lambda: (_ for _ in ()).throw(AssertionError("must not load model")),
    )

    manifest = _run(
        fixture,
        base_model,
        output,
        init_adapter=adapter,
        dry_run=True,
        target_modules="all-linear",
    )

    assert manifest["training_mode"] == "RESUME_TRAINABLE_ADAPTER"
    initial = manifest["initial_adapter"]
    assert initial["fingerprint"]["sha256"] == driver.adapter_fingerprint(adapter)[
        "sha256"
    ]
    assert (
        initial["target_contract_binding"]["binding_method"]
        == "ADAPTER_TRAINING_CONTRACT_V1"
    )
    assert manifest["hyperparameters"]["lora"]["r"] == 8
    weight_path = adapter / "adapter_model.safetensors"
    original_weight = weight_path.read_bytes()
    weight_path.write_bytes(b"swapped-adapter-weights")
    with pytest.raises(ValueError, match="adapter_fingerprint_sha256"):
        _run(
            fixture,
            base_model,
            tmp_path / "swapped-adapter",
            init_adapter=adapter,
            dry_run=True,
        )
    weight_path.write_bytes(original_weight)
    with pytest.raises(ValueError, match="rank mismatches"):
        _run(
            fixture,
            base_model,
            tmp_path / "bad-resume",
            init_adapter=adapter,
            dry_run=True,
            lora_r=16,
        )

    contract_path = adapter / driver.ADAPTER_CONTRACT_NAME
    contract = json.loads(contract_path.read_text("utf-8"))
    contract["model_output_contract"] = "wrong-contract"
    _bound_write(contract_path, _json_bytes(contract))
    with pytest.raises(ValueError, match="model_output_contract"):
        _run(
            fixture,
            base_model,
            tmp_path / "bad-adapter-contract",
            init_adapter=adapter,
            dry_run=True,
        )


def test_dataset_target_prompt_hash_and_source_leakage_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(tmp_path)
    base_model = _base_model(tmp_path)
    _patch_preflight(monkeypatch)

    with pytest.raises(ValueError, match="explicit SHA256 mismatch"):
        driver.run_training_driver(
            dataset=fixture["dataset"],
            dataset_manifest=fixture["manifest_path"],
            base_model=base_model,
            output_dir=tmp_path / "bad-sha",
            expected_dataset_sha256="0" * 64,
            dry_run=True,
        )

    rows = fixture["rows"]
    rows[0]["metadata"]["semantic_target"]["semantic_priority"] = "PRIORITY_REVIEW"
    dataset_sha = _bound_write(fixture["dataset"], _jsonl_bytes(rows))
    fixture["manifest"]["outputs"]["trainable_balanced"]["sha256"] = dataset_sha
    fixture["manifest_sha"] = _bound_write(
        fixture["manifest_path"], _json_bytes(fixture["manifest"])
    )
    with pytest.raises(ValueError, match="semantic_target is inconsistent"):
        _run(
            {**fixture, "dataset_sha": dataset_sha},
            base_model,
            tmp_path / "bad-semantic",
            dry_run=True,
        )

    prompt_fixture = _training_fixture(tmp_path / "prompt")
    prompt_fixture["manifest"]["prompt"]["version"] = "wrong-prompt"
    prompt_fixture["manifest_sha"] = _bound_write(
        prompt_fixture["manifest_path"], _json_bytes(prompt_fixture["manifest"])
    )
    with pytest.raises(ValueError, match="prompt binding mismatch"):
        _run(
            prompt_fixture,
            base_model,
            tmp_path / "bad-prompt",
            dry_run=True,
        )

    fresh = _training_fixture(tmp_path / "leak")
    leaked_rows = fresh["rows"]
    source = json.loads(leaked_rows[0]["messages"][1]["content"])
    source["model_prediction"] = "ADVERSE"
    source_text = driver.stable_json(source)
    leaked_rows[0]["messages"][1]["content"] = source_text
    leaked_rows[0]["metadata"]["content_sha256"] = _sha(source_text.encode("utf-8"))
    leaked_sha = _bound_write(fresh["dataset"], _jsonl_bytes(leaked_rows))
    fresh["manifest"]["outputs"]["trainable_balanced"]["sha256"] = leaked_sha
    fresh["manifest_sha"] = _bound_write(
        fresh["manifest_path"], _json_bytes(fresh["manifest"])
    )
    with pytest.raises(ValueError, match="prohibited source keys"):
        _run(
            {**fresh, "dataset_sha": leaked_sha},
            base_model,
            tmp_path / "source-leak",
            dry_run=True,
        )

    structured = _training_fixture(tmp_path / "structured-leak")
    structured_rows = structured["rows"]
    source = json.loads(structured_rows[0]["messages"][1]["content"])
    source["nested"] = {"market_audit": {"return_2h": -0.21}}
    source_text = driver.stable_json(source)
    structured_rows[0]["messages"][1]["content"] = source_text
    structured_rows[0]["metadata"]["content_sha256"] = _sha(
        source_text.encode("utf-8")
    )
    structured_sha = _bound_write(
        structured["dataset"], _jsonl_bytes(structured_rows)
    )
    structured["manifest"]["outputs"]["trainable_balanced"][
        "sha256"
    ] = structured_sha
    structured["manifest_sha"] = _bound_write(
        structured["manifest_path"], _json_bytes(structured["manifest"])
    )
    with pytest.raises(ValueError, match="prohibited post-event supervision"):
        _run(
            {**structured, "dataset_sha": structured_sha},
            base_model,
            tmp_path / "structured-source-leak",
            dry_run=True,
        )


def _fake_training_stack(records: dict[str, Any]) -> dict[str, Any]:
    class FakeTensor:
        def __init__(self, dtype: str) -> None:
            self.dtype = dtype

        def to(self, *, dtype: str) -> "FakeTensor":
            return FakeTensor(dtype)

    class FakeParameter:
        def __init__(self, dtype: str, *, requires_grad: bool, size: int) -> None:
            self.data = FakeTensor(dtype)
            self.requires_grad = requires_grad
            self._size = size

        @property
        def dtype(self) -> str:
            return self.data.dtype

        def numel(self) -> int:
            return self._size

    class FakeTorch:
        float16 = "torch.float16"
        bfloat16 = "torch.bfloat16"
        float32 = "torch.float32"
        uint8 = "torch.uint8"

    class FakeBitsAndBytesConfig:
        def __init__(self, **kwargs: Any) -> None:
            records["quantization"] = kwargs

    class FakeModel:
        def __init__(self) -> None:
            self.config = SimpleNamespace(use_cache=True)
            self.parameters = [
                ("base.weight", FakeParameter("torch.uint8", requires_grad=False, size=32)),
                ("lora_a", FakeParameter("torch.bfloat16", requires_grad=True, size=8)),
                ("lora_b", FakeParameter("torch.float32", requires_grad=True, size=12)),
            ]

        def named_parameters(self) -> list[tuple[str, FakeParameter]]:
            return self.parameters

    class FakeAutoModel:
        @classmethod
        def from_pretrained(cls, path: str, **kwargs: Any) -> FakeModel:
            records["model_load"] = {"path": path, **kwargs}
            return FakeModel()

    class FakePeftModel:
        @classmethod
        def from_pretrained(
            cls, model: Any, path: str, *, is_trainable: bool
        ) -> Any:
            records["peft_resume"] = {
                "model": model,
                "path": path,
                "is_trainable": is_trainable,
            }
            return model

    class FakeLoraConfig:
        def __init__(self, **kwargs: Any) -> None:
            records["new_lora"] = kwargs

    class FakeDataset:
        @classmethod
        def from_list(cls, examples: list[dict[str, Any]]) -> list[dict[str, Any]]:
            records["examples"] = examples
            return examples

    class FakeSFTConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            records["sft_config"] = kwargs

    class FakeTrainer:
        def __init__(self, **kwargs: Any) -> None:
            records["trainer"] = kwargs
            self.model = kwargs["model"]
            # Mirror TRL 0.29.1: constructing SFTTrainer around a quantized
            # PEFT model coerces every trainable adapter tensor to BF16.
            for _name, parameter in self.model.named_parameters():
                if parameter.requires_grad:
                    parameter.data = parameter.data.to(dtype=FakeTorch.bfloat16)
            self.state = SimpleNamespace(global_step=7)

        def train(self) -> Any:
            return SimpleNamespace(metrics={"train_loss": 0.25})

        def save_state(self) -> None:
            records["state_saved"] = True

        def save_model(self, path: str) -> None:
            target = Path(path)
            target.mkdir()
            (target / "adapter_config.json").write_text(
                json.dumps(
                    {
                        "base_model_name_or_path": records["model_load"]["path"],
                        "peft_type": "LORA",
                        "task_type": "CAUSAL_LM",
                        "bias": "none",
                        "r": 8,
                        "lora_alpha": 32,
                        "lora_dropout": 0.05,
                        "target_modules": sorted(driver.QWEN_ALL_LINEAR_TARGETS),
                        "use_rslora": False,
                    }
                ),
                encoding="utf-8",
            )
            (target / "adapter_model.safetensors").write_bytes(b"trained")

    def prepare(model: Any, **kwargs: Any) -> Any:
        records["prepare"] = kwargs
        return model

    def set_seed(seed: int, *, deterministic: bool) -> None:
        records["seed"] = {"seed": seed, "deterministic": deterministic}

    return {
        "torch": FakeTorch,
        "Dataset": FakeDataset,
        "LoraConfig": FakeLoraConfig,
        "PeftModel": FakePeftModel,
        "prepare_model_for_kbit_training": prepare,
        "AutoModelForCausalLM": FakeAutoModel,
        "BitsAndBytesConfig": FakeBitsAndBytesConfig,
        "set_seed": set_seed,
        "SFTConfig": FakeSFTConfig,
        "SFTTrainer": FakeTrainer,
        "runtime": {"imported_versions": dict(driver.REQUIRED_PACKAGE_VERSIONS)},
    }


@pytest.mark.parametrize("resume", [False, True])
def test_training_wiring_is_qlora_train_only_and_resume_is_trainable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, resume: bool
) -> None:
    fixture = _training_fixture(tmp_path)
    base_model = _base_model(tmp_path)
    adapter = _init_adapter(tmp_path, base_model) if resume else None
    output = tmp_path / ("train-resume" if resume else "train-new")
    records: dict[str, Any] = {}
    _patch_preflight(monkeypatch)
    monkeypatch.setattr(driver, "_load_training_stack", lambda: _fake_training_stack(records))

    manifest = _run(
        fixture,
        base_model,
        output,
        init_adapter=adapter,
        dry_run=False,
    )

    assert manifest["status"] == "COMPLETED"
    assert manifest["training_completed"] is True
    assert records["seed"] == {"seed": 42, "deterministic": True}
    assert records["quantization"] == {
        "load_in_4bit": True,
        "bnb_4bit_quant_type": "nf4",
        "bnb_4bit_use_double_quant": True,
        "bnb_4bit_compute_dtype": "torch.float16",
        "bnb_4bit_quant_storage": "torch.uint8",
    }
    assert records["model_load"]["device_map"] == {"": 0}
    assert records["model_load"]["local_files_only"] is True
    assert records["sft_config"]["eval_strategy"] == "no"
    assert records["sft_config"]["do_eval"] is False
    assert records["sft_config"]["load_best_model_at_end"] is False
    assert records["sft_config"]["completion_only_loss"] is True
    assert records["sft_config"]["warmup_steps"] == pytest.approx(0.08)
    assert "warmup_ratio" not in records["sft_config"]
    assert records["trainer"]["eval_dataset"] is None
    assert manifest["result"]["trainable_dtype_normalization"] == {
        "target_dtype": "torch.float32",
        "parameter_tensor_count": 2,
        "parameter_numel": 20,
        "converted_tensor_count": 2,
        "converted_numel": 20,
        "before_numel_by_dtype": {"torch.bfloat16": 20},
        "after_numel_by_dtype": {"torch.float32": 20},
        "frozen_parameters_untouched": True,
    }
    assert records["trainer"]["model"].parameters[0][1].dtype == "torch.uint8"
    if resume:
        assert records["peft_resume"]["is_trainable"] is True
        assert records["trainer"]["peft_config"] is None
        assert "new_lora" not in records
    else:
        assert "peft_resume" not in records
        assert records["trainer"]["peft_config"].__class__.__name__ == "FakeLoraConfig"
    final_adapter = output / driver.FINAL_ADAPTER_DIR
    assert (final_adapter / driver.ADAPTER_CONTRACT_NAME).is_file()
    assert manifest["result"]["final_adapter"]["fingerprint"]["file_count"] == 2
    assert manifest["adapter_output_contract"]["adapter_fingerprint_sha256"] == (
        manifest["result"]["final_adapter"]["fingerprint"]["sha256"]
    )


def test_training_failure_removes_only_owned_staging_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(tmp_path)
    base_model = _base_model(tmp_path)
    output = tmp_path / "failed-training"
    stage = output.parent / f".{output.name}.tmp"
    _patch_preflight(monkeypatch)

    def fail_stack() -> dict[str, Any]:
        raise RuntimeError("simulated training stack failure")

    monkeypatch.setattr(driver, "_load_training_stack", fail_stack)
    with pytest.raises(RuntimeError, match="simulated"):
        _run(fixture, base_model, output, dry_run=False)
    assert not output.exists()
    assert not stage.exists()


def test_legacy_init_adapter_revalidates_only_declared_train_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _training_fixture(tmp_path)
    base_model = _base_model(tmp_path)
    adapter = _init_adapter(tmp_path, base_model)
    (adapter / driver.ADAPTER_CONTRACT_NAME).unlink()
    (adapter / (driver.ADAPTER_CONTRACT_NAME + ".sha256")).unlink()
    legacy_dataset = adapter / "legacy-train.jsonl"
    target = {"materiality": "UNCLEAR", "polarity": "UNCLEAR"}
    source_text = driver.stable_json({"headline": "source"})
    legacy_row = {
        "messages": [
            {"role": "system", "content": QWEN_WEAK_SYSTEM_PROMPT},
            {"role": "user", "content": source_text},
            {"role": "assistant", "content": driver.stable_json(target)},
        ],
        "metadata": {
            "sample_id": "legacy-train-one",
            "split": "TRAIN",
            "target_contract": driver.TARGET_CONTRACT,
            "model_output_contract": driver.MODEL_OUTPUT_CONTRACT,
            "prompt_version": QWEN_WEAK_PROMPT_VERSION,
            "prompt_sha256": QWEN_WEAK_PROMPT_SHA256,
            "content_sha256": _sha(source_text.encode("utf-8")),
            "qwen_prediction_included": False,
            "post_event_market_data_included": False,
            "evidence_state_used_as_model_target": False,
            "human_gold_claimed": False,
            "semantic_target": expected_semantic_payload(
                target["materiality"], target["polarity"]
            ),
        },
    }
    legacy_dataset.write_bytes(_jsonl_bytes([legacy_row]))
    (adapter / "args.json").write_bytes(
        _json_bytes(
            {
                "model": str(base_model.resolve()),
                "dataset": [legacy_dataset.name],
                "val_dataset": [],
            }
        )
    )
    _patch_preflight(monkeypatch)
    manifest = _run(
        fixture,
        base_model,
        tmp_path / "legacy-resume-dry",
        init_adapter=adapter,
        dry_run=True,
    )
    binding = manifest["initial_adapter"]["target_contract_binding"]
    assert binding["binding_method"] == "LEGACY_ARGS_TRAIN_DATASET_REVALIDATED"
    assert binding["train_dataset"]["row_count"] == 1


def test_float32_does_not_enable_fp16() -> None:
    resolved = driver._resolve_hyperparameters(
        adapter_config=None,
        seed=42,
        lora_r=None,
        lora_alpha=None,
        lora_dropout=None,
        target_modules=None,
        epochs=2,
        learning_rate=2e-5,
        warmup_ratio=0.08,
        weight_decay=0.1,
        gradient_accumulation_steps=16,
        max_length=2048,
        save_steps=20,
        save_total_limit=8,
        logging_steps=5,
        lr_scheduler_type="cosine",
        max_grad_norm=1.0,
        compute_dtype="float32",
        optimizer="adamw_torch_fused",
    )
    assert resolved["training"]["fp16"] is False
    assert resolved["training"]["bf16"] is False


def test_transformers_five_batch_encoding_token_ids_are_supported() -> None:
    assert driver._chat_template_token_ids(
        {"input_ids": [151644, 42], "attention_mask": [1, 1]},
        label="unit test",
    ) == [151644, 42]
    with pytest.raises(ValueError, match="invalid input_ids"):
        driver._chat_template_token_ids(
            {"input_ids": [[151644, 42]]}, label="unit test"
        )
