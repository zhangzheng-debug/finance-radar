from __future__ import annotations

import json
from pathlib import Path

from scripts.combine_qwen_v15_frozen_dev import combine
from scripts.freeze_qwen_v15_deepseek_dev import CONTRACT_VERSION, OUTPUT_NAME, sha256_file, stable_json


def _component(path: Path, sample: str, issuer: str) -> None:
    path.mkdir()
    row = {
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "{}"},
            {"role": "assistant", "content": stable_json({"materiality": "NOT_MATERIAL_ADVERSE", "polarity": "NEUTRAL"})},
        ],
        "metadata": {
            "sample_id": sample,
            "entity_group": issuer,
            "event_chain_group": "chain-" + sample,
            "source_content_sha256": sample.rjust(64, "0"),
            "split": "DEV",
        },
    }
    dataset = path / OUTPUT_NAME
    dataset.write_text(stable_json(row) + "\n", encoding="utf-8")
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "label_classification": "AI_REVIEW_NOT_HUMAN_GOLD",
        "row_count": 1,
        "output": {"sha256": sha256_file(dataset)},
    }
    (path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_combines_frozen_components_with_group_checks(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _component(first, "a", "issuer-a")
    _component(second, "b", "issuer-b")
    out = tmp_path / "combined"
    manifest = combine(component_dirs=[first, second], output_dir=out, minimum_rows=2)
    assert manifest["row_count"] == 2
    assert manifest["zero_cross_component_overlap"]["entity_group"] is True


def test_rejects_cross_component_issuer_overlap(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _component(first, "a", "same")
    _component(second, "b", "same")
    try:
        combine(component_dirs=[first, second], output_dir=tmp_path / "out", minimum_rows=2)
    except ValueError as exc:
        assert "duplicate entity_group" in str(exc)
    else:
        raise AssertionError("expected issuer overlap failure")
