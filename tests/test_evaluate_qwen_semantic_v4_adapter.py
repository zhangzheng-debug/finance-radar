from __future__ import annotations

import json
from pathlib import Path

from app.models.qwen_risk_contract import expected_semantic_payload
from scripts.evaluate_qwen_semantic_v4_adapter import (
    _load_dataset,
    gate_decision,
    summarize_predictions,
)


def _payload(materiality: str, polarity: str) -> dict:
    codes = [
        "ACTUAL_EVENT_COMPLETED_OR_EFFECTIVE",
        "PRIMARY_SUBJECT_DIRECTLY_AFFECTED",
        "NEW_MATERIAL_FACT_OR_STATUS_CHANGE",
    ]
    risk_status = "ACTIVE" if polarity in {"ADVERSE", "MIXED"} else "NO_ADVERSE_CONDITION"
    if materiality == "MATERIAL_ADVERSE":
        codes.append("MATERIAL_DOWNSIDE_MECHANISM")
    else:
        codes.append("NO_MATERIAL_DOWNSIDE_MECHANISM")
    if polarity == "ADVERSE":
        codes.extend(["ADVERSE_CONDITION_ACTIVE", "ADVERSE_COMPONENT_PRESENT"])
    elif polarity == "POSITIVE":
        codes.append("POSITIVE_COMPONENT_PRESENT")
    elif polarity == "MIXED":
        codes.extend(["ADVERSE_CONDITION_ACTIVE", "POSITIVE_AND_ADVERSE_COMPONENTS"])
    if materiality == "MATERIAL_ADVERSE":
        impact_strength = "MODERATE"
        codes.append("MODERATE_SOURCE_SUPPORTED_IMPACT")
    elif polarity == "NEUTRAL":
        impact_strength = "ROUTINE_OR_NONE"
        codes.append("ROUTINE_OR_NO_SOURCE_SUPPORTED_IMPACT")
    else:
        impact_strength = "MINOR"
        codes.append("MINOR_SOURCE_SUPPORTED_IMPACT")
    return {
        "materiality": materiality,
        "polarity": polarity,
        "impact_strength": impact_strength,
        "event_realization": "REALIZED_OR_EFFECTIVE",
        "subject_relation": "PRIMARY_SUBJECT",
        "risk_status": risk_status,
        "novelty": "NEW_EVENT_OR_STATUS_CHANGE",
        "reason_codes": sorted(codes),
        "brief_reason": "The exact source supports this semantic test classification.",
    }


def test_perfect_balanced_test_passes_shadow_gate() -> None:
    pairs = [
        ("MATERIAL_ADVERSE", "ADVERSE"),
        ("NOT_MATERIAL_ADVERSE", "POSITIVE"),
        ("NOT_MATERIAL_ADVERSE", "MIXED"),
        ("NOT_MATERIAL_ADVERSE", "NEUTRAL"),
    ]
    rows = []
    for index in range(120):
        expected = _payload(*pairs[index % len(pairs)])
        rows.append(
            {
                "expected": expected,
                "predicted": json.loads(json.dumps(expected)),
                "contract_valid": True,
                "semantic_exact_match": True,
            }
        )
    metrics = summarize_predictions(rows)
    assert metrics["parse_success_rate"] == 1.0
    assert metrics["impact_strength"]["accuracy"] == 1.0
    assert metrics["priority_review"]["support"] == 30
    assert metrics["reason_codes"]["micro_f1"] == 1.0
    assert gate_decision(metrics)["passed"] is True


def test_invalid_prediction_counts_against_each_axis_and_priority() -> None:
    expected = _payload("MATERIAL_ADVERSE", "ADVERSE")
    metrics = summarize_predictions(
        [
            {
                "expected": expected,
                "predicted": None,
                "contract_valid": False,
                "semantic_exact_match": False,
            }
        ]
    )
    assert metrics["parse_success_rate"] == 0.0
    assert metrics["materiality"]["accuracy"] == 0.0
    assert metrics["priority_review"]["recall"] == 0.0
    assert gate_decision(metrics)["decision"] == "NOT_QUALIFIED"


def test_perfect_core_v1_test_uses_production_contract_gate() -> None:
    pairs = [
        ("MATERIAL_ADVERSE", "ADVERSE"),
        ("NOT_MATERIAL_ADVERSE", "POSITIVE"),
        ("NOT_MATERIAL_ADVERSE", "MIXED"),
        ("NOT_MATERIAL_ADVERSE", "NEUTRAL"),
    ]
    rows = []
    for index in range(120):
        expected = expected_semantic_payload(*pairs[index % len(pairs)])
        rows.append(
            {
                "expected": expected,
                "predicted": json.loads(json.dumps(expected)),
                "contract_valid": True,
                "semantic_exact_match": True,
            }
        )

    metrics = summarize_predictions(rows, "core-v1")
    gate = gate_decision(metrics, "core-v1")
    assert metrics["parse_success_rate"] == 1.0
    assert metrics["exact_payload_accuracy"] == 1.0
    assert metrics["priority_review"]["support"] == 30
    assert gate["passed"] is True
    assert gate["decision"] == "QUALIFIED_SHADOW_SEMANTIC_CANDIDATE"


def test_core_v1_evaluator_loads_fixed_test_dataset_directly(tmp_path: Path) -> None:
    expected = expected_semantic_payload("MATERIAL_ADVERSE", "ADVERSE")
    row = {
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "{}"},
            {"role": "assistant", "content": json.dumps(expected)},
        ],
        "metadata": {
            "sample_id": "strict-1",
            "split": "TEST",
            "target_contract": "core-v1",
        },
    }
    dataset = tmp_path / "qwen_risk_sft_test.jsonl"
    dataset.write_text(json.dumps(row) + "\n", encoding="utf-8")

    loaded = _load_dataset(dataset, "core-v1")
    assert loaded == [row]
