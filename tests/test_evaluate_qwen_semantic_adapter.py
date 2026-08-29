import json
from pathlib import Path

import pytest

from scripts.evaluate_qwen_semantic_adapter import (
    adapter_fingerprint,
    base_model_fingerprint,
    extract_json_object,
    gate_decision,
    load_evaluation_dataset,
    normalize_payload,
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


def _write_dataset(path: Path, rows: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    return path


def test_dataset_preflight_accepts_core_v1_dev_before_inference(tmp_path: Path):
    dataset = _write_dataset(tmp_path / "dev.jsonl", [_dataset_row()])

    assert load_evaluation_dataset(dataset, dataset_role="DEV_SELECTION_ONLY") == [
        _dataset_row()
    ]


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
