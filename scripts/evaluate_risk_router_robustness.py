#!/usr/bin/env python3
"""Run reproducible feature ablations and emit explicit shadow-model drift gates."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from sklearn.metrics import accuracy_score


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from scripts.train_risk_router import build_pipeline, load_dataset, time_issuer_chain_split, utc_now  # noqa: E402


def evaluate_variant(
    mode: str,
    x_train: list[str],
    y_train: list[str],
    x_test: list[str],
    y_test: list[str],
    *,
    threshold: float,
) -> dict[str, Any]:
    pipeline = build_pipeline(mode)
    started = time.perf_counter()
    pipeline.fit(x_train, y_train)
    fit_seconds = time.perf_counter() - started
    started = time.perf_counter()
    probabilities = pipeline.predict_proba(x_test)
    inference_seconds = time.perf_counter() - started
    classes = [str(item) for item in pipeline.classes_]
    raw: list[str] = []
    routed: list[str] = []
    covered_truth: list[str] = []
    covered_predictions: list[str] = []
    for truth, row in zip(y_test, probabilities):
        index = int(row.argmax())
        prediction = classes[index]
        raw.append(prediction)
        routed_label = prediction if float(row[index]) >= threshold else "ABSTAIN"
        routed.append(routed_label)
        if routed_label != "ABSTAIN":
            covered_truth.append(truth)
            covered_predictions.append(routed_label)
    return {
        "feature_mode": mode,
        "fit_seconds": round(fit_seconds, 3),
        "inference_ms_per_row": round(inference_seconds * 1000 / max(1, len(x_test)), 4),
        "raw_argmax_accuracy": accuracy_score(y_test, raw),
        "coverage": len(covered_predictions) / len(y_test),
        "covered_accuracy": accuracy_score(covered_truth, covered_predictions) if covered_predictions else None,
        "route_distribution": dict(Counter(routed)),
        "rows": len(y_test),
    }


def evaluate(db_path: Path, *, threshold: float = 0.62) -> dict[str, Any]:
    ids, texts, labels, records, dataset = load_dataset(db_path)
    train_indices, test_indices, split = time_issuer_chain_split(labels, records)
    x_train = [texts[index] for index in train_indices]
    y_train = [labels[index] for index in train_indices]
    x_test = [texts[index] for index in test_indices]
    y_test = [labels[index] for index in test_indices]
    variants = [
        evaluate_variant(mode, x_train, y_train, x_test, y_test, threshold=threshold)
        for mode in ("word_only", "char_only", "combined")
    ]
    combined = next(item for item in variants if item["feature_mode"] == "combined")
    test_dates = [records[index]["event_date"] for index in test_indices]
    reference_distribution = {
        label: count / len(test_indices)
        for label, count in combined["route_distribution"].items()
    }
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "dataset": dataset,
        "holdout": {
            **split,
            "event_ids_sha256_only": True,
            "date_range": [min(test_dates), max(test_dates)],
            "note": "This is a frozen grouped temporal holdout, not a never-observed external blind set.",
        },
        "ablation": variants,
        "monitoring_policy": {
            "mode": "shadow_only",
            "minimum_window_rows": 100,
            "reference_route_distribution": reference_distribution,
            "warn_if": {
                "absolute_abstain_rate_delta_gte": 0.15,
                "absolute_risk_review_rate_delta_gte": 0.20,
                "mean_confidence_delta_gte": 0.12,
                "p95_latency_ms_gte": 100.0,
            },
            "block_promotion_if": {
                "covered_accuracy_lt": 0.85,
                "coverage_lt": 0.65,
                "issuer_overlap_count_gt": 0,
                "event_chain_overlap_count_gt": 0,
                "unresolved_drift_warning": True,
            },
        },
        "no_trading": True,
        "promotion_decision": "REMAIN_SHADOW",
    }


def main() -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=settings.ledger_db)
    parser.add_argument(
        "--output",
        type=Path,
        default=settings.artifact_dir / "risk_router_robustness.json",
    )
    parser.add_argument("--abstain-threshold", type=float, default=0.62)
    args = parser.parse_args()
    report = evaluate(args.db, threshold=args.abstain_threshold)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
