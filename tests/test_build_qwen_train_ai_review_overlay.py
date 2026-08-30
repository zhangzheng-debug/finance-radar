from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.models.qwen_weak_supervision_contract import (
    QWEN_WEAK_PROMPT_VERSION,
    QWEN_WEAK_SUPERVISION_VERSION,
    QWEN_WEAK_SYSTEM_PROMPT,
)
from scripts import build_qwen_dev_ai_review_overlay as shared
from scripts import build_qwen_train_ai_review_overlay as overlay


PROMPT = QWEN_WEAK_SYSTEM_PROMPT
PROMPT_VERSION = QWEN_WEAK_PROMPT_VERSION


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _review_row(
    sample_id: str,
    pair: tuple[str, str],
    slot: str,
) -> dict:
    materiality, polarity = pair
    review_class = (
        "INDEPENDENT_AI_ARBITRATION_NOT_HUMAN_GOLD"
        if slot == "C"
        else "INDEPENDENT_AI_REVIEW_NOT_HUMAN_GOLD"
    )
    return {
        "sample_id": sample_id,
        "materiality": materiality,
        "polarity": polarity,
        "reason": "The supplied source text supports this semantic pair.",
        "review_class": review_class,
    }


def _policy_payload(*, routine_multiplier: int = 2) -> dict:
    multipliers = {key: 1 for key in overlay.TRAINABLE_PAIR_KEYS}
    multipliers["NOT_MATERIAL_ADVERSE|NEUTRAL"] = routine_multiplier
    return {
        "contract_version": overlay.PAIR_MULTIPLIER_CONTRACT_VERSION,
        "policy_version": "test-balanced-v1",
        "multipliers": multipliers,
    }


def _fixture(
    tmp_path: Path,
    *,
    count: int = overlay.EXPECTED_UNIQUE_ROW_COUNT,
) -> tuple[dict[str, Path], list[str]]:
    paths = {
        "train_sft": tmp_path / "train-unique.jsonl",
        "source_only": tmp_path / "source-only.jsonl",
        "review_a": tmp_path / "review-a.jsonl",
        "review_b": tmp_path / "review-b.jsonl",
        "review_c": tmp_path / "review-c.jsonl",
        "pair_policy": tmp_path / "pair-policy.json",
        "output_dir": tmp_path / "overlay",
    }
    prompt_sha = hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()
    ids = [f"train-sample-{index:03d}" for index in range(count)]
    train_rows: list[dict] = []
    source_rows: list[dict] = []
    review_a_rows: list[dict] = []
    review_b_rows: list[dict] = []
    review_c_rows: list[dict] = []
    for index, sample_id in enumerate(ids):
        content = {
            "as_of": "2026-08-30T00:00:00+00:00",
            "event_date": "2026-08-30",
            "headline": f"TRAIN source headline {index}",
            "summary": "Contemporaneous source-only semantic evidence.",
            "passages": [
                {
                    "document_type": "8-K",
                    "item_section": "1.01",
                    "published_at": (
                        "2026-08-30T01:30:00+00:00"
                        if index == 0
                        else "2026-08-30"
                    ),
                    "passage": f"Exact contemporaneous passage {index}.",
                }
            ],
        }
        content_sha = hashlib.sha256(
            overlay.stable_json(content).encode("utf-8")
        ).hexdigest()
        train_rows.append(
            {
                "messages": [
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": overlay.stable_json(content)},
                    {
                        "role": "assistant",
                        "content": "ORIGINAL WEAK TARGET MUST NOT BE PARSED",
                    },
                ],
                "metadata": {
                    "sample_id": sample_id,
                    "event_id": f"event-{index:03d}",
                    "entity_group": f"issuer-{index:03d}",
                    "event_chain_group": f"chain-{index:03d}",
                    "content_sha256": content_sha,
                    "split": "TRAIN",
                    "target_contract": "core-v1",
                    "model_output_contract": "core-axes-v1",
                    "weak_supervision_version": QWEN_WEAK_SUPERVISION_VERSION,
                    "semantic_target": {"weak_truth": "MUST_NOT_BE_USED"},
                    "prompt_version": PROMPT_VERSION,
                    "prompt_sha256": prompt_sha,
                    "weak_rule": "MUST_NOT_BE_COPIED",
                    "human_gold_claimed": False,
                    "qwen_prediction_included": False,
                    "post_event_market_data_included": False,
                    "evidence_state_used_as_model_target": False,
                },
            }
        )
        source_rows.append({"sample_id": sample_id, "content": content})
        if index == 0:
            a_pair = ("MATERIAL_ADVERSE", "ADVERSE")
            b_pair = ("NOT_MATERIAL_ADVERSE", "NEUTRAL")
            c_pair = ("NOT_MATERIAL_ADVERSE", "POSITIVE")
            review_c_rows.append(_review_row(sample_id, c_pair, "C"))
        elif index == 1:
            a_pair = b_pair = ("UNCLEAR", "ADVERSE")
        elif index == 2:
            a_pair = b_pair = ("MATERIAL_ADVERSE", "UNCLEAR")
        else:
            a_pair = b_pair = ("NOT_MATERIAL_ADVERSE", "NEUTRAL")
        review_a_rows.append(_review_row(sample_id, a_pair, "A"))
        review_b_rows.append(_review_row(sample_id, b_pair, "B"))

    _write_jsonl(paths["train_sft"], train_rows)
    _write_jsonl(paths["source_only"], source_rows)
    _write_jsonl(paths["review_a"], review_a_rows)
    _write_jsonl(paths["review_b"], review_b_rows)
    _write_jsonl(paths["review_c"], review_c_rows)
    paths["pair_policy"].write_text(
        json.dumps(_policy_payload(), ensure_ascii=False),
        encoding="utf-8",
    )
    return paths, ids


def _hardware_plan(*, max_length: int = 1280) -> dict:
    target_modules = [
        "down_proj",
        "gate_proj",
        "k_proj",
        "o_proj",
        "q_proj",
        "up_proj",
        "v_proj",
    ]
    return {
        "contract_version": overlay.SEQUENCE_LENGTH_HARDWARE_PLAN_CONTRACT,
        "quantization": {
            "load_in_4bit": True,
            "bnb_4bit_quant_type": "nf4",
            "bnb_4bit_use_double_quant": True,
            "bnb_4bit_compute_dtype": "float16",
            "bnb_4bit_quant_storage": "uint8",
        },
        "lora": {
            "r": 8,
            "lora_alpha": 32,
            "lora_dropout": 0.05,
            "target_modules": target_modules,
        },
        "training": {
            "per_device_train_batch_size": 1,
            "gradient_accumulation_steps": 16,
            "max_length": max_length,
            "optimizer": "paged_adamw_8bit",
            "gradient_checkpointing": True,
        },
    }


def _sequence_evidence(
    paths: dict[str, Path],
    sample_id: str,
    *,
    measured_full_tokens: int = 1300,
    max_length: int = 1280,
) -> dict:
    source_row = next(
        row
        for row in _read_jsonl(paths["train_sft"])
        if row["metadata"]["sample_id"] == sample_id
    )
    source_row_sha = hashlib.sha256(
        overlay.stable_json(source_row).encode("utf-8")
    ).hexdigest()
    plan = _hardware_plan(max_length=max_length)
    plan_sha = hashlib.sha256(
        overlay.stable_json(plan).encode("utf-8")
    ).hexdigest()
    evidence = {
        "measured_full_tokens": measured_full_tokens,
        "max_length": max_length,
        "source_unique_row_sha256": source_row_sha,
        "unique_dataset_sha256": hashlib.sha256(
            paths["train_sft"].read_bytes()
        ).hexdigest(),
        "base_model_weights_sha256": "a" * 64,
        "tokenizer_bundle_sha256": "b" * 64,
        "chat_template_sha256": "c" * 64,
        "measurement_tool_version": "test-token-audit-v1",
        "target_modules": list(plan["lora"]["target_modules"]),
        "hardware_plan": plan,
        "hardware_plan_sha256": plan_sha,
    }
    receipt = {
        "sample_id": sample_id,
        "reason_code": overlay.SEQUENCE_LENGTH_HARDWARE_EXCLUSION,
        **evidence,
    }
    evidence["token_audit_receipt_sha256"] = hashlib.sha256(
        overlay.stable_json(receipt).encode("utf-8")
    ).hexdigest()
    return evidence


def _quality_payload(entries: list[dict], *, version: str) -> dict:
    return {
        "contract_version": version,
        "label_classification": overlay.LABEL_CLASSIFICATION,
        "entries": entries,
    }


def _write_quality_payload(paths: dict[str, Path], payload: dict) -> Path:
    path = paths["train_sft"].with_name("quality-exclusions.json")
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _build_json_policy(paths: dict[str, Path]) -> dict:
    return overlay.build_overlay(
        train_sft=paths["train_sft"],
        source_only=paths["source_only"],
        review_a=paths["review_a"],
        review_b=paths["review_b"],
        review_c=paths["review_c"],
        output_dir=paths["output_dir"],
        pair_multipliers_json=paths["pair_policy"],
    )


def test_builds_unique_and_balanced_train_overlays(tmp_path: Path) -> None:
    paths, ids = _fixture(tmp_path)

    manifest = _build_json_policy(paths)
    unique_path = paths["output_dir"] / overlay.UNIQUE_OUTPUT_NAME
    trainable_path = paths["output_dir"] / overlay.TRAINABLE_OUTPUT_NAME
    unique_rows = _read_jsonl(unique_path)
    trainable_rows = _read_jsonl(trainable_path)

    assert len(unique_rows) == 729
    assert [row["metadata"]["sample_id"] for row in unique_rows] == ids
    assert len(trainable_rows) == 1455
    assert json.loads(unique_rows[1]["messages"][-1]["content"]) == {
        "materiality": "UNCLEAR",
        "polarity": "ADVERSE",
    }
    assert json.loads(unique_rows[2]["messages"][-1]["content"]) == {
        "materiality": "MATERIAL_ADVERSE",
        "polarity": "UNCLEAR",
    }
    assert unique_rows[1]["metadata"]["training_eligibility"] == {
        "eligible": True,
        "exclusion_reason": None,
        "labels_rewritten": False,
        "pair_multiplier": 1,
    }
    assert unique_rows[2]["metadata"]["training_eligibility"] == {
        "eligible": True,
        "exclusion_reason": None,
        "labels_rewritten": False,
        "pair_multiplier": 1,
    }
    assert any(
        "UNCLEAR" in json.loads(row["messages"][-1]["content"]).values()
        for row in trainable_rows
    )
    assert all(
        row["metadata"]["label_classification"]
        == "AI_REVIEW_NOT_HUMAN_GOLD"
        for row in unique_rows + trainable_rows
    )
    assert unique_rows[0]["metadata"]["review_resolution"][
        "decision_source"
    ] == "C_ARBITRATION"
    assert unique_rows[3]["metadata"]["review_resolution"][
        "decision_source"
    ] == "A_B_CONSENSUS"

    routine_replicas = [
        row
        for row in trainable_rows
        if row["metadata"]["sample_id"] == ids[3]
    ]
    assert [
        row["metadata"]["training_replica"]["replica_index"]
        for row in routine_replicas
    ] == [1, 2]
    unique_row_sha = hashlib.sha256(
        overlay.stable_json(unique_rows[3]).encode("utf-8")
    ).hexdigest()
    assert all(
        row["metadata"]["training_replica"]["source_unique_row_sha256"]
        == unique_row_sha
        and row["metadata"]["training_replica"]["source_unique_sample_id"]
        == ids[3]
        and row["metadata"]["training_replica"]["labels_rewritten"] is False
        for row in routine_replicas
    )

    assert manifest["outputs"]["unique_audit"]["row_count"] == 729
    assert manifest["outputs"]["trainable_balanced"]["row_count"] == 1455
    assert manifest["outputs"]["trainable_balanced"][
        "unique_source_row_count"
    ] == 729
    assert manifest["trainability_policy"]["unclear_training_enabled"] is True
    assert manifest["trainability_policy"]["unclear_labels_rewritten"] is False
    assert manifest["trainability_policy"]["exclusion_reasons"] == {}
    assert manifest["distributions"]["unique_audit"]["pair"] == {
        "MATERIAL_ADVERSE|UNCLEAR": 1,
        "NOT_MATERIAL_ADVERSE|NEUTRAL": 726,
        "NOT_MATERIAL_ADVERSE|POSITIVE": 1,
        "UNCLEAR|ADVERSE": 1,
    }
    assert manifest["distributions"]["trainable_effective"]["pair"] == {
        "MATERIAL_ADVERSE|UNCLEAR": 1,
        "NOT_MATERIAL_ADVERSE|NEUTRAL": 1452,
        "NOT_MATERIAL_ADVERSE|POSITIVE": 1,
        "UNCLEAR|ADVERSE": 1,
    }
    assert manifest["pair_multiplier_policy"]["source"] == (
        "EXPLICIT_JSON_FILE"
    )
    assert manifest["pair_multiplier_policy"][
        "policy_design_provenance"
    ] == overlay.EXPLICIT_POLICY_DESIGN_PROVENANCE
    assert manifest["pair_multiplier_policy"][
        "builder_runtime_input_isolation"
    ] == overlay.BUILDER_RUNTIME_INPUT_ISOLATION
    assert manifest["pair_multiplier_policy"]["input_file"]["sha256"] == (
        hashlib.sha256(paths["pair_policy"].read_bytes()).hexdigest()
    )
    assert hashlib.sha256(unique_path.read_bytes()).hexdigest() == manifest[
        "outputs"
    ]["unique_audit"]["sha256"]
    assert hashlib.sha256(trainable_path.read_bytes()).hexdigest() == manifest[
        "outputs"
    ]["trainable_balanced"]["sha256"]


def test_explicit_neutral_versioned_preset_is_one_copy(tmp_path: Path) -> None:
    paths, _ = _fixture(tmp_path)

    manifest = overlay.build_overlay(
        train_sft=paths["train_sft"],
        source_only=paths["source_only"],
        review_a=paths["review_a"],
        review_b=paths["review_b"],
        review_c=paths["review_c"],
        output_dir=paths["output_dir"],
        pair_multiplier_preset="neutral-1x-v1",
    )

    assert manifest["pair_multiplier_policy"]["policy_version"] == (
        "neutral-1x-v1"
    )
    assert manifest["pair_multiplier_policy"]["source"] == "VERSIONED_PRESET"
    assert manifest["pair_multiplier_policy"]["input_file"] is None
    assert manifest["pair_multiplier_policy"][
        "policy_design_provenance"
    ] == overlay.NEUTRAL_POLICY_DESIGN_PROVENANCE
    assert manifest["outputs"]["trainable_balanced"]["row_count"] == 729
    assert set(manifest["pair_multiplier_policy"]["multipliers"].values()) == {1}


def test_v13_train_only_curriculum_weights_consensus_but_not_c(
    tmp_path: Path,
) -> None:
    expected_pair_multipliers = {
        key: 1 for key in overlay.TRAINABLE_PAIR_KEYS
    }
    expected_pair_multipliers.update(
        {
            "MATERIAL_ADVERSE|ADVERSE": 2,
            "NOT_MATERIAL_ADVERSE|NEUTRAL": 2,
            "NOT_MATERIAL_ADVERSE|POSITIVE": 1,
            "NOT_MATERIAL_ADVERSE|MIXED": 2,
            "NOT_MATERIAL_ADVERSE|ADVERSE": 3,
            "UNCLEAR|UNCLEAR": 2,
        }
    )
    assert overlay.V13_CONSENSUS_PAIR_MULTIPLIERS == (
        expected_pair_multipliers
    )
    paths, ids = _fixture(tmp_path)
    consensus_pairs = {
        ids[3]: ("MATERIAL_ADVERSE", "ADVERSE"),
        ids[4]: ("NOT_MATERIAL_ADVERSE", "NEUTRAL"),
        ids[5]: ("NOT_MATERIAL_ADVERSE", "POSITIVE"),
        ids[6]: ("NOT_MATERIAL_ADVERSE", "MIXED"),
        ids[7]: ("NOT_MATERIAL_ADVERSE", "ADVERSE"),
        ids[8]: ("UNCLEAR", "UNCLEAR"),
    }
    for review_path in (paths["review_a"], paths["review_b"]):
        rows = _read_jsonl(review_path)
        for row in rows:
            pair = consensus_pairs.get(row["sample_id"])
            if pair is not None:
                row["materiality"], row["polarity"] = pair
        _write_jsonl(review_path, rows)
    c_rows = _read_jsonl(paths["review_c"])
    assert len(c_rows) == 1
    c_rows[0]["materiality"] = "NOT_MATERIAL_ADVERSE"
    c_rows[0]["polarity"] = "ADVERSE"
    _write_jsonl(paths["review_c"], c_rows)

    manifest = overlay.build_overlay(
        train_sft=paths["train_sft"],
        source_only=paths["source_only"],
        review_a=paths["review_a"],
        review_b=paths["review_b"],
        review_c=paths["review_c"],
        output_dir=paths["output_dir"],
        pair_multiplier_preset=overlay.V13_CURRICULUM_PRESET,
    )
    unique_rows = _read_jsonl(
        paths["output_dir"] / overlay.UNIQUE_OUTPUT_NAME
    )
    trainable_rows = _read_jsonl(
        paths["output_dir"] / overlay.TRAINABLE_OUTPUT_NAME
    )

    replica_counts: dict[str, int] = {}
    for row in trainable_rows:
        sample_id = row["metadata"]["sample_id"]
        replica_counts[sample_id] = replica_counts.get(sample_id, 0) + 1
    assert replica_counts[ids[0]] == 1  # C is fixed 1x despite NMA|ADV=3x.
    assert {
        sample_id: replica_counts[sample_id]
        for sample_id in consensus_pairs
    } == {
        ids[3]: 2,
        ids[4]: 2,
        ids[5]: 1,
        ids[6]: 2,
        ids[7]: 3,
        ids[8]: 2,
    }
    assert unique_rows[0]["metadata"]["review_resolution"][
        "decision_source"
    ] == "C_ARBITRATION"
    assert unique_rows[0]["metadata"]["training_eligibility"][
        "pair_multiplier"
    ] == 1
    assert all(
        row["metadata"]["label_classification"]
        == "AI_REVIEW_NOT_HUMAN_GOLD"
        for row in unique_rows + trainable_rows
    )

    curriculum = manifest["curriculum"]
    assert curriculum["enabled"] is True
    assert curriculum["version"] == overlay.V13_CURRICULUM_VERSION
    assert curriculum["split_scope"] == "TRAIN_ONLY"
    assert curriculum["unique_source_row_count"] == 729
    assert curriculum["a_b_clean_consensus"]["unique_row_count"] == 728
    assert curriculum["a_b_clean_consensus"]["effective_row_count"] == 1454
    assert curriculum["c_arbitration"] == {
        "unique_row_count": 1,
        "effective_row_count": 1,
        "fixed_multiplier": 1,
    }
    assert curriculum["input_isolation"] == {
        "train_only": True,
        "dev_metrics_read": False,
        "qwen_predictions_read": False,
        "market_results_read": False,
        "sealed_benchmark_read": False,
    }
    assert manifest["outputs"]["unique_audit"]["row_count"] == 729
    assert manifest["outputs"]["trainable_balanced"]["row_count"] == 1455
    assert manifest["distributions"]["trainable_effective"]["pair"] == {
        "MATERIAL_ADVERSE|ADVERSE": 2,
        "MATERIAL_ADVERSE|UNCLEAR": 1,
        "NOT_MATERIAL_ADVERSE|ADVERSE": 4,
        "NOT_MATERIAL_ADVERSE|MIXED": 2,
        "NOT_MATERIAL_ADVERSE|NEUTRAL": 1442,
        "NOT_MATERIAL_ADVERSE|POSITIVE": 1,
        "UNCLEAR|ADVERSE": 1,
        "UNCLEAR|UNCLEAR": 2,
    }
    assert curriculum["effective_distribution"] == manifest[
        "distributions"
    ]["trainable_effective"]
    assert manifest["isolation"]["dev_metrics_read"] is False
    assert manifest["isolation"]["qwen_predictions_read"] is False
    assert manifest["isolation"]["market_results_read"] is False
    assert manifest["isolation"]["sealed_benchmark_read"] is False
    assert manifest["pair_multiplier_policy"][
        "policy_design_provenance"
    ] == overlay.V13_POLICY_DESIGN_PROVENANCE
    assert manifest["pair_multiplier_policy"][
        "builder_runtime_input_isolation"
    ] == overlay.BUILDER_RUNTIME_INPUT_ISOLATION


def test_v13_excludes_numeric_table_sources_only_from_trainable_view(
    tmp_path: Path,
) -> None:
    paths, ids = _fixture(tmp_path)
    numeric_blob = "1234567890" * 240
    train_rows = _read_jsonl(paths["train_sft"])
    source_rows = _read_jsonl(paths["source_only"])
    for index in (3, 4):
        source_rows[index]["content"]["numeric_table"] = numeric_blob
        train_content = json.loads(train_rows[index]["messages"][1]["content"])
        train_content["numeric_table"] = numeric_blob
        train_text = overlay.stable_json(train_content)
        train_rows[index]["messages"][1]["content"] = train_text
        train_rows[index]["metadata"]["content_sha256"] = hashlib.sha256(
            train_text.encode("utf-8")
        ).hexdigest()
    _write_jsonl(paths["train_sft"], train_rows)
    _write_jsonl(paths["source_only"], source_rows)

    manifest = overlay.build_overlay(
        train_sft=paths["train_sft"],
        source_only=paths["source_only"],
        review_a=paths["review_a"],
        review_b=paths["review_b"],
        review_c=paths["review_c"],
        output_dir=paths["output_dir"],
        pair_multiplier_preset=overlay.V13_CURRICULUM_PRESET,
    )
    unique_rows = _read_jsonl(
        paths["output_dir"] / overlay.UNIQUE_OUTPUT_NAME
    )
    trainable_rows = _read_jsonl(
        paths["output_dir"] / overlay.TRAINABLE_OUTPUT_NAME
    )

    assert len(unique_rows) == 729
    assert len(trainable_rows) == 1451
    assert {row["metadata"]["sample_id"] for row in trainable_rows}.isdisjoint(
        {ids[3], ids[4]}
    )
    for index in (3, 4):
        metadata = unique_rows[index]["metadata"]
        assert metadata["training_eligibility"]["eligible"] is False
        assert metadata["training_eligibility"]["exclusion_reason"] == (
            overlay.NUMERIC_TABLE_EXCLUSION_REASON
        )
        assert metadata["source_structure"]["numeric_table_dominated"] is True
    policy = manifest["trainability_policy"]
    assert manifest["outputs"]["trainable_balanced"][
        "unique_source_row_count"
    ] == 727
    assert policy["original_unique_row_count"] == 729
    assert policy["trainable_unique_row_count"] == 727
    assert policy["excluded_unique_row_count"] == 2
    assert policy["excluded_effective_replica_count"] == 4
    assert policy["pre_exclusion_effective_row_count"] == 1455
    assert policy["trainable_effective_row_count"] == 1451
    assert policy["exclusion_reasons"] == {
        overlay.NUMERIC_TABLE_EXCLUSION_REASON: 2
    }
    assert policy["trainable_resolution_counts"] == {
        "A_B_CONSENSUS": 726,
        "C_ARBITRATION": 1,
    }
    assert policy["excluded_resolution_counts"] == {"A_B_CONSENSUS": 2}
    assert policy["excluded_pair_resolution_counts"] == {
        "A_B_CONSENSUS::NOT_MATERIAL_ADVERSE|NEUTRAL": 2
    }
    assert policy["source_structure_exclusion"] == {
        "enabled": True,
        "reason": overlay.NUMERIC_TABLE_EXCLUSION_REASON,
        "stable_json_character_count_min": (
            overlay.NUMERIC_TABLE_MIN_STABLE_JSON_CHARS
        ),
        "digit_character_ratio_min": overlay.NUMERIC_TABLE_MIN_DIGIT_RATIO,
        "label_independent": True,
        "applies_to_preset": overlay.V13_CURRICULUM_PRESET,
    }
    membership = manifest["membership_commitment"]
    assert membership["contract_version"] == (
        overlay.TRAIN_MEMBERSHIP_COMMITMENT_CONTRACT
    )
    assert membership["original_unique"]["count"] == 729
    assert membership["trainable_unique"]["count"] == 727
    assert membership["excluded_complement"]["count"] == 2
    assert membership["numeric_exclusions"]["count"] == 2
    assert membership["quality_exclusions"]["count"] == 0
    assert membership["exclusion_classes_disjoint"] is True


def test_v1_remains_compatible_for_source_conflict_only(tmp_path: Path) -> None:
    paths, ids = _fixture(tmp_path)
    quality_path = _write_quality_payload(
        paths,
        _quality_payload(
            [
                {
                    "sample_id": ids[5],
                    "reason_code": "SOURCE_FIELD_CONFLICT",
                    "reason": "The two source fields conflict.",
                }
            ],
            version=overlay.QUALITY_EXCLUSIONS_CONTRACT_V1,
        ),
    )

    manifest = overlay.build_overlay(
        train_sft=paths["train_sft"],
        source_only=paths["source_only"],
        review_a=paths["review_a"],
        review_b=paths["review_b"],
        review_c=paths["review_c"],
        output_dir=paths["output_dir"],
        pair_multiplier_preset="neutral-1x-v1",
        quality_exclusions_json=quality_path,
    )

    assert manifest["quality_exclusions"]["contract_version"] == (
        overlay.QUALITY_EXCLUSIONS_CONTRACT_V1
    )
    assert manifest["outputs"]["trainable_balanced"][
        "unique_source_row_count"
    ] == 728
    assert manifest["membership_commitment"]["quality_exclusions"][
        "count"
    ] == 1


def test_v1_rejects_sequence_length_hardware_exclusion(tmp_path: Path) -> None:
    paths, ids = _fixture(tmp_path)
    quality_path = _write_quality_payload(
        paths,
        _quality_payload(
            [
                {
                    "sample_id": ids[6],
                    "reason_code": overlay.SEQUENCE_LENGTH_HARDWARE_EXCLUSION,
                    "reason": "This must not be accepted under v1.",
                    "evidence": _sequence_evidence(paths, ids[6]),
                }
            ],
            version=overlay.QUALITY_EXCLUSIONS_CONTRACT_V1,
        ),
    )

    with pytest.raises(ValueError, match="reason_code is invalid"):
        overlay.build_overlay(
            train_sft=paths["train_sft"],
            source_only=paths["source_only"],
            review_a=paths["review_a"],
            review_b=paths["review_b"],
            review_c=paths["review_c"],
            output_dir=paths["output_dir"],
            pair_multiplier_preset="neutral-1x-v1",
            quality_exclusions_json=quality_path,
        )
    assert not paths["output_dir"].exists()


def test_v2_hardware_evidence_and_membership_close_with_numeric_exclusions(
    tmp_path: Path,
) -> None:
    paths, ids = _fixture(tmp_path)
    numeric_blob = "1234567890" * 240
    train_rows = _read_jsonl(paths["train_sft"])
    source_rows = _read_jsonl(paths["source_only"])
    for index in (3, 4):
        source_rows[index]["content"]["numeric_table"] = numeric_blob
        train_content = json.loads(train_rows[index]["messages"][1]["content"])
        train_content["numeric_table"] = numeric_blob
        train_text = overlay.stable_json(train_content)
        train_rows[index]["messages"][1]["content"] = train_text
        train_rows[index]["metadata"]["content_sha256"] = hashlib.sha256(
            train_text.encode("utf-8")
        ).hexdigest()
    _write_jsonl(paths["train_sft"], train_rows)
    _write_jsonl(paths["source_only"], source_rows)

    entries = [
        {
            "sample_id": ids[5],
            "reason_code": "SOURCE_FIELD_CONFLICT",
            "reason": "The two source fields conflict.",
        }
    ]
    for index, measured in zip((6, 7, 8), (1344, 1321, 1313), strict=True):
        entries.append(
            {
                "sample_id": ids[index],
                "reason_code": overlay.SEQUENCE_LENGTH_HARDWARE_EXCLUSION,
                "reason": "A label-blind token audit exceeded the fixed ceiling.",
                "evidence": _sequence_evidence(
                    paths, ids[index], measured_full_tokens=measured
                ),
            }
        )
    quality_path = _write_quality_payload(
        paths,
        _quality_payload(
            entries,
            version=overlay.QUALITY_EXCLUSIONS_CONTRACT_V2,
        ),
    )

    manifest = overlay.build_overlay(
        train_sft=paths["train_sft"],
        source_only=paths["source_only"],
        review_a=paths["review_a"],
        review_b=paths["review_b"],
        review_c=paths["review_c"],
        output_dir=paths["output_dir"],
        pair_multiplier_preset=overlay.V13_CURRICULUM_PRESET,
        quality_exclusions_json=quality_path,
    )
    unique_rows = _read_jsonl(paths["output_dir"] / overlay.UNIQUE_OUTPUT_NAME)
    trainable_rows = _read_jsonl(
        paths["output_dir"] / overlay.TRAINABLE_OUTPUT_NAME
    )

    assert len(unique_rows) == 729
    assert len({row["metadata"]["sample_id"] for row in trainable_rows}) == 723
    assert len(trainable_rows) == 1443
    assert manifest["quality_exclusions"]["reason_code_counts"] == {
        overlay.SEQUENCE_LENGTH_HARDWARE_EXCLUSION: 3,
        "SOURCE_FIELD_CONFLICT": 1,
    }
    assert manifest["trainability_policy"]["exclusion_reasons"] == {
        overlay.NUMERIC_TABLE_EXCLUSION_REASON: 2,
        overlay.SEQUENCE_LENGTH_HARDWARE_EXCLUSION: 3,
        "SOURCE_FIELD_CONFLICT": 1,
    }
    membership = manifest["membership_commitment"]
    assert membership["original_unique"]["count"] == 729
    assert membership["trainable_unique"]["count"] == 723
    assert membership["excluded_complement"]["count"] == 6
    assert membership["numeric_exclusions"]["count"] == 2
    assert membership["quality_exclusions"]["count"] == 4
    assert membership["exclusion_classes_disjoint"] is True
    sequence_row = unique_rows[6]["metadata"]
    assert sequence_row["quality_exclusion"]["contract_version"] == (
        overlay.QUALITY_EXCLUSIONS_CONTRACT_V2
    )
    assert sequence_row["quality_exclusion"]["evidence"][
        "measured_full_tokens"
    ] == 1344
    assert sequence_row["source_sft_row_sha256"] == sequence_row[
        "quality_exclusion"
    ]["evidence"]["source_unique_row_sha256"]


@pytest.mark.parametrize(
    "mutation,match",
    (
        ("missing_field", "schema is invalid"),
        ("not_over_threshold", "does not exceed max_length"),
        ("duplicate_sample", "duplicate sample_id"),
        ("unknown_sample", "sample_id is outside TRAIN"),
    ),
)
def test_v2_quality_contract_failures_leave_no_output(
    tmp_path: Path,
    mutation: str,
    match: str,
) -> None:
    paths, ids = _fixture(tmp_path)
    entry = {
        "sample_id": ids[6],
        "reason_code": overlay.SEQUENCE_LENGTH_HARDWARE_EXCLUSION,
        "reason": "A label-blind token audit exceeded the fixed ceiling.",
        "evidence": _sequence_evidence(paths, ids[6]),
    }
    entries = [entry]
    if mutation == "missing_field":
        del entry["evidence"]["chat_template_sha256"]
    elif mutation == "not_over_threshold":
        entry["evidence"] = _sequence_evidence(
            paths,
            ids[6],
            measured_full_tokens=1280,
            max_length=1280,
        )
    elif mutation == "duplicate_sample":
        entries.append(json.loads(json.dumps(entry)))
    elif mutation == "unknown_sample":
        entry["sample_id"] = "not-a-train-member"
    quality_path = _write_quality_payload(
        paths,
        _quality_payload(
            entries,
            version=overlay.QUALITY_EXCLUSIONS_CONTRACT_V2,
        ),
    )

    with pytest.raises(ValueError, match=match):
        overlay.build_overlay(
            train_sft=paths["train_sft"],
            source_only=paths["source_only"],
            review_a=paths["review_a"],
            review_b=paths["review_b"],
            review_c=paths["review_c"],
            output_dir=paths["output_dir"],
            pair_multiplier_preset="neutral-1x-v1",
            quality_exclusions_json=quality_path,
        )
    assert not paths["output_dir"].exists()


def test_numeric_table_structure_is_not_excluded_by_neutral_preset(
    tmp_path: Path,
) -> None:
    paths, ids = _fixture(tmp_path)
    numeric_blob = "1234567890" * 240
    train_rows = _read_jsonl(paths["train_sft"])
    source_rows = _read_jsonl(paths["source_only"])
    source_rows[3]["content"]["numeric_table"] = numeric_blob
    train_content = json.loads(train_rows[3]["messages"][1]["content"])
    train_content["numeric_table"] = numeric_blob
    train_text = overlay.stable_json(train_content)
    train_rows[3]["messages"][1]["content"] = train_text
    train_rows[3]["metadata"]["content_sha256"] = hashlib.sha256(
        train_text.encode("utf-8")
    ).hexdigest()
    _write_jsonl(paths["train_sft"], train_rows)
    _write_jsonl(paths["source_only"], source_rows)

    manifest = overlay.build_overlay(
        train_sft=paths["train_sft"],
        source_only=paths["source_only"],
        review_a=paths["review_a"],
        review_b=paths["review_b"],
        review_c=paths["review_c"],
        output_dir=paths["output_dir"],
        pair_multiplier_preset="neutral-1x-v1",
    )
    trainable_rows = _read_jsonl(
        paths["output_dir"] / overlay.TRAINABLE_OUTPUT_NAME
    )

    assert ids[3] in {row["metadata"]["sample_id"] for row in trainable_rows}
    assert manifest["outputs"]["trainable_balanced"][
        "unique_source_row_count"
    ] == 729
    assert manifest["trainability_policy"]["excluded_unique_row_count"] == 0
    assert manifest["trainability_policy"]["source_structure_exclusion"][
        "enabled"
    ] is False


def test_v13_resolution_override_is_bound_into_policy_hash() -> None:
    preset_policy = overlay._validate_multiplier_policy(
        overlay.PAIR_MULTIPLIER_PRESETS[overlay.V13_CURRICULUM_PRESET],
        source="VERSIONED_PRESET",
        preset=overlay.V13_CURRICULUM_PRESET,
        input_file=None,
    )
    external_policy = overlay._validate_multiplier_policy(
        overlay.PAIR_MULTIPLIER_PRESETS[overlay.V13_CURRICULUM_PRESET],
        source="EXPLICIT_JSON_FILE",
        preset=None,
        input_file=None,
    )

    assert preset_policy["multipliers"] == external_policy["multipliers"]
    assert preset_policy["policy_sha256"] != external_policy["policy_sha256"]
    assert preset_policy["resolution_multiplier_policy"][
        "c_arbitration_fixed_multiplier"
    ] == 1
    assert external_policy["resolution_multiplier_policy"][
        "c_arbitration_fixed_multiplier"
    ] is None


@pytest.mark.parametrize("case", ["missing", "both"])
def test_multiplier_policy_selection_must_be_explicit(
    tmp_path: Path, case: str
) -> None:
    paths, _ = _fixture(tmp_path)
    kwargs = {}
    if case == "both":
        kwargs = {
            "pair_multipliers_json": paths["pair_policy"],
            "pair_multiplier_preset": "neutral-1x-v1",
        }

    with pytest.raises(ValueError, match="select exactly one"):
        overlay.build_overlay(
            train_sft=paths["train_sft"],
            source_only=paths["source_only"],
            review_a=paths["review_a"],
            review_b=paths["review_b"],
            review_c=paths["review_c"],
            output_dir=paths["output_dir"],
            **kwargs,
        )


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing_pair", "exactly cover"),
        ("zero", "integer from 1"),
        ("extra_field", "must contain only"),
        ("wrong_contract", "contract_version mismatch"),
        ("invalid_version", "policy_version is invalid"),
    ],
)
def test_custom_multiplier_policy_is_strict(
    tmp_path: Path, case: str, message: str
) -> None:
    paths, _ = _fixture(tmp_path)
    policy = _policy_payload()
    if case == "missing_pair":
        policy["multipliers"].pop(next(iter(policy["multipliers"])))
    elif case == "zero":
        policy["multipliers"][next(iter(policy["multipliers"]))] = 0
    elif case == "extra_field":
        policy["data_driven_tuning"] = True
    elif case == "invalid_version":
        policy["policy_version"] = "invalid version with spaces"
    else:
        policy["contract_version"] = "wrong"
    paths["pair_policy"].write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        _build_json_policy(paths)
    assert not paths["output_dir"].exists()


@pytest.mark.parametrize("case", ["missing", "extra"])
def test_c_must_exactly_equal_ab_disagreement_set(
    tmp_path: Path, case: str
) -> None:
    paths, ids = _fixture(tmp_path)
    c_rows = _read_jsonl(paths["review_c"])
    if case == "missing":
        c_rows.clear()
    else:
        c_rows.append(
            _review_row(ids[3], ("NOT_MATERIAL_ADVERSE", "NEUTRAL"), "C")
        )
    _write_jsonl(paths["review_c"], c_rows)

    with pytest.raises(ValueError, match="review C arbitration sample_id coverage"):
        _build_json_policy(paths)


def test_timezone_equivalence_is_allowed_and_bound(tmp_path: Path) -> None:
    paths, _ = _fixture(tmp_path)
    source_rows = _read_jsonl(paths["source_only"])
    source_rows[0]["content"]["as_of"] = "2026-08-30T08:00:00+08:00"
    source_rows[0]["content"]["passages"][0]["published_at"] = (
        "2026-08-30T09:30:00+08:00"
    )
    _write_jsonl(paths["source_only"], source_rows)

    manifest = _build_json_policy(paths)
    unique_rows = _read_jsonl(
        paths["output_dir"] / overlay.UNIQUE_OUTPUT_NAME
    )

    assert manifest["source_content_equivalence"]["raw_match_count"] == 728
    assert manifest["source_content_equivalence"][
        "timezone_normalized_match_count"
    ] == 1
    assert unique_rows[0]["metadata"]["source_content_equivalence"][
        "match_method"
    ] == "TIMEZONE_NORMALIZED"
    assert unique_rows[0]["metadata"]["source_content_equivalence"][
        "contract_version"
    ] == shared.SOURCE_CONTENT_EQUIVALENCE_CONTRACT_VERSION


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("different_instant", "does not match train_unique user payload"),
        ("plain_text", "does not match train_unique user payload"),
        ("naive_time", "strict timezone-aware ISO-8601 datetime"),
        ("source_leak", "prohibited supervision keys"),
    ],
)
def test_source_mismatch_naive_time_and_leakage_fail_closed(
    tmp_path: Path, case: str, message: str
) -> None:
    paths, _ = _fixture(tmp_path)
    rows = _read_jsonl(paths["source_only"])
    if case == "different_instant":
        rows[0]["content"]["as_of"] = "2026-08-30T08:00:01+08:00"
    elif case == "plain_text":
        rows[0]["content"]["headline"] = "Different ordinary text"
    elif case == "naive_time":
        rows[0]["content"]["as_of"] = "2026-08-30T08:00:00"
    else:
        rows[0]["content"]["nested"] = {"qwen_prediction": "ADVERSE"}
    _write_jsonl(paths["source_only"], rows)

    with pytest.raises(ValueError, match=message):
        _build_json_policy(paths)
    assert not paths["output_dir"].exists()


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("extra_review_field", "invalid flat review fields"),
        ("wrong_review_class", "review A review_class mismatch"),
        ("prohibited_reason", "prohibited prediction or market text"),
        ("wrong_split", "row is not TRAIN"),
        ("wrong_prompt", "system prompt text mismatch"),
    ],
)
def test_review_and_train_sft_contracts_fail_closed(
    tmp_path: Path, case: str, message: str
) -> None:
    paths, _ = _fixture(tmp_path)
    if case.startswith("wrong_") and case in {"wrong_split", "wrong_prompt"}:
        rows = _read_jsonl(paths["train_sft"])
        if case == "wrong_split":
            rows[0]["metadata"]["split"] = "DEV"
        else:
            rows[0]["messages"][0]["content"] += " changed"
        _write_jsonl(paths["train_sft"], rows)
    else:
        rows = _read_jsonl(paths["review_a"])
        if case == "extra_review_field":
            rows[0]["impact_strength"] = "HIGH"
        elif case == "wrong_review_class":
            rows[0]["review_class"] = (
                "INDEPENDENT_AI_ARBITRATION_NOT_HUMAN_GOLD"
            )
        else:
            rows[0]["reason"] = "The Qwen model prediction supplied this pair."
        _write_jsonl(paths["review_a"], rows)

    with pytest.raises(ValueError, match=message):
        _build_json_policy(paths)
    assert not paths["output_dir"].exists()


def test_requires_exactly_729_original_members(tmp_path: Path) -> None:
    paths, _ = _fixture(tmp_path, count=728)

    with pytest.raises(ValueError, match="must contain exactly 729 rows"):
        _build_json_policy(paths)


def test_cli_requires_explicit_json_or_versioned_preset(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.jsonl"
    with pytest.raises(SystemExit) as exc_info:
        overlay.main(
            [
                "--train-sft",
                str(missing),
                "--source-only",
                str(missing),
                "--review-a",
                str(missing),
                "--review-b",
                str(missing),
                "--review-c",
                str(missing),
                "--output-dir",
                str(tmp_path / "out"),
            ]
        )
    assert exc_info.value.code == 2


def test_existing_output_is_rejected_before_inputs_or_policy(tmp_path: Path) -> None:
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    missing = tmp_path / "missing.jsonl"

    with pytest.raises(FileExistsError, match="output directory already exists"):
        overlay.build_overlay(
            train_sft=missing,
            source_only=missing,
            review_a=missing,
            review_b=missing,
            review_c=missing,
            output_dir=output_dir,
        )


def test_atomic_failure_leaves_no_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _ = _fixture(tmp_path)
    original = shared._write_new_file
    calls = 0

    def fail_second(path: Path, raw: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected write failure")
        original(path, raw)

    monkeypatch.setattr(shared, "_write_new_file", fail_second)
    with pytest.raises(OSError, match="injected write failure"):
        _build_json_policy(paths)

    assert not paths["output_dir"].exists()
    assert not list(tmp_path.glob(".overlay.*.tmp"))
