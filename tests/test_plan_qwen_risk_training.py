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
    train_rows = [_semantic_row(i, "TRAIN") for i in range(train_count)]
    balanced_train_rows = []
    for row in train_rows:
        balanced = json.loads(json.dumps(row))
        sample_id = balanced["metadata"]["sample_id"]
        balanced["metadata"].update(
            {
                "origin_sample_id": sample_id,
                "training_instance_id": sample_id,
                "oversample_repeat_index": 0,
                "oversampled": False,
            }
        )
        balanced_train_rows.append(balanced)
    validation_rows = [
        _semantic_row(i, "VALIDATION") for i in range(validation_count)
    ]
    semantic_rows = train_rows + validation_rows
    files = {
        "qwen_risk_sft_train.jsonl": train_rows,
        "qwen_risk_sft_train_balanced.jsonl": balanced_train_rows,
        "qwen_risk_sft_validation.jsonl": validation_rows,
        "qwen_risk_blind_manifest.jsonl": [{"sample_id": "blind-1"}],
        "qwen_risk_evidence_posture_audit.jsonl": [
            {
                "sample_id": row["metadata"]["sample_id"],
                "split": row["metadata"]["split"],
                "evidence_state": "DISCOVERY_ONLY",
                "qwen_training_included": True,
                "evidence_state_exposed_to_model": False,
            }
            for row in semantic_rows
        ],
    }
    outputs = {name: _write_jsonl(tmp_path / name, rows) for name, rows in files.items()}
    manifest = {
        "base_model": "Qwen/Qwen2.5-1.5B-Instruct",
        "outputs": outputs,
        "train_rows": train_count,
        "train_effective_rows": train_count,
        "train_oversampled_rows": 0,
        "validation_rows": validation_count,
        "human_blind_rows": 1,
        "evidence_posture_audit_rows": train_count + validation_count,
        "train_priority_resampling": {
            "policy": "TRAIN_ONLY_PRIORITY_REVIEW_CAPPED_REPEAT_V1",
            "selection_uses_human_semantic_target": True,
            "validation_resampled": False,
            "human_blind_resampled": False,
            "unique_train_rows": train_count,
            "unique_priority_review_rows": train_count,
            "effective_train_rows": train_count,
            "effective_priority_review_rows": train_count,
            "oversampled_rows": 0,
            "target_priority_fraction": 0.25,
            "achieved_priority_fraction": 1.0,
            "max_occurrences_per_sample": 4,
            "target_met": True,
        },
        "evidence_state_used_as_model_target": False,
        "evidence_state_exposed_to_model": False,
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
    assert plan["train_rows"] == 2
    assert plan["train_effective_rows"] == 2
    assert command[command.index("--quant_bits") + 1] == "4"
    assert command[command.index("--val_dataset") + 1].endswith(
        "qwen_risk_sft_validation.jsonl"
    )
    assert command[command.index("--dataset") + 1].endswith(
        "qwen_risk_sft_train_balanced.jsonl"
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


def test_build_plan_rejects_non_priority_or_untracked_repeat(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, 2, 1)
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    balanced_path = tmp_path / "qwen_risk_sft_train_balanced.jsonl"
    rows = [json.loads(line) for line in balanced_path.read_text(encoding="utf-8").splitlines()]
    extra = json.loads(json.dumps(rows[0]))
    extra["metadata"].update(
        {
            "training_instance_id": "TRAIN-0#priority-repeat-1",
            "oversample_repeat_index": 1,
            "oversampled": True,
        }
    )
    rows.append(extra)
    digest = _write_jsonl(balanced_path, rows)
    manifest_value["outputs"][balanced_path.name] = digest
    # Deliberately leave the policy counts unchanged: the verifier must reject
    # a dataset that was modified after the audited resampling plan.
    manifest.write_text(json.dumps(manifest_value), encoding="utf-8")

    with pytest.raises(ValueError, match="manifest mismatch"):
        build_plan(manifest, tmp_path / "output", min_train_rows=2, min_validation_rows=1)
