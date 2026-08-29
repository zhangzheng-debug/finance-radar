import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.qwen_risk_contract import expected_semantic_payload
from scripts.evaluate_qwen_semantic_adapter import (
    AXES_MODEL_OUTPUT_CONTRACT,
    LEGACY_MODEL_OUTPUT_CONTRACT,
    adapter_fingerprint,
    base_model_fingerprint,
    dataset_contract_binding,
    explicit_generation_config,
    extract_model_output,
    extract_json_object,
    gate_decision,
    load_evaluation_dataset,
    normalize_expected_payload,
    normalize_model_output,
    normalize_payload,
    polarity_alias_report,
    run_inference,
    summarize_prediction_strata,
    summarize_predictions,
)
from scripts.prepare_qwen_semantic_consensus_sft import (
    EXPERIMENT_SYSTEM_PROMPT,
    _balanced_training_rows,
)


def _row(expected, predicted, *, valid=True):
    return {
        "expected": expected,
        "predicted": predicted,
        "contract_valid": valid,
        "exact_match": valid and expected == predicted,
    }


def _dataset_row(
    sample_id: str = "sample-1", *, split: str = "DEV", target_contract: str = "core-v1"
):
    return {
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "{}"},
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "materiality": "MATERIAL_ADVERSE",
                        "polarity": "ADVERSE",
                        "adverse_strength": "HIGH",
                        "semantic_priority": "PRIORITY_REVIEW",
                    }
                ),
            },
        ],
        "metadata": {
            "sample_id": sample_id,
            "split": split,
            "target_contract": target_contract,
        },
    }


def _axes_dataset_row(
    sample_id: str = "axes-1",
    *,
    prompt: str = "two-axis system prompt",
    prompt_version: str = "qwen-core-axes-prompt-v11",
    materiality: str = "MATERIAL_ADVERSE",
    polarity: str = "ADVERSE",
) -> dict:
    row = _dataset_row(sample_id)
    row["messages"][0]["content"] = prompt
    row["messages"][-1]["content"] = json.dumps(
        {"materiality": materiality, "polarity": polarity}
    )
    row["metadata"].update(
        {
            "model_output_contract": AXES_MODEL_OUTPUT_CONTRACT,
            "semantic_target": expected_semantic_payload(materiality, polarity),
            "prompt_version": prompt_version,
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        }
    )
    return row


def _write_dataset(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    return path


def test_dataset_preflight_accepts_core_v1_dev_before_inference(tmp_path: Path):
    dataset = _write_dataset(tmp_path / "dev.jsonl", [_dataset_row()])

    rows = load_evaluation_dataset(dataset, dataset_role="DEV_SELECTION_ONLY")

    assert rows == [_dataset_row()]
    assert dataset_contract_binding(rows) == {
        "target_contract": "core-v1",
        "model_output_contract": LEGACY_MODEL_OUTPUT_CONTRACT,
        "model_output_contract_explicit": False,
        "legacy_compatibility_mode": True,
        "prompt_version": None,
        "prompt_sha256": None,
        "prompt_binding_verified": False,
    }


def test_axes_dataset_keeps_full_core_truth_separate_from_two_axis_target(
    tmp_path: Path,
):
    row = _axes_dataset_row()
    dataset = _write_dataset(tmp_path / "axes-dev.jsonl", [row])

    rows = load_evaluation_dataset(dataset, dataset_role="DEV_SELECTION_ONLY")
    binding = dataset_contract_binding(rows)
    model_target = json.loads(rows[0]["messages"][-1]["content"])
    expected, issues = normalize_expected_payload(
        model_target,
        model_output_contract=binding["model_output_contract"],
        semantic_target=rows[0]["metadata"]["semantic_target"],
    )

    assert set(model_target) == {"materiality", "polarity"}
    assert expected == expected_semantic_payload("MATERIAL_ADVERSE", "ADVERSE")
    assert issues == []
    assert binding == {
        "target_contract": "core-v1",
        "model_output_contract": AXES_MODEL_OUTPUT_CONTRACT,
        "model_output_contract_explicit": True,
        "legacy_compatibility_mode": False,
        "prompt_version": "qwen-core-axes-prompt-v11",
        "prompt_sha256": hashlib.sha256(
            b"two-axis system prompt"
        ).hexdigest(),
        "prompt_binding_verified": True,
    }


def test_axes_dataset_rejects_missing_or_inconsistent_full_semantic_truth(
    tmp_path: Path,
):
    missing = _axes_dataset_row("missing")
    missing["metadata"].pop("semantic_target")
    with pytest.raises(ValueError, match="semantic_target:missing"):
        load_evaluation_dataset(
            _write_dataset(tmp_path / "missing-truth.jsonl", [missing]),
            dataset_role="DEV_SELECTION_ONLY",
        )

    inconsistent = _axes_dataset_row("inconsistent")
    inconsistent["metadata"]["semantic_target"] = expected_semantic_payload(
        "NOT_MATERIAL_ADVERSE", "ADVERSE"
    )
    with pytest.raises(ValueError, match="inconsistent_with_model_target"):
        load_evaluation_dataset(
            _write_dataset(tmp_path / "inconsistent-truth.jsonl", [inconsistent]),
            dataset_role="DEV_SELECTION_ONLY",
        )


def test_dataset_preflight_rejects_wrong_final_role_and_target_contract(tmp_path: Path):
    wrong_role = _dataset_row()
    wrong_role["messages"][-1]["role"] = "user"
    with pytest.raises(ValueError, match="final message is not assistant"):
        load_evaluation_dataset(
            _write_dataset(tmp_path / "wrong-role.jsonl", [wrong_role]),
            dataset_role="DIAGNOSTIC_ONLY",
        )

    wrong_contract = _dataset_row(target_contract="full-v2")
    with pytest.raises(ValueError, match="target_contract must be core-v1"):
        load_evaluation_dataset(
            _write_dataset(tmp_path / "wrong-contract.jsonl", [wrong_contract]),
            dataset_role="DIAGNOSTIC_ONLY",
        )


def test_new_contract_requires_exact_prompt_identity_while_legacy_does_not(
    tmp_path: Path,
):
    missing_identity = _axes_dataset_row("missing-identity")
    missing_identity["metadata"].pop("prompt_version")
    missing_identity["metadata"].pop("prompt_sha256")
    with pytest.raises(
        ValueError, match="explicit model_output_contract requires prompt identity"
    ):
        load_evaluation_dataset(
            _write_dataset(tmp_path / "missing-prompt.jsonl", [missing_identity]),
            dataset_role="DEV_SELECTION_ONLY",
        )

    stale_hash = _axes_dataset_row("stale-hash")
    stale_hash["messages"][0]["content"] += " changed"
    with pytest.raises(ValueError, match="system prompt SHA256 mismatch"):
        load_evaluation_dataset(
            _write_dataset(tmp_path / "stale-prompt.jsonl", [stale_hash]),
            dataset_role="DEV_SELECTION_ONLY",
        )

    non_text_version = _axes_dataset_row("non-text-version")
    non_text_version["metadata"]["prompt_version"] = 11
    with pytest.raises(ValueError, match="prompt_version must be text"):
        load_evaluation_dataset(
            _write_dataset(tmp_path / "non-text-prompt.jsonl", [non_text_version]),
            dataset_role="DEV_SELECTION_ONLY",
        )

    explicit_legacy = _dataset_row("explicit-legacy")
    explicit_legacy["metadata"][
        "model_output_contract"
    ] = LEGACY_MODEL_OUTPUT_CONTRACT
    with pytest.raises(
        ValueError, match="explicit model_output_contract requires prompt identity"
    ):
        load_evaluation_dataset(
            _write_dataset(tmp_path / "explicit-legacy.jsonl", [explicit_legacy]),
            dataset_role="DEV_SELECTION_ONLY",
        )

    legacy = _write_dataset(tmp_path / "legacy.jsonl", [_dataset_row("legacy")])
    assert load_evaluation_dataset(legacy, dataset_role="DEV_SELECTION_ONLY")


def test_dataset_rejects_mixed_model_output_or_prompt_bindings(tmp_path: Path):
    axes = _axes_dataset_row("axes")
    legacy = _dataset_row("legacy")
    with pytest.raises(ValueError, match="model/prompt contract mismatch"):
        load_evaluation_dataset(
            _write_dataset(tmp_path / "mixed-contract.jsonl", [axes, legacy]),
            dataset_role="DEV_SELECTION_ONLY",
        )

    first = _axes_dataset_row("first")
    second = _axes_dataset_row("second", prompt_version="different-version")
    with pytest.raises(ValueError, match="model/prompt contract mismatch"):
        load_evaluation_dataset(
            _write_dataset(tmp_path / "mixed-prompt.jsonl", [first, second]),
            dataset_role="DEV_SELECTION_ONLY",
        )


def test_dataset_preflight_rejects_invalid_target_duplicate_id_and_non_dev_split(
    tmp_path: Path,
):
    invalid_target = _dataset_row()
    invalid_target["messages"][-1]["content"] = json.dumps(
        {"materiality": "MATERIAL_ADVERSE"}
    )
    with pytest.raises(ValueError, match="target contract invalid"):
        load_evaluation_dataset(
            _write_dataset(tmp_path / "invalid-target.jsonl", [invalid_target]),
            dataset_role="DIAGNOSTIC_ONLY",
        )

    duplicate = [_dataset_row("same"), _dataset_row("same")]
    with pytest.raises(ValueError, match="duplicate or missing sample_id"):
        load_evaluation_dataset(
            _write_dataset(tmp_path / "duplicate.jsonl", duplicate),
            dataset_role="DIAGNOSTIC_ONLY",
        )

    with pytest.raises(ValueError, match="metadata.split=DEV"):
        load_evaluation_dataset(
            _write_dataset(tmp_path / "test.jsonl", [_dataset_row(split="TEST")]),
            dataset_role="DEV_SELECTION_ONLY",
        )


def test_model_and_adapter_fingerprints_are_reproducible_and_content_bound(
    tmp_path: Path,
):
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text('{"model":"qwen"}', encoding="utf-8")
    first_model = base_model_fingerprint(model)
    assert base_model_fingerprint(model) == first_model
    (model / "config.json").write_text('{"model":"changed"}', encoding="utf-8")
    assert base_model_fingerprint(model)["sha256"] != first_model["sha256"]

    adapter = tmp_path / "checkpoint-28"
    adapter.mkdir()
    (adapter / "adapter_config.json").write_text("{}", encoding="utf-8")
    weights = adapter / "adapter_model.safetensors"
    weights.write_bytes(b"adapter-v1")
    first_adapter = adapter_fingerprint(adapter)
    weights.write_bytes(b"adapter-v2")
    assert adapter_fingerprint(adapter)["sha256"] != first_adapter["sha256"]


def test_extract_json_object_accepts_fence_and_surrounding_text():
    assert extract_json_object('```json\n{"polarity":"ADVERSE"}\n```') == {"polarity": "ADVERSE"}
    assert extract_json_object('answer: {"polarity":"NEUTRAL"} done') == {"polarity": "NEUTRAL"}


def test_axes_parser_requires_a_plain_json_object_but_legacy_remains_compatible():
    raw = '{"materiality":"MATERIAL_ADVERSE","polarity":"ADVERSE"}'
    assert extract_model_output(
        raw, model_output_contract=AXES_MODEL_OUTPUT_CONTRACT
    ) == {"materiality": "MATERIAL_ADVERSE", "polarity": "ADVERSE"}
    assert (
        extract_model_output(
            f"```json\n{raw}\n```",
            model_output_contract=AXES_MODEL_OUTPUT_CONTRACT,
        )
        is None
    )
    assert extract_model_output(
        f"answer: {raw}", model_output_contract=AXES_MODEL_OUTPUT_CONTRACT
    ) is None
    assert extract_model_output(
        f"```json\n{raw}\n```",
        model_output_contract=LEGACY_MODEL_OUTPUT_CONTRACT,
    ) == {"materiality": "MATERIAL_ADVERSE", "polarity": "ADVERSE"}


def test_axes_model_output_derives_full_payload_and_rejects_wrong_shape():
    valid = normalize_model_output(
        {"materiality": " material_adverse ", "polarity": "adverse"},
        model_output_contract=AXES_MODEL_OUTPUT_CONTRACT,
    )
    assert valid == {
        "normalized_model_output": {
            "materiality": "MATERIAL_ADVERSE",
            "polarity": "ADVERSE",
        },
        "full_payload": expected_semantic_payload(
            "MATERIAL_ADVERSE", "ADVERSE"
        ),
        "issues": [],
        "polarity_alias_applied": False,
    }

    missing = normalize_model_output(
        {"materiality": "MATERIAL_ADVERSE"},
        model_output_contract=AXES_MODEL_OUTPUT_CONTRACT,
    )
    assert "model_output_missing_fields:polarity" in missing["issues"]
    assert missing["full_payload"] is None

    extra = normalize_model_output(
        {
            "materiality": "MATERIAL_ADVERSE",
            "polarity": "ADVERSE",
            "adverse_strength": "HIGH",
        },
        model_output_contract=AXES_MODEL_OUTPUT_CONTRACT,
    )
    assert "model_output_unsupported_fields:adverse_strength" in extra["issues"]
    assert extra["full_payload"] is None


def test_negative_polarity_alias_is_opt_in_exact_and_audited():
    raw = {"materiality": "MATERIAL_ADVERSE", "polarity": " negative "}
    rejected = normalize_model_output(
        raw,
        model_output_contract=AXES_MODEL_OUTPUT_CONTRACT,
        allow_negative_polarity_alias=False,
    )
    assert "negative_polarity_alias_disabled" in rejected["issues"]
    assert "invalid_polarity" in rejected["issues"]
    assert rejected["full_payload"] is None
    assert rejected["polarity_alias_applied"] is False

    accepted = normalize_model_output(
        raw,
        model_output_contract=AXES_MODEL_OUTPUT_CONTRACT,
        allow_negative_polarity_alias=True,
    )
    assert accepted["normalized_model_output"]["polarity"] == "ADVERSE"
    assert accepted["full_payload"] == expected_semantic_payload(
        "MATERIAL_ADVERSE", "ADVERSE"
    )
    assert accepted["issues"] == []
    assert accepted["polarity_alias_applied"] is True

    expected, expected_issues = normalize_expected_payload(
        raw,
        model_output_contract=AXES_MODEL_OUTPUT_CONTRACT,
        semantic_target=expected_semantic_payload(
            "MATERIAL_ADVERSE", "ADVERSE"
        ),
    )
    assert expected is None
    assert "model_target:negative_polarity_alias_disabled" in expected_issues

    fuzzy = normalize_model_output(
        {"materiality": "MATERIAL_ADVERSE", "polarity": "VERY_NEGATIVE"},
        model_output_contract=AXES_MODEL_OUTPUT_CONTRACT,
        allow_negative_polarity_alias=True,
    )
    assert fuzzy["full_payload"] is None
    assert fuzzy["polarity_alias_applied"] is False
    assert polarity_alias_report(enabled=True, applied_rows=1) == {
        "enabled": True,
        "mapping": {"NEGATIVE": "ADVERSE"},
        "applied_rows": 1,
    }
    with pytest.raises(ValueError, match="disabled polarity alias"):
        polarity_alias_report(enabled=False, applied_rows=1)


def test_explicit_generation_config_uses_fresh_safe_greedy_values():
    fallback_pad = explicit_generation_config(
        SimpleNamespace(eos_token_id=151645, pad_token_id=None),
        max_new_tokens=96,
    )
    assert fallback_pad == {
        "max_new_tokens": 96,
        "min_new_tokens": 0,
        "do_sample": False,
        "repetition_penalty": 1.0,
        "encoder_repetition_penalty": 1.0,
        "no_repeat_ngram_size": 0,
        "num_beams": 1,
        "num_beam_groups": 1,
        "num_return_sequences": 1,
        "use_cache": True,
        "eos_token_id": 151645,
        "pad_token_id": 151645,
    }
    explicit_pad = explicit_generation_config(
        SimpleNamespace(eos_token_id=151645, pad_token_id=151643),
        max_new_tokens=32,
    )
    assert explicit_pad["pad_token_id"] == 151643

    with pytest.raises(ValueError, match="max_new_tokens must be positive"):
        explicit_generation_config(
            SimpleNamespace(eos_token_id=1, pad_token_id=1), max_new_tokens=0
        )
    with pytest.raises(ValueError, match="max_new_tokens must be an integer"):
        explicit_generation_config(
            SimpleNamespace(eos_token_id=1, pad_token_id=1),
            max_new_tokens=True,
        )
    with pytest.raises(ValueError, match="eos_token_id"):
        explicit_generation_config(
            SimpleNamespace(eos_token_id=[1, 2], pad_token_id=1),
            max_new_tokens=1,
        )
    with pytest.raises(ValueError, match="pad_token_id"):
        explicit_generation_config(
            SimpleNamespace(eos_token_id=1, pad_token_id=-1),
            max_new_tokens=1,
        )


def test_run_inference_refuses_existing_output_before_any_model_work(tmp_path: Path):
    output_dir = tmp_path / "existing"
    output_dir.mkdir()
    with pytest.raises(FileExistsError, match="output directory already exists"):
        run_inference(
            base_model=tmp_path / "missing-model",
            adapter=tmp_path / "missing-adapter",
            dataset=tmp_path / "missing-dataset.jsonl",
            dataset_role="DEV_SELECTION_ONLY",
            output_dir=output_dir,
            max_new_tokens=96,
        )


def test_summarize_and_gate_perfect_payloads():
    priority = normalize_payload(
        {
            "materiality": "MATERIAL_ADVERSE",
            "polarity": "ADVERSE",
            "adverse_strength": "HIGH",
            "semantic_priority": "PRIORITY_REVIEW",
        }
    )
    routine = normalize_payload(
        {
            "materiality": "NOT_MATERIAL_ADVERSE",
            "polarity": "NEUTRAL",
            "adverse_strength": "NONE",
            "semantic_priority": "ROUTINE",
        }
    )
    metrics = summarize_predictions(
        [_row(priority, priority) for _ in range(20)]
        + [_row(routine, routine) for _ in range(100)]
    )
    assert metrics["exact_payload_accuracy"] == 1.0
    assert metrics["priority_review"]["recall"] == 1.0
    assert gate_decision(metrics)["passed"] is True


def test_gate_rejects_tiny_or_priority_sparse_reference_sets():
    priority = normalize_payload(
        {
            "materiality": "MATERIAL_ADVERSE",
            "polarity": "ADVERSE",
            "adverse_strength": "HIGH",
            "semantic_priority": "PRIORITY_REVIEW",
        }
    )
    routine = normalize_payload(
        {
            "materiality": "NOT_MATERIAL_ADVERSE",
            "polarity": "NEUTRAL",
            "adverse_strength": "NONE",
            "semantic_priority": "ROUTINE",
        }
    )
    tiny = gate_decision(summarize_predictions([_row(priority, priority)] * 20))
    sparse = gate_decision(
        summarize_predictions(
            [_row(priority, priority)] * 19 + [_row(routine, routine)] * 101
        )
    )
    assert tiny["checks"]["rows_ge_120"] is False
    assert sparse["checks"]["priority_support_ge_20"] is False
    assert tiny["passed"] is False
    assert sparse["passed"] is False


def test_invalid_output_counts_against_all_axes():
    expected = normalize_payload(
        {
            "materiality": "MATERIAL_ADVERSE",
            "polarity": "ADVERSE",
            "adverse_strength": "HIGH",
            "semantic_priority": "PRIORITY_REVIEW",
        }
    )
    metrics = summarize_predictions([_row(expected, None, valid=False)])
    assert metrics["parse_success_rate"] == 0.0
    assert metrics["materiality"]["accuracy"] == 0.0
    assert gate_decision(metrics)["passed"] is False


def test_benchmark_strata_are_reported_separately_without_a_subgroup_gate():
    priority = normalize_payload(
        {
            "materiality": "MATERIAL_ADVERSE",
            "polarity": "ADVERSE",
            "adverse_strength": "HIGH",
            "semantic_priority": "PRIORITY_REVIEW",
        }
    )
    routine = normalize_payload(
        {
            "materiality": "NOT_MATERIAL_ADVERSE",
            "polarity": "NEUTRAL",
            "adverse_strength": "NONE",
            "semantic_priority": "ROUTINE",
        }
    )
    general = {**_row(routine, routine), "benchmark_stratum": "GENERAL"}
    high_risk = {**_row(priority, priority), "benchmark_stratum": "HIGH_RISK"}

    strata = summarize_prediction_strata([general, high_risk])

    assert set(strata) == {"GENERAL", "HIGH_RISK"}
    assert strata["GENERAL"]["rows"] == 1
    assert strata["HIGH_RISK"]["priority_review"]["recall"] == 1.0


def test_gate_rejects_excess_false_priority_rate():
    priority = normalize_payload(
        {
            "materiality": "MATERIAL_ADVERSE",
            "polarity": "ADVERSE",
            "adverse_strength": "HIGH",
            "semantic_priority": "PRIORITY_REVIEW",
        }
    )
    routine = normalize_payload(
        {
            "materiality": "NOT_MATERIAL_ADVERSE",
            "polarity": "NEUTRAL",
            "adverse_strength": "NONE",
            "semantic_priority": "ROUTINE",
        }
    )
    metrics = summarize_predictions(
        [_row(priority, priority), _row(routine, priority), _row(routine, routine)]
    )
    assert metrics["priority_review"]["false_priority_rate"] == 0.5
    assert gate_decision(metrics)["passed"] is False


def test_semantic_balancer_repeats_only_training_minority_pairs():
    def item(materiality, polarity, sample_id):
        return {
            "messages": [
                {},
                {},
                {
                    "content": __import__("json").dumps(
                        {
                            "materiality": materiality,
                            "polarity": polarity,
                            "adverse_strength": "HIGH" if materiality == "MATERIAL_ADVERSE" else "NONE",
                            "semantic_priority": "PRIORITY_REVIEW" if materiality == "MATERIAL_ADVERSE" else "ROUTINE",
                        }
                    )
                },
            ],
            "metadata": {"sample_id": sample_id},
        }

    neutral = item("NOT_MATERIAL_ADVERSE", "NEUTRAL", "neutral")
    priority = item("MATERIAL_ADVERSE", "ADVERSE", "priority")
    balanced = _balanced_training_rows([neutral, priority])
    assert len(balanced) == 4
    assert [row["metadata"]["sample_id"] for row in balanced].count("priority") == 3
    assert all("training_repeat" not in row["metadata"] for row in (balanced[0], balanced[1]))
    assert balanced[-1]["metadata"]["origin_sample_id"] == "priority"


def test_experiment_prompt_names_realized_risk_and_rejects_keyword_shortcuts():
    assert "Form 25" in EXPERIMENT_SYSTEM_PROMPT
    assert "假设性清算" in EXPERIMENT_SYSTEM_PROMPT
    assert "不得仅凭关键词" in EXPERIMENT_SYSTEM_PROMPT
