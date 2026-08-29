from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

import pytest

from scripts.evaluate_qwen_semantic_adapter import stable_json, summarize_predictions
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
