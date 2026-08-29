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
    dataset_role: str = "DEV_SELECTION_ONLY",
    base_model_sha256: str = "a" * 64,
    max_new_tokens: int = 96,
    predictions_sha256_override: str | None = None,
    token_acc: float | None = None,
) -> Path:
    report_dir = path.with_suffix("")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "report.json"
    predictions = report_dir / "predictions.jsonl"
    predictions.write_text(
        json.dumps({"sample_id": f"sample-{step}", "step": step}) + "\n",
        encoding="utf-8",
    )
    predictions_sha256 = hashlib.sha256(predictions.read_bytes()).hexdigest()
    value = {
        "schema_version": 2,
        "evaluation_only": True,
        "production_model_changed": False,
        "human_gold_claimed": False,
        "dataset_role": dataset_role,
        "reserved_test_only": reserved_test_only,
        "target_contract": "core-v1",
        "dataset_sha256": dataset_sha256,
        "adapter": f"D:/run/checkpoint-{step}",
        "adapter_fingerprint": {
            "scheme": "sha256-peft-adapter-files-v1",
            "sha256": f"{step:064x}",
            "files": [{"path": "adapter_model.safetensors"}],
        },
        "base_model_fingerprint": {
            "scheme": "sha256-directory-manifest-full-small-head-tail-large-v1",
            "sha256": base_model_sha256,
            "files": [{"path": "config.json"}],
        },
        "max_new_tokens": max_new_tokens,
        "generation_config": {
            "max_new_tokens": max_new_tokens,
            "do_sample": False,
        },
        "evaluator_gate_advisory_only": True,
        "checkpoint_selection_authority": (
            "summarize_qwen_v4_checkpoint_evaluations.py strict selector gate"
        ),
        "predictions_sha256": (
            predictions_sha256
            if predictions_sha256_override is None
            else predictions_sha256_override
        ),
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
    if token_acc is not None:
        value["metrics"]["token_acc"] = token_acc
    report_path.write_text(json.dumps(value), encoding="utf-8")
    return report_path


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
    assert result["strict_selector_gate_authoritative"] is True
    assert result["evaluator_gate_used_for_selection"] is False
    assert result["selection_standard"] == "STRICT_SELECTOR_GATE_ONLY"


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


def test_selector_accepts_only_dev_selection_role(tmp_path: Path) -> None:
    dataset = tmp_path / "dev.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    diagnostic = _report(
        tmp_path / "diagnostic.json",
        digest,
        28,
        dataset_role="DIAGNOSTIC_ONLY",
    )

    with pytest.raises(ValueError, match="accepts only DEV_SELECTION_ONLY"):
        summarize(
            report_paths=[diagnostic],
            expected_dataset=dataset,
            output=tmp_path / "selection.json",
        )


def test_selector_verifies_adjacent_predictions_digest(tmp_path: Path) -> None:
    dataset = tmp_path / "dev.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    report = _report(tmp_path / "step28.json", digest, 28)
    (report.parent / "predictions.jsonl").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ValueError, match="predictions.jsonl digest mismatch"):
        summarize(
            report_paths=[report],
            expected_dataset=dataset,
            output=tmp_path / "selection.json",
        )


@pytest.mark.parametrize(
    ("second_kwargs", "message"),
    [
        ({"base_model_sha256": "b" * 64}, "base model fingerprint mismatch"),
        ({"max_new_tokens": 128}, "generation configuration mismatch"),
    ],
)
def test_selector_requires_comparable_model_and_generation_configuration(
    tmp_path: Path, second_kwargs: dict, message: str
) -> None:
    dataset = tmp_path / "dev.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    reports = [
        _report(tmp_path / "step28.json", digest, 28),
        _report(tmp_path / "step56.json", digest, 56, **second_kwargs),
    ]

    with pytest.raises(ValueError, match=message):
        summarize(
            report_paths=reports,
            expected_dataset=dataset,
            output=tmp_path / "selection.json",
        )


def test_token_accuracy_cannot_replace_semantic_gate_metrics(tmp_path: Path) -> None:
    dataset = tmp_path / "dev.jsonl"
    dataset.write_text("{}\n", encoding="utf-8")
    digest = hashlib.sha256(dataset.read_bytes()).hexdigest()
    report = _report(
        tmp_path / "step28.json", digest, 28, exact=0.20, token_acc=1.0
    )

    result = summarize(
        report_paths=[report],
        expected_dataset=dataset,
        output=tmp_path / "selection.json",
    )
    assert result["selected_checkpoint"] is None
    assert result["evaluated_checkpoints"][0]["checks"][
        "exact_payload_accuracy_ge_0_75"
    ] is False


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
