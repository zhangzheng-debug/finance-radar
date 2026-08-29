from __future__ import annotations

import json
from pathlib import Path

from scripts.build_qwen_semantic_core_v4_weak_dataset import stable_json
from scripts.freeze_qwen_semantic_triple_ai_benchmark import freeze_benchmark


def _review() -> dict:
    return {
        "materiality": "NOT_MATERIAL_ADVERSE", "polarity": "NEUTRAL",
        "impact_strength": "ROUTINE_OR_NONE", "event_realization": "REALIZED_OR_EFFECTIVE",
        "subject_relation": "PRIMARY_SUBJECT", "risk_status": "NO_ADVERSE_CONDITION",
        "novelty": "NEW_EVENT_OR_STATUS_CHANGE",
        "reason_codes": ["ACTUAL_EVENT_COMPLETED_OR_EFFECTIVE", "PRIMARY_SUBJECT_DIRECTLY_AFFECTED", "NO_MATERIAL_DOWNSIDE_MECHANISM", "NEW_MATERIAL_FACT_OR_STATUS_CHANGE", "ROUTINE_OR_NO_SOURCE_SUPPORTED_IMPACT"],
        "brief_reason": "The source reports an ordinary completed update without a material downside mechanism.",
    }


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(stable_json(row) + "\n" for row in rows), encoding="utf-8")


def test_freezes_ordered_core_and_v2_artifacts(tmp_path: Path) -> None:
    provider = tmp_path / "provider.jsonl"
    index = tmp_path / "index.jsonl"
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    c = tmp_path / "c.jsonl"
    _write(provider, [{"sample_id": "s1", "content": {"headline": "ordinary update", "summary": "completed", "passages": []}}])
    _write(index, [{"sample_id": "s1", "source_event_id": "e1", "entity_group": "issuer:1", "event_chain_group": "chain:1"}])
    for path in (a, b, c):
        _write(path, [{"sample_id": "s1", "review": _review()}])
    output = tmp_path / "output"
    manifest = freeze_benchmark(
        provider_input=provider, source_index=index, review_a=a, review_b=b, arbiter=c, output_dir=output,
    )
    assert manifest["row_count"] == 1
    assert manifest["classification"] == "AI_NOT_HUMAN_GOLD"
    row = json.loads((output / "qwen_strict60_core_v1.jsonl").read_text().splitlines()[0])
    assert row["expected"]["semantic_priority"] == "ROUTINE"
    assert row["metadata"]["qwen_prediction_included"] is False
    assert (output / "qwen_strict60_full_v2_truth.jsonl").exists()


def test_rejects_order_mismatch(tmp_path: Path) -> None:
    provider = tmp_path / "provider.jsonl"
    index = tmp_path / "index.jsonl"
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    c = tmp_path / "c.jsonl"
    _write(provider, [{"sample_id": "s1", "content": {"headline": "x", "summary": "x", "passages": []}}])
    _write(index, [{"sample_id": "s1"}])
    _write(a, [{"sample_id": "s1", "review": _review()}])
    _write(b, [{"sample_id": "s2", "review": _review()}])
    _write(c, [{"sample_id": "s1", "review": _review()}])
    try:
        freeze_benchmark(provider_input=provider, source_index=index, review_a=a, review_b=b, arbiter=c, output_dir=tmp_path / "out")
    except ValueError as error:
        assert "order mismatch" in str(error)
    else:
        raise AssertionError("expected order mismatch")
