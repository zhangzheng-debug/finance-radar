from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

import pytest

from app.models.qwen_weak_supervision_contract import (
    QWEN_WEAK_PROMPT_SHA256,
    QWEN_WEAK_PROMPT_VERSION,
    QWEN_WEAK_SYSTEM_PROMPT,
)
from scripts.evaluate_qwen_semantic_adapter import (
    AXES_MODEL_OUTPUT_CONTRACT,
    GENERATION_CONFIG_VERSION,
    normalize_model_output,
    stable_json,
    summarize_predictions,
)
from scripts.summarize_qwen_v4_checkpoint_evaluations import summarize


PRIORITY_PAYLOAD = {
    "materiality": "MATERIAL_ADVERSE",
    "polarity": "ADVERSE",
    "adverse_strength": "HIGH",
    "semantic_priority": "PRIORITY_REVIEW",
}
ROUTINE_PAYLOAD = {
    "materiality": "NOT_MATERIAL_ADVERSE",
    "polarity": "NEUTRAL",
    "adverse_strength": "NONE",
    "semantic_priority": "ROUTINE",
}
AXES_PROMPT = QWEN_WEAK_SYSTEM_PROMPT
AXES_PROMPT_VERSION = QWEN_WEAK_PROMPT_VERSION
AXES_PROMPT_SHA256 = QWEN_WEAK_PROMPT_SHA256


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dataset(path: Path, *, priority_rows: int = 40, routine_rows: int = 100) -> Path:
    rows: list[dict[str, Any]] = []
    for index in range(priority_rows + routine_rows):
        expected = PRIORITY_PAYLOAD if index < priority_rows else ROUTINE_PAYLOAD
        rows.append(
            {
                "messages": [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": "{}"},
                    {"role": "assistant", "content": stable_json(expected)},
                ],
                "metadata": {
                    "sample_id": f"sample-{index:03d}",
                    "event_id": f"event-{index:03d}",
                    "split": "DEV",
                    "target_contract": "core-v1",
                },
            }
        )
    path.write_text("".join(stable_json(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _dataset_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _prediction_rows(
    dataset: Path, *, false_negatives: int = 4, false_positives: int = 5
) -> list[dict[str, Any]]:
    rows = _dataset_rows(dataset)
    priority_indices = [
        index
        for index, row in enumerate(rows)
        if json.loads(row["messages"][-1]["content"])["semantic_priority"]
        == "PRIORITY_REVIEW"
    ]
    routine_indices = [index for index in range(len(rows)) if index not in priority_indices]
    fn_indices = set(priority_indices[:false_negatives])
    fp_indices = set(routine_indices[:false_positives])
    predictions: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        expected = json.loads(row["messages"][-1]["content"])
        predicted = deepcopy(expected)
        if index - 1 in fn_indices:
            predicted = deepcopy(ROUTINE_PAYLOAD)
        elif index - 1 in fp_indices:
            predicted = deepcopy(PRIORITY_PAYLOAD)
        predictions.append(
            {
                "index": index,
                "sample_id": row["metadata"]["sample_id"],
                "event_id": row["metadata"]["event_id"],
                "benchmark_stratum": row["metadata"].get("benchmark_stratum"),
                "expected": expected,
                "predicted": predicted,
                "raw_output": stable_json(predicted),
                "contract_issues": [],
                "contract_valid": True,
                "exact_match": predicted == expected,
            }
        )
    return predictions


def _write_predictions(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(stable_json(row) + "\n" for row in rows), encoding="utf-8")


def _set_core_metric(metrics: dict[str, Any], field: str, value: Any) -> None:
    if field == "rows":
        metrics["rows"] = value
    elif field == "contract_valid_rows":
        metrics["contract_valid_rows"] = value
    elif field == "parse_success_rate":
        metrics["parse_success_rate"] = value
    elif field == "exact_payload_accuracy":
        metrics["exact_payload_accuracy"] = value
    elif field == "materiality_macro_f1":
        metrics["materiality"]["macro_f1_truth_supported_classes"] = value
    elif field == "polarity_macro_f1":
        metrics["polarity"]["macro_f1_truth_supported_classes"] = value
    elif field == "priority_recall":
        metrics["priority_review"]["recall"] = value
    elif field == "priority_support":
        metrics["priority_review"]["support"] = value
    elif field == "non_priority_support":
        metrics["priority_review"]["non_priority_support"] = value
    elif field == "false_priority_rate":
        metrics["priority_review"]["false_priority_rate"] = value
    else:  # pragma: no cover - test helper misuse
        raise AssertionError(field)


def _report(
    path: Path,
    dataset: Path,
    step: int,
    *,
    false_negatives: int = 4,
    false_positives: int = 5,
    reserved_test_only: bool = False,
    dataset_role: str = "DEV_SELECTION_ONLY",
    base_model_sha256: str = "a" * 64,
    max_new_tokens: int = 96,
    generation_extra: dict[str, Any] | None = None,
    predictions_sha256_override: str | None = None,
    token_acc: float | None = None,
    metric_override: tuple[str, Any] | None = None,
) -> Path:
    report_dir = path.with_suffix("")
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "report.json"
    predictions_path = report_dir / "predictions.jsonl"
    predictions = _prediction_rows(
        dataset,
        false_negatives=false_negatives,
        false_positives=false_positives,
    )
    _write_predictions(predictions_path, predictions)
    metrics = summarize_predictions(predictions)
    if token_acc is not None:
        metrics["token_acc"] = token_acc
    if metric_override is not None:
        _set_core_metric(metrics, *metric_override)
    generation_config = {
        "max_new_tokens": max_new_tokens,
        "do_sample": False,
        **(generation_extra or {}),
    }
    value = {
        "schema_version": 2,
        "evaluation_only": True,
        "production_model_changed": False,
        "human_gold_claimed": False,
        "dataset_role": dataset_role,
        "reserved_test_only": reserved_test_only,
        "target_contract": "core-v1",
        "dataset_path": str(dataset),
        "dataset_sha256": _sha256(dataset),
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
        "generation_config": generation_config,
        "evaluator_gate_advisory_only": True,
        "checkpoint_selection_authority": (
            "summarize_qwen_v4_checkpoint_evaluations.py strict selector gate"
        ),
        "predictions_sha256": (
            _sha256(predictions_path)
            if predictions_sha256_override is None
            else predictions_sha256_override
        ),
        "metrics": metrics,
    }
    report_path.write_text(json.dumps(value), encoding="utf-8")
    return report_path


def _axes_dataset(
    path: Path, *, priority_rows: int = 40, routine_rows: int = 100
) -> Path:
    rows: list[dict[str, Any]] = []
    for index in range(priority_rows + routine_rows):
        semantic_target = (
            deepcopy(PRIORITY_PAYLOAD)
            if index < priority_rows
            else deepcopy(ROUTINE_PAYLOAD)
        )
        model_target = {
            "materiality": semantic_target["materiality"],
            "polarity": semantic_target["polarity"],
        }
        rows.append(
            {
                "messages": [
                    {"role": "system", "content": AXES_PROMPT},
                    {"role": "user", "content": "{}"},
                    {"role": "assistant", "content": stable_json(model_target)},
                ],
                "metadata": {
                    "sample_id": f"axes-{index:03d}",
                    "event_id": f"axes-event-{index:03d}",
                    "split": "DEV",
                    "target_contract": "core-v1",
                    "model_output_contract": AXES_MODEL_OUTPUT_CONTRACT,
                    "prompt_version": AXES_PROMPT_VERSION,
                    "prompt_sha256": AXES_PROMPT_SHA256,
                    "semantic_target": semantic_target,
                },
            }
        )
    path.write_text(
        "".join(stable_json(row) + "\n" for row in rows), encoding="utf-8"
    )
    return path


def _axes_report(
    path: Path,
    dataset: Path,
    step: int,
    *,
    false_negatives: int = 4,
    false_positives: int = 5,
    alias_enabled: bool = False,
    alias_row: int | None = None,
) -> Path:
    dataset_rows = _dataset_rows(dataset)
    priority_indices = list(range(40))
    routine_indices = list(range(40, 140))
    fn_indices = set(priority_indices[:false_negatives])
    fp_indices = set(routine_indices[:false_positives])
    predictions: list[dict[str, Any]] = []
    alias_applied_rows = 0
    for index, dataset_row in enumerate(dataset_rows, start=1):
        metadata = dataset_row["metadata"]
        expected = deepcopy(metadata["semantic_target"])
        expected_model_output = json.loads(dataset_row["messages"][-1]["content"])
        if index - 1 in fn_indices:
            parsed = {
                "materiality": ROUTINE_PAYLOAD["materiality"],
                "polarity": ROUTINE_PAYLOAD["polarity"],
            }
        elif index - 1 in fp_indices:
            parsed = {
                "materiality": PRIORITY_PAYLOAD["materiality"],
                "polarity": PRIORITY_PAYLOAD["polarity"],
            }
        else:
            parsed = deepcopy(expected_model_output)
        if alias_row == index - 1:
            parsed["polarity"] = "NEGATIVE"
        normalized = normalize_model_output(
            parsed,
            model_output_contract=AXES_MODEL_OUTPUT_CONTRACT,
            allow_negative_polarity_alias=alias_enabled,
        )
        alias_applied = bool(normalized["polarity_alias_applied"])
        alias_applied_rows += int(alias_applied)
        predicted = normalized["full_payload"]
        valid = not normalized["issues"]
        predictions.append(
            {
                "index": index,
                "sample_id": metadata["sample_id"],
                "event_id": metadata["event_id"],
                "benchmark_stratum": metadata.get("benchmark_stratum"),
                "expected": expected,
                "expected_model_output": expected_model_output,
                "model_output_contract": AXES_MODEL_OUTPUT_CONTRACT,
                "parsed_model_output": parsed,
                "normalized_model_output": normalized["normalized_model_output"],
                "predicted": predicted,
                "raw_output": stable_json(parsed),
                "polarity_alias_applied": alias_applied,
                "contract_issues": normalized["issues"],
                "contract_valid": valid,
                "exact_match": bool(valid and predicted == expected),
            }
        )

    report_dir = path.with_suffix("")
    report_dir.mkdir(parents=True)
    report_path = report_dir / "report.json"
    predictions_path = report_dir / "predictions.jsonl"
    _write_predictions(predictions_path, predictions)
    generation_config = {
        "max_new_tokens": 96,
        "do_sample": False,
        "repetition_penalty": 1.0,
        "num_beams": 1,
        "use_cache": True,
        "eos_token_id": 151645,
        "pad_token_id": 151643,
    }
    report = {
        "schema_version": 2,
        "evaluation_only": True,
        "production_model_changed": False,
        "human_gold_claimed": False,
        "dataset_role": "DEV_SELECTION_ONLY",
        "reserved_test_only": False,
        "target_contract": "core-v1",
        "model_output_contract": AXES_MODEL_OUTPUT_CONTRACT,
        "model_output_contract_explicit": True,
        "legacy_compatibility_mode": False,
        "prompt_version": AXES_PROMPT_VERSION,
        "prompt_sha256": AXES_PROMPT_SHA256,
        "prompt_binding_verified": True,
        "dataset_path": str(dataset),
        "dataset_sha256": _sha256(dataset),
        "adapter": f"D:/run/checkpoint-{step}",
        "adapter_fingerprint": {
            "scheme": "sha256-peft-adapter-files-v1",
            "sha256": f"{step:064x}",
            "files": [{"path": "adapter_model.safetensors"}],
        },
        "base_model_fingerprint": {
            "scheme": "sha256-directory-manifest-full-small-head-tail-large-v1",
            "sha256": "a" * 64,
            "files": [{"path": "config.json"}],
        },
        "max_new_tokens": 96,
        "generation_config_version": GENERATION_CONFIG_VERSION,
        "generation_config_inherits_base_model": False,
        "generation_config": generation_config,
        "polarity_alias": {
            "enabled": alias_enabled,
            "mapping": {"NEGATIVE": "ADVERSE"},
            "applied_rows": alias_applied_rows,
        },
        "evaluator_gate_advisory_only": True,
        "checkpoint_selection_authority": (
            "summarize_qwen_v4_checkpoint_evaluations.py strict selector gate"
        ),
        "predictions_sha256": _sha256(predictions_path),
        "metrics": summarize_predictions(predictions),
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    return report_path


def _read_predictions(report: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (report.parent / "predictions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def _rewrite_bound_predictions(
    report: Path,
    rows: list[dict[str, Any]],
    *,
    recompute_report_metrics: bool = False,
) -> None:
    predictions_path = report.parent / "predictions.jsonl"
    _write_predictions(predictions_path, rows)
    value = json.loads(report.read_text(encoding="utf-8"))
    value["predictions_sha256"] = _sha256(predictions_path)
    if recompute_report_metrics:
        value["metrics"] = summarize_predictions(rows)
    report.write_text(json.dumps(value), encoding="utf-8")


def test_selects_lowest_fpr_then_higher_metrics_from_predictions(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    reports = [
        _report(tmp_path / "step28.json", dataset, 28, false_positives=6, false_negatives=2),
        _report(tmp_path / "step56.json", dataset, 56, false_positives=4, false_negatives=7),
        _report(tmp_path / "step84.json", dataset, 84, false_positives=4, false_negatives=4),
    ]
    result = summarize(
        report_paths=reports,
        expected_dataset=dataset,
        output=tmp_path / "selection.json",
    )
    selected = result["selected_checkpoint"]
    assert selected["checkpoint_step"] == 84
    assert selected["metrics_source"] == "RECOMPUTED_FROM_HASH_BOUND_PREDICTIONS"
    assert selected["report_core_metrics_verified"] is True
    assert selected["predictions_dataset_binding_verified"] is True
    assert result["dataset_rows_verified"] == 140
    assert result["prediction_metrics_recomputed"] is True
    assert result["decision"] == "DEV_CANDIDATE_FROZEN"
    assert result["reserved_benchmark_opened"] is False
    assert result["strict_selector_gate_authoritative"] is True
    assert result["evaluator_gate_used_for_selection"] is False
    assert result["selection_standard"] == "STRICT_SELECTOR_GATE_ONLY"


def test_returns_no_candidate_when_recomputed_strict_gate_fails(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    report = _report(tmp_path / "step28.json", dataset, 28, false_positives=9)
    result = summarize(
        report_paths=[report],
        expected_dataset=dataset,
        output=tmp_path / "selection.json",
    )
    assert result["selected_checkpoint"] is None
    assert result["evaluated_checkpoints"][0]["metrics"]["false_priority_rate"] == 0.09
    assert result["decision"] == "NO_DEV_CHECKPOINT_QUALIFIED"


def test_rejects_reserved_test_report_and_dataset_mismatch(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    reserved = _report(
        tmp_path / "reserved.json", dataset, 28, reserved_test_only=True
    )
    with pytest.raises(ValueError, match="reserved TEST"):
        summarize(
            report_paths=[reserved],
            expected_dataset=dataset,
            output=tmp_path / "selection-a.json",
        )

    mismatch = _report(tmp_path / "mismatch.json", dataset, 56)
    value = json.loads(mismatch.read_text(encoding="utf-8"))
    value["dataset_sha256"] = "0" * 64
    mismatch.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError, match="dataset digest mismatch"):
        summarize(
            report_paths=[mismatch],
            expected_dataset=dataset,
            output=tmp_path / "selection-b.json",
        )


def test_selector_accepts_only_dev_selection_role_and_dev_rows(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    diagnostic = _report(
        tmp_path / "diagnostic.json",
        dataset,
        28,
        dataset_role="DIAGNOSTIC_ONLY",
    )
    with pytest.raises(ValueError, match="accepts only DEV_SELECTION_ONLY"):
        summarize(
            report_paths=[diagnostic],
            expected_dataset=dataset,
            output=tmp_path / "selection-a.json",
        )

    wrong_split_dataset = _dataset(tmp_path / "wrong-split.jsonl")
    rows = _dataset_rows(wrong_split_dataset)
    rows[0]["metadata"]["split"] = "TEST"
    wrong_split_dataset.write_text(
        "".join(stable_json(row) + "\n" for row in rows), encoding="utf-8"
    )
    wrong_split_report = _report(tmp_path / "wrong-split-report.json", wrong_split_dataset, 56)
    with pytest.raises(ValueError, match="metadata.split=DEV"):
        summarize(
            report_paths=[wrong_split_report],
            expected_dataset=wrong_split_dataset,
            output=tmp_path / "selection-b.json",
        )


def test_selector_verifies_adjacent_predictions_digest(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    report = _report(tmp_path / "step28.json", dataset, 28)
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
        (
            {"generation_extra": {"repetition_penalty": 1.0}},
            "generation configuration mismatch",
        ),
    ],
)
def test_selector_requires_comparable_model_and_generation_configuration(
    tmp_path: Path, second_kwargs: dict[str, Any], message: str
) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    reports = [
        _report(tmp_path / "step28.json", dataset, 28),
        _report(tmp_path / "step56.json", dataset, 56, **second_kwargs),
    ]
    with pytest.raises(ValueError, match=message):
        summarize(
            report_paths=reports,
            expected_dataset=dataset,
            output=tmp_path / "selection.json",
        )


def test_selector_accepts_additional_bound_generation_fields(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    extra = {
        "repetition_penalty": 1.0,
        "eos_token_id": 151645,
        "pad_token_id": 151643,
    }
    report = _report(
        tmp_path / "step28.json", dataset, 28, generation_extra=extra
    )
    result = summarize(
        report_paths=[report],
        expected_dataset=dataset,
        output=tmp_path / "selection.json",
    )
    assert result["generation_config"] == {
        "max_new_tokens": 96,
        "do_sample": False,
        **extra,
    }


def test_token_accuracy_cannot_replace_recomputed_semantic_metrics(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    report = _report(
        tmp_path / "step28.json",
        dataset,
        28,
        false_positives=40,
        token_acc=1.0,
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
def test_rejects_nonfinite_or_out_of_range_report_rates(tmp_path: Path, value: float) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    report = _report(
        tmp_path / "bad.json",
        dataset,
        28,
        metric_override=("false_priority_rate", value),
    )
    with pytest.raises(ValueError, match="outside"):
        summarize(
            report_paths=[report],
            expected_dataset=dataset,
            output=tmp_path / "selection.json",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rows", 139),
        ("contract_valid_rows", 139),
        ("parse_success_rate", 0.99),
        ("exact_payload_accuracy", 0.99),
        ("materiality_macro_f1", 0.99),
        ("polarity_macro_f1", 0.99),
        ("priority_support", 39),
        ("non_priority_support", 99),
        ("priority_recall", 0.99),
        ("false_priority_rate", 0.01),
    ],
)
def test_rejects_report_core_metric_tampering(
    tmp_path: Path, field: str, value: Any
) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    report = _report(
        tmp_path / "tampered.json",
        dataset,
        28,
        metric_override=(field, value),
    )
    with pytest.raises(
        ValueError,
        match="report core metrics mismatch|exceeds rows|do not sum|does not match",
    ):
        summarize(
            report_paths=[report],
            expected_dataset=dataset,
            output=tmp_path / "selection.json",
        )


def test_rejects_prediction_row_count_and_duplicate_sample_id(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    truncated = _report(tmp_path / "truncated.json", dataset, 28)
    rows = _read_predictions(truncated)
    _rewrite_bound_predictions(truncated, rows[:-1])
    with pytest.raises(ValueError, match="row count does not match DEV dataset"):
        summarize(
            report_paths=[truncated],
            expected_dataset=dataset,
            output=tmp_path / "selection-a.json",
        )

    duplicate = _report(tmp_path / "duplicate.json", dataset, 56)
    rows = _read_predictions(duplicate)
    rows[-1] = deepcopy(rows[0])
    _rewrite_bound_predictions(duplicate, rows)
    with pytest.raises(ValueError, match="duplicate prediction sample_id"):
        summarize(
            report_paths=[duplicate],
            expected_dataset=dataset,
            output=tmp_path / "selection-b.json",
        )

    unknown = _report(tmp_path / "unknown.json", dataset, 84)
    rows = _read_predictions(unknown)
    rows[-1]["sample_id"] = "unknown-sample"
    _rewrite_bound_predictions(unknown, rows)
    with pytest.raises(ValueError, match="sample_id is not in DEV dataset"):
        summarize(
            report_paths=[unknown],
            expected_dataset=dataset,
            output=tmp_path / "selection-c.json",
        )


def test_sample_id_normalization_matches_evaluator_preflight(tmp_path: Path) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    rows = _dataset_rows(dataset)
    rows[0]["metadata"]["sample_id"] = " sample-000 "
    rows[1]["metadata"]["sample_id"] = 7
    dataset.write_text(
        "".join(stable_json(row) + "\n" for row in rows), encoding="utf-8"
    )
    report = _report(tmp_path / "step28.json", dataset, 28)
    result = summarize(
        report_paths=[report],
        expected_dataset=dataset,
        output=tmp_path / "selection.json",
    )
    assert result["dataset_rows_verified"] == 140


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda row: row.update({"expected": deepcopy(ROUTINE_PAYLOAD)}),
            "expected payload does not match DEV dataset",
        ),
        (
            lambda row: row["expected"].update({"unexpected": "value"}),
            "expected payload violates core-v1",
        ),
        (
            lambda row: row.update({"event_id": "wrong-event"}),
            "event_id does not match DEV dataset",
        ),
        (
            lambda row: row.update({"raw_output": stable_json(PRIORITY_PAYLOAD)}),
            "payload does not match raw_output",
        ),
        (
            lambda row: row.update({"contract_valid": False}),
            "contract_valid mismatch",
        ),
        (
            lambda row: row.update({"contract_issues": ["invented_issue"]}),
            "contract_issues mismatch",
        ),
        (
            lambda row: row.update({"exact_match": True}),
            "exact_match mismatch",
        ),
    ],
)
def test_rejects_prediction_dataset_or_contract_inconsistency(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    report = _report(tmp_path / "step28.json", dataset, 28)
    rows = _read_predictions(report)
    mutate(rows[0])
    _rewrite_bound_predictions(report, rows)
    with pytest.raises(ValueError, match=message):
        summarize(
            report_paths=[report],
            expected_dataset=dataset,
            output=tmp_path / "selection.json",
        )


def test_validly_recorded_invalid_prediction_is_scored_and_fails_parse_gate(
    tmp_path: Path,
) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    report = _report(tmp_path / "step28.json", dataset, 28)
    rows = _read_predictions(report)
    rows[0].update(
        {
            "predicted": None,
            "raw_output": "not json",
            "contract_issues": ["payload_not_object"],
            "contract_valid": False,
            "exact_match": False,
        }
    )
    _rewrite_bound_predictions(report, rows, recompute_report_metrics=True)
    result = summarize(
        report_paths=[report],
        expected_dataset=dataset,
        output=tmp_path / "selection.json",
    )
    checkpoint = result["evaluated_checkpoints"][0]
    assert checkpoint["metrics"]["contract_valid_rows"] == 139
    assert checkpoint["metrics"]["parse_success_rate"] == 139 / 140
    assert checkpoint["checks"]["parse_success_rate_eq_1_00"] is False
    assert checkpoint["passed"] is False


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("priority_recall", "priority recall presence does not match support"),
        ("false_priority_rate", "false_priority_rate presence does not match support"),
    ],
)
def test_rejects_null_routing_rates_when_denominator_is_nonzero(
    tmp_path: Path, field: str, message: str
) -> None:
    dataset = _dataset(tmp_path / "dev.jsonl")
    report = _report(
        tmp_path / "step28.json",
        dataset,
        28,
        metric_override=(field, None),
    )
    with pytest.raises(ValueError, match=message):
        summarize(
            report_paths=[report],
            expected_dataset=dataset,
            output=tmp_path / "selection.json",
        )


@pytest.mark.parametrize(
    ("priority_rows", "routine_rows", "field"),
    [
        (0, 140, "priority_recall"),
        (140, 0, "false_priority_rate"),
    ],
)
def test_zero_denominator_routing_rate_remains_none_and_cannot_pass(
    tmp_path: Path, priority_rows: int, routine_rows: int, field: str
) -> None:
    dataset = _dataset(
        tmp_path / "dev.jsonl",
        priority_rows=priority_rows,
        routine_rows=routine_rows,
    )
    report = _report(tmp_path / "step28.json", dataset, 28)
    result = summarize(
        report_paths=[report],
        expected_dataset=dataset,
        output=tmp_path / "selection.json",
    )
    checkpoint = result["evaluated_checkpoints"][0]
    assert checkpoint["metrics"][field] is None
    assert checkpoint["passed"] is False


def test_selector_recomputes_core_metrics_from_axes_raw_output(tmp_path: Path) -> None:
    dataset = _axes_dataset(tmp_path / "axes-dev.jsonl")
    report = _axes_report(tmp_path / "axes-step.json", dataset, 28)

    result = summarize(
        report_paths=[report],
        expected_dataset=dataset,
        output=tmp_path / "selection.json",
    )

    selected = result["selected_checkpoint"]
    assert selected["checkpoint_step"] == 28
    assert selected["model_output_contract"] == AXES_MODEL_OUTPUT_CONTRACT
    assert selected["prompt_version"] == AXES_PROMPT_VERSION
    assert selected["prompt_sha256"] == AXES_PROMPT_SHA256
    assert selected["prompt_binding_verified"] is True
    assert selected["generation_config_version"] == GENERATION_CONFIG_VERSION
    assert selected["generation_config_inherits_base_model"] is False
    assert result["model_output_contract"] == AXES_MODEL_OUTPUT_CONTRACT
    assert result["polarity_alias_policy"]["enabled"] is False


def test_selector_recomputes_and_binds_negative_alias_from_axes_raw_output(
    tmp_path: Path,
) -> None:
    dataset = _axes_dataset(tmp_path / "axes-dev.jsonl")
    report = _axes_report(
        tmp_path / "axes-step.json",
        dataset,
        28,
        alias_enabled=True,
        alias_row=40,
    )

    result = summarize(
        report_paths=[report],
        expected_dataset=dataset,
        output=tmp_path / "selection.json",
    )

    checkpoint = result["evaluated_checkpoints"][0]
    assert checkpoint["polarity_alias"] == {
        "enabled": True,
        "mapping": {"NEGATIVE": "ADVERSE"},
        "applied_rows": 1,
    }
    assert result["polarity_alias_policy"]["enabled"] is True


@pytest.mark.parametrize(
    ("mutate_prediction", "message"),
    [
        (
            lambda row: row.update({"parsed_model_output": {"materiality": "UNCLEAR"}}),
            "parsed_model_output does not match raw_output",
        ),
        (
            lambda row: row.update({"normalized_model_output": {"polarity": "NEUTRAL"}}),
            "normalized_model_output mismatch",
        ),
        (
            lambda row: row.update({"predicted": deepcopy(PRIORITY_PAYLOAD)}),
            "payload does not match raw_output",
        ),
        (
            lambda row: row.update({"contract_issues": ["invented"]}),
            "contract_issues mismatch",
        ),
        (
            lambda row: row.update({"contract_valid": False}),
            "contract_valid mismatch",
        ),
        (
            lambda row: row.update({"exact_match": True}),
            "exact_match mismatch",
        ),
        (
            lambda row: row.update({"polarity_alias_applied": True}),
            "polarity_alias_applied mismatch",
        ),
    ],
)
def test_axes_selector_rejects_prediction_derivation_tampering(
    tmp_path: Path,
    mutate_prediction: Callable[[dict[str, Any]], None],
    message: str,
) -> None:
    dataset = _axes_dataset(tmp_path / "axes-dev.jsonl")
    report = _axes_report(tmp_path / "axes-step.json", dataset, 28)
    rows = _read_predictions(report)
    mutate_prediction(rows[0])
    _rewrite_bound_predictions(report, rows)

    with pytest.raises(ValueError, match=message):
        summarize(
            report_paths=[report],
            expected_dataset=dataset,
            output=tmp_path / "selection.json",
        )


@pytest.mark.parametrize(
    ("report_field", "value", "message"),
    [
        ("model_output_contract", "wrong-contract", "unsupported model_output_contract"),
        ("prompt_version", "wrong-version", "prompt_version does not match DEV dataset"),
        ("prompt_sha256", "0" * 64, "prompt_sha256 does not match DEV dataset"),
        ("prompt_binding_verified", False, "prompt binding not verified"),
        ("generation_config_version", "wrong-version", "unsupported generation_config_version"),
        ("generation_config_inherits_base_model", True, "inheritance is not disabled"),
    ],
)
def test_axes_selector_rejects_report_contract_tampering(
    tmp_path: Path, report_field: str, value: Any, message: str
) -> None:
    dataset = _axes_dataset(tmp_path / "axes-dev.jsonl")
    report = _axes_report(tmp_path / "axes-step.json", dataset, 28)
    payload = json.loads(report.read_text(encoding="utf-8"))
    payload[report_field] = value
    report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        summarize(
            report_paths=[report],
            expected_dataset=dataset,
            output=tmp_path / "selection.json",
        )


def test_axes_selector_rejects_alias_count_and_cross_checkpoint_policy_mismatch(
    tmp_path: Path,
) -> None:
    dataset = _axes_dataset(tmp_path / "axes-dev.jsonl")
    bad_count = _axes_report(
        tmp_path / "bad-count.json", dataset, 28, alias_enabled=True
    )
    payload = json.loads(bad_count.read_text(encoding="utf-8"))
    payload["polarity_alias"]["applied_rows"] = 1
    bad_count.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="polarity_alias applied_rows mismatch"):
        summarize(
            report_paths=[bad_count],
            expected_dataset=dataset,
            output=tmp_path / "selection-a.json",
        )

    disabled = _axes_report(tmp_path / "disabled.json", dataset, 56)
    enabled = _axes_report(tmp_path / "enabled.json", dataset, 84, alias_enabled=True)
    with pytest.raises(ValueError, match="polarity alias policy mismatch across reports"):
        summarize(
            report_paths=[disabled, enabled],
            expected_dataset=dataset,
            output=tmp_path / "selection-b.json",
        )


def test_axes_selector_rejects_non_explicit_generation_configuration(
    tmp_path: Path,
) -> None:
    dataset = _axes_dataset(tmp_path / "axes-dev.jsonl")
    report = _axes_report(tmp_path / "axes-step.json", dataset, 28)
    payload = json.loads(report.read_text(encoding="utf-8"))
    del payload["generation_config"]["num_beams"]
    report.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="missing explicit fields"):
        summarize(
            report_paths=[report],
            expected_dataset=dataset,
            output=tmp_path / "selection.json",
        )


def test_axes_invalid_raw_output_is_scored_as_parse_failure(tmp_path: Path) -> None:
    dataset = _axes_dataset(tmp_path / "axes-dev.jsonl")
    report = _axes_report(tmp_path / "axes-step.json", dataset, 28)
    rows = _read_predictions(report)
    rows[0].update(
        {
            "raw_output": "```json\n{}\n```",
            "parsed_model_output": None,
            "normalized_model_output": None,
            "predicted": None,
            "polarity_alias_applied": False,
            "contract_issues": ["payload_not_object"],
            "contract_valid": False,
            "exact_match": False,
        }
    )
    _rewrite_bound_predictions(report, rows, recompute_report_metrics=True)

    result = summarize(
        report_paths=[report],
        expected_dataset=dataset,
        output=tmp_path / "selection.json",
    )

    checkpoint = result["evaluated_checkpoints"][0]
    assert checkpoint["metrics"]["contract_valid_rows"] == 139
    assert checkpoint["checks"]["parse_success_rate_eq_1_00"] is False
    assert checkpoint["passed"] is False
