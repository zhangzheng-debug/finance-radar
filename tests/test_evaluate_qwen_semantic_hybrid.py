import json

from app.models.qwen_risk_contract import expected_semantic_payload
from scripts.evaluate_qwen_semantic_hybrid import apply_hybrid_prediction


def _dataset_row(text: str):
    return {
        "messages": [
            {"role": "system", "content": "prompt"},
            {
                "role": "user",
                "content": json.dumps(
                    {"headline": "10-Q", "summary": "", "passages": [{"passage": text}]}
                ),
            },
            {"role": "assistant", "content": "{}"},
        ]
    }


def _prediction(expected, predicted):
    return {
        "sample_id": "sample",
        "expected": expected,
        "predicted": predicted,
        "contract_valid": True,
        "contract_issues": [],
        "exact_match": expected == predicted,
    }


def test_hardcase_anchor_recovers_realized_bankruptcy() -> None:
    expected = expected_semantic_payload("MATERIAL_ADVERSE", "ADVERSE")
    routine = expected_semantic_payload("NOT_MATERIAL_ADVERSE", "NEUTRAL")
    row = apply_hybrid_prediction(
        _dataset_row("The debtor filed a Chapter 11 petition with the bankruptcy court."),
        _prediction(expected, routine),
    )
    assert row["predicted"] == expected
    assert row["decision_source"] == "DETERMINISTIC_HARDCASE_ANCHOR"
    assert row["exact_match"] is True


def test_paid_form_25_contrast_prevents_keyword_only_alarm() -> None:
    expected = expected_semantic_payload("NOT_MATERIAL_ADVERSE", "NEUTRAL")
    priority = expected_semantic_payload("MATERIAL_ADVERSE", "ADVERSE")
    row = apply_hybrid_prediction(
        _dataset_row("Form 25 followed a completed merger paying $18.50 per share in cash."),
        _prediction(expected, priority),
    )
    assert row["predicted"] == expected
    assert row["hardcase_rule"] == "paid_or_completed_listing_exit"


def test_qwen_prediction_remains_when_no_anchor_matches() -> None:
    expected = expected_semantic_payload("NOT_MATERIAL_ADVERSE", "POSITIVE")
    row = apply_hybrid_prediction(
        _dataset_row("The company introduced a new ordinary product."),
        _prediction(expected, expected),
    )
    assert row["predicted"] == expected
    assert row["decision_source"] == "QWEN_ADAPTER"
