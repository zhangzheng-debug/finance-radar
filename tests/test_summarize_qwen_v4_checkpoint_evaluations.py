from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.summarize_qwen_v4_checkpoint_evaluations import summarize


def _report(
    path: Path,
    dataset_sha256: str,
    step: int,
    *,
    fpr: float = 0.05,
    recall: float = 0.85,
    polarity: float = 0.70,
    materiality: float = 0.75,
    exact: float = 0.80,
    reserved_test_only: bool = False,
) -> Path:
    value = {
        "evaluation_only": True,
        "production_model_changed": False,
        "reserved_test_only": reserved_test_only,
        "dataset_sha256": dataset_sha256,
        "adapter": f"D:/run/checkpoint-{step}",
        "metrics": {
            "rows": 152,
            "parse_success_rate": 1.0,
            "exact_payload_accuracy": exact,
            "materiality": {"macro_f1_truth_supported_classes": materiality},
            "polarity": {"macro_f1_truth_supported_classes": polarity},
            "priority_review": {
                "support": 44,
                "recall": recall,
                "false_priority_rate": fpr,
            },
        },
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_selects_lowest_fpr_then_higher_metrics(tmp_path: Path) -> None:
    dataset = tmp_path / "dev.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    reports = [
        _report(tmp_path / "step28.json", digest, 28, fpr=0.06, recall=0.95),
        _report(tmp_path / "step56.json", digest, 56, fpr=0.04, recall=0.81),
        _report(tmp_path / "step84.json", digest, 84, fpr=0.04, recall=0.90),
    ]
    result = summarize(
        report_paths=reports,
        expected_dataset=dataset,
        output=tmp_path / "selection.json",
    )
    assert result["selected_checkpoint"]["checkpoint_step"] == 84
    assert result["decision"] == "DEV_CANDIDATE_FROZEN"
    assert result["reserved_benchmark_opened"] is False


def test_returns_no_candidate_when_strict_gate_fails(tmp_path: Path) -> None:
    dataset = tmp_path / "dev.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    report = _report(tmp_path / "step28.json", digest, 28, fpr=0.081)
    result = summarize(
        report_paths=[report],
        expected_dataset=dataset,
        output=tmp_path / "selection.json",
    )
    assert result["selected_checkpoint"] is None
    assert result["decision"] == "NO_DEV_CHECKPOINT_QUALIFIED"


def test_rejects_reserved_test_report_and_dataset_mismatch(tmp_path: Path) -> None:
    dataset = tmp_path / "dev.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    reserved = _report(
        tmp_path / "reserved.json", digest, 28, reserved_test_only=True
    )
    with pytest.raises(ValueError, match="reserved TEST"):
        summarize(
            report_paths=[reserved],
            expected_dataset=dataset,
            output=tmp_path / "selection-a.json",
        )

    mismatch = _report(tmp_path / "mismatch.json", "0" * 64, 56)
    with pytest.raises(ValueError, match="dataset digest mismatch"):
        summarize(
            report_paths=[mismatch],
            expected_dataset=dataset,
            output=tmp_path / "selection-b.json",
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -0.01, 1.01])
def test_rejects_nonfinite_or_out_of_range_rates(tmp_path: Path, value: float) -> None:
    dataset = tmp_path / "dev.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    report = _report(tmp_path / "bad.json", digest, 28, fpr=value)
    with pytest.raises(ValueError, match="outside \\[0, 1\\]"):
        summarize(
            report_paths=[report],
            expected_dataset=dataset,
            output=tmp_path / "selection.json",
        )
