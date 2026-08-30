from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.models.qwen_risk_contract import (
    QWEN_RISK_PROMPT_VERSION,
    expected_semantic_payload,
    normalize_qwen_risk_content,
)
from scripts.evaluate_qwen_risk_blind import evaluate


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _content(index: int) -> dict:
    return {
        "headline": f"Issuer event {index}",
        "summary": "source text",
        "passages": [],
        "target_label_hidden": True,
        "post_event_market_data_included": False,
        "model_output_included": False,
    }


def _bundle(tmp_path: Path, rows: int = 3):
    dataset = tmp_path / "frozen.jsonl"
    values = []
    blind_manifest = []
    for index in range(rows):
        content = _content(index)
        values.append(
            {
                "sample_id": f"sample-{index}",
                "split": "HUMAN_BLIND",
                "materiality": "MATERIAL_ADVERSE",
                "polarity": "ADVERSE",
                "evidence_state": "DISCOVERY_ONLY",
                "content": content,
            }
        )
        normalized = normalize_qwen_risk_content(content)
        blind_manifest.append(
            {
                "sample_id": f"sample-{index}",
                "content_sha256": hashlib.sha256(
                    json.dumps(
                        normalized,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            }
        )
    dataset.write_text("".join(json.dumps(row) + "\n" for row in values), encoding="utf-8")
    dataset.with_suffix(".jsonl.sha256").write_text(
        f"{_sha(dataset)}  {dataset.name}\n", encoding="ascii"
    )
    blind = tmp_path / "qwen_risk_blind_manifest.jsonl"
    blind.write_text("".join(json.dumps(row) + "\n" for row in blind_manifest), encoding="utf-8")
    manifest = tmp_path / "qwen_risk_sft_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "frozen_dataset_sha256": _sha(dataset),
                "semantic_contract_version": "qwen-risk-semantics-v1",
                "prompt_version": QWEN_RISK_PROMPT_VERSION,
                "outputs": {blind.name: _sha(blind)},
            }
        ),
        encoding="utf-8",
    )
    adapter = tmp_path / "adapter_model.safetensors"
    adapter.write_bytes(b"adapter")
    return dataset, manifest, adapter


class Provider:
    def __init__(self, adapter: Path):
        self.adapter_sha256 = _sha(adapter)
        self.model_version = "qwen-risk-" + self.adapter_sha256[:16]

    def predict_content(self, content):
        return expected_semantic_payload("MATERIAL_ADVERSE", "ADVERSE"), 1.0


def test_blind_evaluation_passes_and_refuses_reuse(tmp_path: Path) -> None:
    dataset, manifest, adapter = _bundle(tmp_path)
    output = tmp_path / "evaluation"
    result = evaluate(
        dataset,
        manifest,
        adapter,
        output,
        Provider(adapter),
        minimum_rows=3,
        minimum_priority_review_support=3,
        thresholds={
            "materiality_macro_f1": 1.0,
            "polarity_macro_f1": 1.0,
            "priority_review_recall": 1.0,
        },
    )
    assert result["status"] == "PASS"
    assert result["production_eligible"] is True
    assert result["gate_checks"]["priority_review_support"] is True
    assert result["blind_supports"] == {"PRIORITY_REVIEW": 3}
    assert (output / "BLIND_CONSUMED.json").is_file()
    with pytest.raises(ValueError, match="may not be reused"):
        evaluate(dataset, manifest, adapter, output, Provider(adapter), minimum_rows=3)


def test_blind_evaluation_rejects_wrong_candidate_hash(tmp_path: Path) -> None:
    dataset, manifest, adapter = _bundle(tmp_path)
    provider = Provider(adapter)
    provider.adapter_sha256 = "b" * 64
    with pytest.raises(ValueError, match="adapter hash"):
        evaluate(
            dataset,
            manifest,
            adapter,
            tmp_path / "evaluation",
            provider,
            minimum_rows=3,
        )


def test_blind_evaluation_fails_when_adverse_support_is_too_small(tmp_path: Path) -> None:
    dataset, manifest, adapter = _bundle(tmp_path)
    output = tmp_path / "evaluation"
    result = evaluate(
        dataset,
        manifest,
        adapter,
        output,
        Provider(adapter),
        minimum_rows=3,
        minimum_priority_review_support=4,
    )

    assert result["status"] == "FAIL"
    assert result["production_eligible"] is False
    assert result["gate_checks"]["priority_review_support"] is False
    assert (output / "BLIND_CONSUMED.json").is_file()
