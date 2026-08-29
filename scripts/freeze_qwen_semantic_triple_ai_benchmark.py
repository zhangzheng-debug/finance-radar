#!/usr/bin/env python3
"""Freeze an AI-arbitrated, leak-audited Qwen benchmark.

The benchmark is deliberately labelled AI_NOT_HUMAN_GOLD.  It contains no
prior Qwen predictions and no market outcomes.  The core-v1 file is suitable
for the currently deployed Qwen output contract; the v2 sidecar retains the
independent mechanism labels for later multi-task work and audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.qwen_risk_contract import (  # noqa: E402
    expected_semantic_payload,
    normalize_qwen_risk_content,
)
from app.models.qwen_risk_contract_v2 import (  # noqa: E402
    QWEN_RISK_CONTRACT_V2_VERSION,
    validate_semantic_v2_payload,
)
from scripts.build_qwen_semantic_core_v4_weak_dataset import SYSTEM_PROMPT, stable_json  # noqa: E402


BENCHMARK_CONTRACT = "qwen-triple-ai-strict60-v1"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    data = "".join(stable_json(row) + "\n" for row in rows).encode("utf-8")
    atomic_write(path, data)
    return hashlib.sha256(data).hexdigest()


def _reviews(path: Path) -> tuple[list[str], dict[str, dict[str, Any]]]:
    rows = read_jsonl(path)
    order: list[str] = []
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        sample_id = str(row.get("sample_id") or "")
        review = row.get("review")
        if not sample_id or sample_id in result or not isinstance(review, dict):
            raise ValueError(f"invalid or duplicate review row in {path}")
        issues = validate_semantic_v2_payload(review)
        if issues:
            raise ValueError(f"invalid review for {sample_id} in {path}: {issues}")
        order.append(sample_id)
        result[sample_id] = review
    return order, result


def freeze_benchmark(
    *,
    provider_input: Path,
    source_index: Path,
    review_a: Path,
    review_b: Path,
    arbiter: Path,
    output_dir: Path,
) -> dict[str, Any]:
    providers = read_jsonl(provider_input)
    provider_order = [str(row.get("sample_id") or "") for row in providers]
    if not all(provider_order) or len(set(provider_order)) != len(provider_order):
        raise ValueError("provider sample IDs missing or duplicated")
    index_all = read_jsonl(source_index)
    index_by_id = {str(row.get("sample_id") or ""): row for row in index_all}
    missing_index = sorted(set(provider_order) - set(index_by_id))
    if missing_index:
        raise ValueError(f"provider rows missing source index: {missing_index[:3]}")

    order_a, labels_a = _reviews(review_a)
    order_b, labels_b = _reviews(review_b)
    order_c, labels_c = _reviews(arbiter)
    if not (provider_order == order_a == order_b == order_c):
        raise ValueError("provider/reviewer/arbiter order mismatch")

    axes = (
        "materiality", "polarity", "impact_strength", "event_realization",
        "subject_relation", "risk_status", "novelty",
    )
    agreement = {axis: sum(labels_a[s][axis] == labels_b[s][axis] for s in provider_order) for axis in axes}
    all_axis_agreement = sum(all(labels_a[s][axis] == labels_b[s][axis] for axis in axes) for s in provider_order)

    benchmark: list[dict[str, Any]] = []
    v2_truth: list[dict[str, Any]] = []
    for row, sample_id in zip(providers, provider_order):
        content = row.get("content")
        if not isinstance(content, dict):
            raise ValueError(f"provider content missing for {sample_id}")
        normalized = normalize_qwen_risk_content(content)
        final = labels_c[sample_id]
        target = expected_semantic_payload(final["materiality"], final["polarity"])
        source = index_by_id[sample_id]
        metadata = {
            "sample_id": sample_id,
            "event_id": source.get("source_event_id"),
            "entity_group": source.get("entity_group"),
            "event_chain_group": source.get("event_chain_group"),
            "content_sha256": hashlib.sha256(stable_json(normalized).encode("utf-8")).hexdigest(),
            "split": "SEALED_STRICT60_TEST",
            "label_provenance": "THREE_INDEPENDENT_AI_REVIEWS_WITH_AI_ARBITRATION",
            "label_classification": "AI_NOT_HUMAN_GOLD",
            "reviewer_ab_all_axis_agreement": all(labels_a[sample_id][axis] == labels_b[sample_id][axis] for axis in axes),
            "qwen_prediction_included": False,
            "post_event_market_data_included": False,
            "human_gold_claimed": False,
        }
        benchmark.append({
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": stable_json(normalized)},
                {"role": "assistant", "content": stable_json(target)},
            ],
            "expected": target,
            "metadata": metadata,
        })
        v2_truth.append({
            "sample_id": sample_id,
            "review_a": labels_a[sample_id],
            "review_b": labels_b[sample_id],
            "arbiter_final": final,
            "source_index": source,
            "classification": "AI_NOT_HUMAN_GOLD",
        })

    output_dir.mkdir(parents=True, exist_ok=True)
    benchmark_path = output_dir / "qwen_strict60_core_v1.jsonl"
    truth_path = output_dir / "qwen_strict60_full_v2_truth.jsonl"
    hashes = {
        "core_v1": _write_jsonl(benchmark_path, benchmark),
        "full_v2_truth": _write_jsonl(truth_path, v2_truth),
    }
    pair_counts = Counter(
        f"{row['expected']['materiality']}|{row['expected']['polarity']}" for row in benchmark
    )
    manifest = {
        "schema_version": 1,
        "benchmark_contract": BENCHMARK_CONTRACT,
        "semantic_v2_contract": QWEN_RISK_CONTRACT_V2_VERSION,
        "classification": "AI_NOT_HUMAN_GOLD",
        "row_count": len(benchmark),
        "input_sha256": {
            "provider_input": sha256_file(provider_input),
            "source_index": sha256_file(source_index),
            "review_a": sha256_file(review_a),
            "review_b": sha256_file(review_b),
            "arbiter": sha256_file(arbiter),
        },
        "output_sha256": hashes,
        "reviewer_ab_axis_agreement": agreement,
        "reviewer_ab_all_axis_agreement": all_axis_agreement,
        "core_pair_counts": dict(sorted(pair_counts.items())),
        "qwen_predictions_used": False,
        "market_outcomes_used": False,
        "human_gold_claimed": False,
        "production_model_changed": False,
    }
    manifest_path = output_dir / "manifest.json"
    atomic_write(manifest_path, (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    atomic_write(output_dir / "manifest.json.sha256", (sha256_file(manifest_path) + "  manifest.json\n").encode("ascii"))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-input", type=Path, required=True)
    parser.add_argument("--source-index", type=Path, required=True)
    parser.add_argument("--review-a", type=Path, required=True)
    parser.add_argument("--review-b", type=Path, required=True)
    parser.add_argument("--arbiter", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = freeze_benchmark(
        provider_input=args.provider_input.resolve(), source_index=args.source_index.resolve(),
        review_a=args.review_a.resolve(), review_b=args.review_b.resolve(), arbiter=args.arbiter.resolve(),
        output_dir=args.output_dir.resolve(),
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
