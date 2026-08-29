#!/usr/bin/env python3
"""Select a Qwen v4 checkpoint from issuer-isolated DEV reports only.

This script does not run inference or inspect a reserved benchmark.  It verifies
and independently scores the adjacent prediction file for every DEV-only report,
requires matching base-model fingerprints and generation settings, applies the
authoritative frozen v4 development gates, and ranks only checkpoints that pass
every gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.qwen_risk_contract import validate_semantic_payload  # noqa: E402
from scripts.evaluate_qwen_semantic_adapter import (  # noqa: E402
    extract_json_object,
    load_evaluation_dataset,
    normalize_payload,
    summarize_predictions,
)


CHECKPOINT_RE = re.compile(r"(?:^|[\\/])checkpoint-(\d+)(?:$|[\\/])")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEV_SELECTION_ROLE = "DEV_SELECTION_ONLY"
REPORT_SCHEMA_VERSION = 2
TARGET_CONTRACT = "core-v1"
GATE_THRESHOLDS = {
    "rows_min": 120,
    "priority_support_min": 20,
    "parse_success_rate_min": 1.0,
    "exact_payload_accuracy_min": 0.75,
    "materiality_macro_f1_min": 0.70,
    "polarity_macro_f1_min": 0.65,
    "priority_recall_min": 0.80,
    "false_priority_rate_max": 0.08,
}


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checkpoint_step(adapter: str) -> int:
    match = CHECKPOINT_RE.search(adapter)
    if not match:
        raise ValueError(f"adapter is not a checkpoint-N path: {adapter}")
    return int(match.group(1))


def _bounded_rate(value: Any, *, field: str, path: Path) -> float:
    if isinstance(value, bool):
        raise ValueError(f"invalid {field} in {path}: {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field} in {path}: {value!r}") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field} outside [0, 1] in {path}: {value!r}")
    return number


def _nonnegative_int(value: Any, *, field: str, path: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"invalid {field} in {path}: {value!r}")
    return value


def _fingerprint(value: Any, *, field: str, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} missing in {path}")
    digest = str(value.get("sha256") or "").strip().lower()
    scheme = str(value.get("scheme") or "").strip()
    files = value.get("files")
    if not SHA256_RE.fullmatch(digest) or not scheme or not isinstance(files, list) or not files:
        raise ValueError(f"invalid {field} in {path}")
    return value


def _generation_config(value: Any, *, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"generation_config missing in {path}")
    required = {"max_new_tokens", "do_sample"}
    missing = sorted(required - set(value))
    if missing:
        raise ValueError(f"generation_config missing required fields in {path}: {missing}")
    max_new_tokens = value.get("max_new_tokens")
    if isinstance(max_new_tokens, bool) or not isinstance(max_new_tokens, int) or max_new_tokens < 1:
        raise ValueError(f"invalid max_new_tokens in {path}")
    if value.get("do_sample") is not False:
        raise ValueError(f"selection requires deterministic generation in {path}")
    return value


def _load_expected_dataset(path: Path) -> dict[str, dict[str, Any]]:
    rows = load_evaluation_dataset(path, dataset_role=DEV_SELECTION_ROLE)
    expected_by_sample: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, start=1):
        sample_id = str(row["metadata"].get("sample_id") or "").strip()
        expected = normalize_payload(json.loads(row["messages"][-1]["content"]))
        if expected is None:
            raise ValueError(f"DEV dataset sample has invalid expected payload: {sample_id}")
        expected_by_sample[sample_id] = {
            "index": index,
            "event_id": row["metadata"].get("event_id"),
            "benchmark_stratum": row["metadata"].get("benchmark_stratum"),
            "expected": expected,
        }
    return expected_by_sample


def _load_bound_predictions(
    path: Path, *, expected_by_sample: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    raw_rows: list[tuple[int, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw_rows.append((line_number, json.loads(line)))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"predictions.jsonl line {line_number} is not valid JSON: {path}"
            ) from exc
    if len(raw_rows) != len(expected_by_sample):
        raise ValueError(
            "predictions.jsonl row count does not match DEV dataset: "
            f"{len(raw_rows)} != {len(expected_by_sample)} in {path}"
        )

    seen_sample_ids: set[str] = set()
    scored_rows: list[dict[str, Any]] = []
    for line_number, row in raw_rows:
        if not isinstance(row, dict):
            raise ValueError(f"predictions.jsonl line {line_number} is not an object: {path}")
        sample_id = str(row.get("sample_id") or "").strip()
        if not sample_id:
            raise ValueError(f"predictions.jsonl line {line_number} has no sample_id: {path}")
        if sample_id in seen_sample_ids:
            raise ValueError(f"duplicate prediction sample_id {sample_id!r} in {path}")
        seen_sample_ids.add(sample_id)
        dataset_row = expected_by_sample.get(sample_id)
        if dataset_row is None:
            raise ValueError(f"prediction sample_id is not in DEV dataset: {sample_id!r}")
        reported_index = row.get("index")
        if (
            isinstance(reported_index, bool)
            or not isinstance(reported_index, int)
            or reported_index != dataset_row["index"]
        ):
            raise ValueError(f"prediction index does not match DEV dataset for {sample_id!r}")
        if row.get("event_id") != dataset_row["event_id"]:
            raise ValueError(f"prediction event_id does not match DEV dataset for {sample_id!r}")
        if row.get("benchmark_stratum") != dataset_row["benchmark_stratum"]:
            raise ValueError(
                f"prediction benchmark_stratum does not match DEV dataset for {sample_id!r}"
            )

        raw_expected = row.get("expected")
        expected_issues = validate_semantic_payload(raw_expected)
        expected = normalize_payload(raw_expected)
        if expected_issues:
            raise ValueError(
                f"prediction expected payload violates {TARGET_CONTRACT} for {sample_id!r}: "
                + ",".join(expected_issues)
            )
        if raw_expected != expected:
            raise ValueError(f"prediction expected payload is not canonical for {sample_id!r}")
        if expected != dataset_row["expected"]:
            raise ValueError(f"prediction expected payload does not match DEV dataset for {sample_id!r}")

        raw_output = row.get("raw_output")
        if not isinstance(raw_output, str):
            raise ValueError(f"prediction raw_output is not text for {sample_id!r}")
        predicted = normalize_payload(extract_json_object(raw_output))
        reported_predicted = row.get("predicted")
        if reported_predicted != predicted:
            raise ValueError(f"prediction payload does not match raw_output for {sample_id!r}")
        predicted_issues = validate_semantic_payload(predicted)
        contract_valid = not predicted_issues
        reported_contract_valid = row.get("contract_valid")
        if not isinstance(reported_contract_valid, bool):
            raise ValueError(f"prediction contract_valid is not boolean for {sample_id!r}")
        if reported_contract_valid != contract_valid:
            raise ValueError(f"prediction contract_valid mismatch for {sample_id!r}")
        reported_contract_issues = row.get("contract_issues")
        if not isinstance(reported_contract_issues, list):
            raise ValueError(f"prediction contract_issues is not a list for {sample_id!r}")
        if reported_contract_issues != predicted_issues:
            raise ValueError(f"prediction contract_issues mismatch for {sample_id!r}")

        exact_match = bool(contract_valid and predicted == expected)
        reported_exact_match = row.get("exact_match")
        if not isinstance(reported_exact_match, bool):
            raise ValueError(f"prediction exact_match is not boolean for {sample_id!r}")
        if reported_exact_match != exact_match:
            raise ValueError(f"prediction exact_match mismatch for {sample_id!r}")
        scored_rows.append(
            {
                "expected": expected,
                "predicted": predicted,
                "contract_valid": contract_valid,
                "exact_match": exact_match,
            }
        )

    missing = sorted(set(expected_by_sample) - seen_sample_ids)
    if missing:
        raise ValueError(f"predictions.jsonl is missing DEV sample_ids: {missing[:3]}")
    return scored_rows


def _core_metric_values(metrics: Any, *, path: Path) -> dict[str, Any]:
    if not isinstance(metrics, dict):
        raise ValueError(f"metrics missing: {path}")
    priority = metrics.get("priority_review")
    materiality = metrics.get("materiality")
    polarity = metrics.get("polarity")
    if not all(isinstance(value, dict) for value in (priority, materiality, polarity)):
        raise ValueError(f"required metric blocks missing: {path}")

    rows = _nonnegative_int(metrics.get("rows"), field="rows", path=path)
    contract_valid_rows = _nonnegative_int(
        metrics.get("contract_valid_rows"), field="contract_valid_rows", path=path
    )
    priority_support = _nonnegative_int(
        priority.get("support"), field="priority_support", path=path
    )
    non_priority_support = _nonnegative_int(
        priority.get("non_priority_support"), field="non_priority_support", path=path
    )
    if contract_valid_rows > rows:
        raise ValueError(f"contract_valid_rows exceeds rows in {path}")
    if priority_support + non_priority_support != rows:
        raise ValueError(f"priority support counts do not sum to rows in {path}")

    parse_success_rate = _bounded_rate(
        metrics.get("parse_success_rate"), field="parse_success_rate", path=path
    )
    expected_parse_rate = contract_valid_rows / rows if rows else 0.0
    if not math.isclose(parse_success_rate, expected_parse_rate, rel_tol=0.0, abs_tol=1e-12):
        raise ValueError(f"parse_success_rate does not match contract_valid_rows in {path}")
    recall_value = priority.get("recall")
    if (priority_support == 0) != (recall_value is None):
        raise ValueError(f"priority recall presence does not match support in {path}")
    priority_recall = (
        None
        if recall_value is None
        else _bounded_rate(recall_value, field="priority_recall", path=path)
    )
    false_priority_value = priority.get("false_priority_rate")
    if (non_priority_support == 0) != (false_priority_value is None):
        raise ValueError(f"false_priority_rate presence does not match support in {path}")
    false_priority_rate = (
        None
        if false_priority_value is None
        else _bounded_rate(false_priority_value, field="false_priority_rate", path=path)
    )
    return {
        "rows": rows,
        "contract_valid_rows": contract_valid_rows,
        "priority_support": priority_support,
        "non_priority_support": non_priority_support,
        "parse_success_rate": parse_success_rate,
        "exact_payload_accuracy": _bounded_rate(
            metrics.get("exact_payload_accuracy"), field="exact_payload_accuracy", path=path
        ),
        "materiality_macro_f1": _bounded_rate(
            materiality.get("macro_f1_truth_supported_classes"),
            field="materiality_macro_f1",
            path=path,
        ),
        "polarity_macro_f1": _bounded_rate(
            polarity.get("macro_f1_truth_supported_classes"),
            field="polarity_macro_f1",
            path=path,
        ),
        "priority_recall": priority_recall,
        "false_priority_rate": false_priority_rate,
    }


def _require_matching_core_metrics(
    *, reported: dict[str, Any], recomputed: dict[str, Any], path: Path
) -> None:
    mismatches: list[str] = []
    for field, actual in recomputed.items():
        claimed = reported.get(field)
        if isinstance(actual, float) and isinstance(claimed, (int, float)):
            equal = math.isclose(actual, float(claimed), rel_tol=0.0, abs_tol=1e-12)
        else:
            equal = claimed == actual
        if not equal:
            mismatches.append(f"{field}:report={claimed!r},recomputed={actual!r}")
    if mismatches:
        raise ValueError(
            f"report core metrics mismatch with predictions.jsonl in {path}: "
            + "; ".join(mismatches)
        )


def _load_report(
    path: Path,
    expected_dataset_sha256: str,
    expected_by_sample: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError(f"report is not an object: {path}")
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ValueError(f"unsupported report schema: {path}")
    if report.get("evaluation_only") is not True:
        raise ValueError(f"report is not evaluation-only: {path}")
    if report.get("production_model_changed") is not False:
        raise ValueError(f"report changed production state: {path}")
    if report.get("dataset_role") != DEV_SELECTION_ROLE:
        raise ValueError(f"selector accepts only {DEV_SELECTION_ROLE}: {path}")
    if report.get("reserved_test_only") is not False:
        raise ValueError(f"reserved TEST report cannot select a checkpoint: {path}")
    if report.get("target_contract") != TARGET_CONTRACT:
        raise ValueError(f"selector requires target_contract={TARGET_CONTRACT}: {path}")
    if report.get("evaluator_gate_advisory_only") is not True:
        raise ValueError(f"evaluator gate authority missing: {path}")
    if str(report.get("dataset_sha256") or "").lower() != expected_dataset_sha256:
        raise ValueError(f"DEV dataset digest mismatch: {path}")
    predictions = path.parent / "predictions.jsonl"
    if not predictions.is_file():
        raise ValueError(f"adjacent predictions.jsonl missing: {path}")
    expected_predictions_sha256 = str(report.get("predictions_sha256") or "").lower()
    if not SHA256_RE.fullmatch(expected_predictions_sha256):
        raise ValueError(f"invalid predictions_sha256: {path}")
    actual_predictions_sha256 = _sha256(predictions)
    if actual_predictions_sha256 != expected_predictions_sha256:
        raise ValueError(f"predictions.jsonl digest mismatch: {path}")
    base_model_fingerprint = _fingerprint(
        report.get("base_model_fingerprint"), field="base_model_fingerprint", path=path
    )
    adapter_fingerprint = _fingerprint(
        report.get("adapter_fingerprint"), field="adapter_fingerprint", path=path
    )
    generation_config = _generation_config(report.get("generation_config"), path=path)
    if report.get("max_new_tokens") != generation_config["max_new_tokens"]:
        raise ValueError(f"max_new_tokens binding mismatch: {path}")
    adapter = str(report.get("adapter") or "")
    step = _checkpoint_step(adapter)
    scored_rows = _load_bound_predictions(
        predictions, expected_by_sample=expected_by_sample
    )
    recomputed_metrics = summarize_predictions(scored_rows)
    recomputed_values = _core_metric_values(recomputed_metrics, path=predictions)
    if recomputed_values["rows"] != len(expected_by_sample):
        raise ValueError(
            f"recomputed prediction rows do not match DEV dataset in {predictions}"
        )
    reported_values = _core_metric_values(report.get("metrics"), path=path)
    _require_matching_core_metrics(
        reported=reported_values, recomputed=recomputed_values, path=path
    )
    values = recomputed_values
    checks = {
        "rows_ge_120": values["rows"] >= GATE_THRESHOLDS["rows_min"],
        "priority_support_ge_20": (
            values["priority_support"] >= GATE_THRESHOLDS["priority_support_min"]
        ),
        "parse_success_rate_eq_1_00": (
            values["parse_success_rate"] >= GATE_THRESHOLDS["parse_success_rate_min"]
        ),
        "exact_payload_accuracy_ge_0_75": (
            values["exact_payload_accuracy"]
            >= GATE_THRESHOLDS["exact_payload_accuracy_min"]
        ),
        "materiality_macro_f1_ge_0_70": (
            values["materiality_macro_f1"]
            >= GATE_THRESHOLDS["materiality_macro_f1_min"]
        ),
        "polarity_macro_f1_ge_0_65": (
            values["polarity_macro_f1"]
            >= GATE_THRESHOLDS["polarity_macro_f1_min"]
        ),
        "priority_recall_ge_0_80": (
            (values["priority_recall"] or 0.0)
            >= GATE_THRESHOLDS["priority_recall_min"]
        ),
        "false_priority_rate_le_0_08": (
            values["false_priority_rate"] is not None
            and values["false_priority_rate"]
            <= GATE_THRESHOLDS["false_priority_rate_max"]
        ),
    }
    return {
        "report_path": str(path.resolve()),
        "report_sha256": _sha256(path),
        "predictions_path": str(predictions.resolve()),
        "predictions_sha256": actual_predictions_sha256,
        "adapter": adapter,
        "adapter_fingerprint": adapter_fingerprint,
        "base_model_fingerprint": base_model_fingerprint,
        "generation_config": generation_config,
        "dataset_role": DEV_SELECTION_ROLE,
        "target_contract": TARGET_CONTRACT,
        "checkpoint_step": step,
        "metrics": values,
        "metrics_source": "RECOMPUTED_FROM_HASH_BOUND_PREDICTIONS",
        "report_core_metrics_verified": True,
        "predictions_dataset_binding_verified": True,
        "checks": checks,
        "passed": all(checks.values()),
    }


def _rank_key(row: dict[str, Any]) -> tuple[Any, ...]:
    metrics = row["metrics"]
    return (
        metrics["false_priority_rate"],
        -metrics["priority_recall"],
        -metrics["polarity_macro_f1"],
        -metrics["materiality_macro_f1"],
        -metrics["exact_payload_accuracy"],
        row["checkpoint_step"],
    )


def summarize(
    *, report_paths: list[Path], expected_dataset: Path, output: Path
) -> dict[str, Any]:
    expected_dataset = expected_dataset.resolve()
    if not expected_dataset.is_file():
        raise FileNotFoundError(expected_dataset)
    if not report_paths:
        raise ValueError("at least one DEV report is required")
    if output.exists():
        raise FileExistsError(output)

    dataset_sha256 = _sha256(expected_dataset)
    expected_by_sample = _load_expected_dataset(expected_dataset)
    rows = [
        _load_report(path.resolve(), dataset_sha256, expected_by_sample)
        for path in report_paths
    ]
    steps = [row["checkpoint_step"] for row in rows]
    if len(set(steps)) != len(steps):
        raise ValueError("duplicate checkpoint step")
    base_model_fingerprint = rows[0]["base_model_fingerprint"]
    generation_config = rows[0]["generation_config"]
    for row in rows[1:]:
        if _stable_json(row["base_model_fingerprint"]) != _stable_json(
            base_model_fingerprint
        ):
            raise ValueError("base model fingerprint mismatch across reports")
        if row["generation_config"] != generation_config:
            raise ValueError("generation configuration mismatch across reports")
    passing = sorted((row for row in rows if row["passed"]), key=_rank_key)
    summary = {
        "schema_version": 2,
        "selection_scope": "ISSUER_ISOLATED_DEV_ONLY",
        "accepted_dataset_role": DEV_SELECTION_ROLE,
        "reserved_benchmark_opened": False,
        "production_model_changed": False,
        "strict_selector_gate_authoritative": True,
        "evaluator_gate_used_for_selection": False,
        "selection_standard": "STRICT_SELECTOR_GATE_ONLY",
        "target_contract": TARGET_CONTRACT,
        "dataset_path": str(expected_dataset),
        "dataset_sha256": dataset_sha256,
        "dataset_rows_verified": len(expected_by_sample),
        "prediction_metrics_recomputed": True,
        "base_model_fingerprint": base_model_fingerprint,
        "generation_config": generation_config,
        "gate_thresholds": GATE_THRESHOLDS,
        "evaluated_checkpoints": sorted(rows, key=lambda row: row["checkpoint_step"]),
        "passing_checkpoint_count": len(passing),
        "selected_checkpoint": passing[0] if passing else None,
        "decision": "DEV_CANDIDATE_FROZEN" if passing else "NO_DEV_CHECKPOINT_QUALIFIED",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--expected-dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    summary = summarize(
        report_paths=args.report,
        expected_dataset=args.expected_dataset,
        output=args.output.resolve(),
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["selected_checkpoint"] is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
