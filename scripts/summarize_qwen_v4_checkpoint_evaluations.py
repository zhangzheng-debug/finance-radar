#!/usr/bin/env python3
"""Select a Qwen v4 checkpoint from issuer-isolated DEV reports only.

This script does not run inference or inspect a reserved benchmark.  It verifies
the adjacent prediction-file digest for every DEV-only report, requires matching
base-model fingerprints and generation settings, applies the authoritative
frozen v4 development gates, and ranks only checkpoints that pass every gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any


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
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field} in {path}: {value!r}") from exc
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(f"{field} outside [0, 1] in {path}: {value!r}")
    return number


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
    if set(value) != {"max_new_tokens", "do_sample"}:
        raise ValueError(f"unsupported generation_config in {path}")
    max_new_tokens = value.get("max_new_tokens")
    if isinstance(max_new_tokens, bool) or not isinstance(max_new_tokens, int) or max_new_tokens < 1:
        raise ValueError(f"invalid max_new_tokens in {path}")
    if value.get("do_sample") is not False:
        raise ValueError(f"selection requires deterministic generation in {path}")
    return value


def _load_report(path: Path, expected_dataset_sha256: str) -> dict[str, Any]:
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
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        raise ValueError(f"metrics missing: {path}")
    adapter = str(report.get("adapter") or "")
    step = _checkpoint_step(adapter)
    priority = metrics.get("priority_review")
    materiality = metrics.get("materiality")
    polarity = metrics.get("polarity")
    if not all(isinstance(value, dict) for value in (priority, materiality, polarity)):
        raise ValueError(f"required metric blocks missing: {path}")

    rows = int(metrics.get("rows") or 0)
    priority_support = int(priority.get("support") or 0)
    if rows < 0 or priority_support < 0 or priority_support > rows:
        raise ValueError(f"invalid row/support counts in {path}: {rows}/{priority_support}")
    values = {
        "rows": rows,
        "priority_support": priority_support,
        "parse_success_rate": _bounded_rate(
            metrics.get("parse_success_rate") or 0.0,
            field="parse_success_rate", path=path,
        ),
        "exact_payload_accuracy": _bounded_rate(
            metrics.get("exact_payload_accuracy") or 0.0,
            field="exact_payload_accuracy", path=path,
        ),
        "materiality_macro_f1": _bounded_rate(
            materiality.get("macro_f1_truth_supported_classes") or 0.0,
            field="materiality_macro_f1", path=path,
        ),
        "polarity_macro_f1": _bounded_rate(
            polarity.get("macro_f1_truth_supported_classes") or 0.0,
            field="polarity_macro_f1", path=path,
        ),
        "priority_recall": _bounded_rate(
            priority.get("recall") or 0.0,
            field="priority_recall", path=path,
        ),
        "false_priority_rate": (
            None
            if priority.get("false_priority_rate") is None
            else _bounded_rate(
                priority["false_priority_rate"], field="false_priority_rate", path=path,
            )
        ),
    }
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
            values["priority_recall"] >= GATE_THRESHOLDS["priority_recall_min"]
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
    rows = [_load_report(path.resolve(), dataset_sha256) for path in report_paths]
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
