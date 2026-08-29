from scripts.evaluate_qwen_semantic_adapter import (
    extract_json_object,
    gate_decision,
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
