from __future__ import annotations

import pytest

from app.models.qwen_risk_contract import expected_semantic_payload
from app.models.qwen_risk_hybrid import classify_qwen_hybrid_anchor
from scripts.prepare_qwen_semantic_hardcase_sft import (
    _semantic_text as offline_semantic_text,
    classify_hardcase as offline_classify_hardcase,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "The company filed a voluntary petition under Chapter 11 in bankruptcy court.",
            ("MATERIAL_ADVERSE", "ADVERSE"),
        ),
        (
            "The company successfully regained compliance with Nasdaq listing rules.",
            ("NOT_MATERIAL_ADVERSE", "POSITIVE"),
        ),
        (
            "Revenue declined and the company missed estimates.",
            ("NOT_MATERIAL_ADVERSE", "ADVERSE"),
        ),
        (
            "The board appoints Jane Doe as CFO as part of an internal succession.",
            ("NOT_MATERIAL_ADVERSE", "NEUTRAL"),
        ),
    ],
)
def test_runtime_anchors_match_frozen_offline_hybrid(text: str, expected: tuple[str, str]) -> None:
    content = {"headline": text, "summary": "", "passages": []}
    runtime = classify_qwen_hybrid_anchor(content)
    offline = offline_classify_hardcase(offline_semantic_text(content))

    assert runtime is not None
    assert offline is not None
    assert runtime[0] == expected_semantic_payload(*expected)
    assert offline[0] == expected
