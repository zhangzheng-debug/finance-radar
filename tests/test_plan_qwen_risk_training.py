from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.models.qwen_risk_contract import expected_semantic_payload
from scripts.plan_qwen_risk_training import build_plan


def _write_jsonl(path: Path, rows: list[dict]) -> str:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _semantic_row(index: int, split: str) -> dict:
    return {
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": json.dumps({"headline": f"event {index}"})},
            {
                "role": "assistant",
                "content": json.dumps(
                    expected_semantic_payload("MATERIAL_ADVERSE", "ADVERSE")
                ),
            },
        ],
        "metadata": {
            "sample_id": f"{split}-{index}",
            "split": split,
            "evidence_state_used_as_model_target": False,
            "post_event_market_data_included": False,
            "model_output_included_in_review": False,
        },
    }


def _manifest(tmp_path: Path, train_count: int, validation_count: int) -> Path:
    files = {
        "qwen_risk_sft_train.jsonl": [_semantic_row(i, "TRAIN") for i in range(train_count)],
        "qwen_risk_sft_validation.jsonl": [
            _semantic_row(i, "VALIDATION") for i in range(validation_count)
        ],
        "qwen_risk_blind_manifest.jsonl": [{"sample_id": "blind-1"}],
        "qwen_risk_evidence_gate_manifest.jsonl": [],
    }
    outputs = {name: _write_jsonl(tmp_path / name, rows) for name, rows in files.items()}
    manifest = {
        "base_model": "Qwen/Qwen2.5-1.5B-Instruct",
        "outputs": outputs,
        "train_rows": train_count,
        "validation_rows": validation_count,
        "human_blind_rows": 1,
        "evidence_gate_rows": 0,
        "evidence_state_used_as_model_target": False,
        "human_blind_labels_exported": False,
        "human_blind_content_exported": False,
        "deepseek_output_included": False,
        "post_event_market_data_included": False,
        "production_model_changed": False,
        "no_trading": True,
    }
    path = tmp_path / "qwen_risk_sft_manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_build_plan_uses_qlora_and_fixed_validation_set(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, 2, 1)
    plan = build_plan(manifest, tmp_path / "output", min_train_rows=2, min_validation_rows=1)
    command = plan["command"]
    assert plan["ready"] is True
    assert command[command.index("--quant_bits") + 1] == "4"
    assert command[command.index("--val_dataset") + 1].endswith(
        "qwen_risk_sft_validation.jsonl"
    )
    assert "--execute" not in command


def test_build_plan_blocks_incomplete_final_dataset(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, 2, 1)
    with pytest.raises(ValueError, match="insufficient training rows"):
        build_plan(manifest, tmp_path / "output")


def test_build_plan_detects_tampered_output(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, 2, 1)
    (tmp_path / "qwen_risk_sft_train.jsonl").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="output digest mismatch"):
        build_plan(manifest, tmp_path / "output", min_train_rows=2, min_validation_rows=1)
