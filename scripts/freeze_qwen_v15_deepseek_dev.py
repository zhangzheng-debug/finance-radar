#!/usr/bin/env python3
"""Freeze a fresh, group-isolated DEV set from completed DeepSeek reviews.

The command consumes only anonymous provider input, its owner-only identity
index, a completed multi-view progress ledger, and prior exposure indexes.  It
never reads Qwen predictions, market outcomes, or the strict-test provider
payload.  Eligibility is decided before labels are copied into the output.

The resulting labels are AI supervision, not human gold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.qwen_risk_contract import (  # noqa: E402
    expected_semantic_payload,
    validate_semantic_payload,
)
from app.models.qwen_weak_supervision_contract import (  # noqa: E402
    QWEN_WEAK_MODEL_OUTPUT_CONTRACT,
    QWEN_WEAK_PROMPT_SHA256,
    QWEN_WEAK_PROMPT_VERSION,
    QWEN_WEAK_SUPERVISION_VERSION,
    QWEN_WEAK_SYSTEM_PROMPT,
)
from scripts.qwen_supervision_leakage_guard import (  # noqa: E402
    post_event_supervision_reasons,
)


CONTRACT_VERSION = "qwen-v15-fresh-deepseek-dev-v1"
LABEL_CLASSIFICATION = "AI_REVIEW_NOT_HUMAN_GOLD"
LABEL_PROVENANCE = "DEEPSEEK_ISOLATED_MULTIVIEW_ARBITRATION"
OUTPUT_NAME = "qwen_core_v15_fresh_deepseek_dev.jsonl"
MANIFEST_NAME = "manifest.json"
MINIMUM_ROWS = 150


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path, *, label: str) -> tuple[list[dict[str, Any]], bytes]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} missing: {path}")
    raw = path.read_bytes()
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{label}:{line_number}: row is not an object")
        rows.append(value)
    if not rows:
        raise ValueError(f"{label} is empty")
    return rows, raw


def atomic_write(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_with_sidecar(path: Path, raw: bytes) -> dict[str, Any]:
    digest = sha256_bytes(raw)
    atomic_write(path, raw)
    sidecar = path.with_name(path.name + ".sha256")
    sidecar_raw = f"{digest}  {path.name}\n".encode("ascii")
    atomic_write(sidecar, sidecar_raw)
    return {
        "filename": path.name,
        "rows": raw.count(b"\n"),
        "bytes": len(raw),
        "sha256": digest,
        "sidecar_sha256": sha256_bytes(sidecar_raw),
    }


def _unique_by_sample(rows: Iterable[dict[str, Any]], *, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for number, row in enumerate(rows, 1):
        sample_id = row.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id.strip() or sample_id != sample_id.strip():
            raise ValueError(f"{label}:{number}: invalid sample_id")
        if sample_id in result:
            raise ValueError(f"{label}:{number}: duplicate sample_id {sample_id}")
        result[sample_id] = row
    return result


def _blocked_sets(paths: Iterable[Path]) -> dict[str, set[str]]:
    blocked = {key: set() for key in ("sample_id", "entity_group", "event_chain_group", "hash")}
    hash_fields = (
        "content_sha256",
        "provider_text_sha256",
        "provider_text_sha256_v1",
        "source_text_sha256",
        "semantic_context_sha256",
    )
    for path in paths:
        rows, _ = read_jsonl(path, label=f"exposure index {path.name}")
        for row in rows:
            for key in ("sample_id", "entity_group", "event_chain_group"):
                value = str(row.get(key) or "").strip()
                if value:
                    blocked[key].add(value)
            for key in hash_fields:
                value = str(row.get(key) or "").strip()
                if value:
                    blocked["hash"].add(value)
    return blocked


def _completed_progress(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    completed: dict[str, dict[str, Any]] = {}
    for number, row in enumerate(rows, 1):
        if row.get("status") != "completed":
            continue
        sample_id = row.get("sample_id")
        result = row.get("result")
        if not isinstance(sample_id, str) or not isinstance(result, dict):
            raise ValueError(f"progress:{number}: malformed completed row")
        if sample_id in completed:
            raise ValueError(f"progress:{number}: duplicate completed sample {sample_id}")
        completed[sample_id] = result
    return completed


def freeze_dev(
    *,
    provider_input: Path,
    source_index: Path,
    progress: Path,
    exposure_indexes: list[Path],
    output_dir: Path,
    minimum_rows: int = MINIMUM_ROWS,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    provider_rows, provider_raw = read_jsonl(provider_input, label="provider input")
    index_rows, index_raw = read_jsonl(source_index, label="source index")
    progress_rows, progress_raw = read_jsonl(progress, label="progress")
    providers = _unique_by_sample(provider_rows, label="provider input")
    indexes = _unique_by_sample(index_rows, label="source index")
    completed = _completed_progress(progress_rows)
    if set(providers) != set(indexes):
        raise ValueError("provider input and source index membership differ")
    blocked = _blocked_sets(exposure_indexes)

    eligibility: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    exclusions: Counter[str] = Counter()
    for sample_id in sorted(completed):
        provider = providers.get(sample_id)
        index = indexes.get(sample_id)
        if provider is None or index is None:
            raise ValueError(f"completed sample is absent from frozen inputs: {sample_id}")
        content = provider.get("content")
        if not isinstance(content, dict) or set(provider) != {"sample_id", "content"}:
            raise ValueError(f"provider row has invalid shape: {sample_id}")
        content_sha = sha256_bytes(stable_json(content).encode("utf-8"))
        result = completed[sample_id]
        if result.get("sample_id") != sample_id or result.get("input_sha256") != content_sha:
            raise ValueError(f"progress binding mismatch: {sample_id}")
        reasons: list[str] = []
        if str(index.get("entity_group_quality") or "") == "EVENT_LOCAL_FALLBACK":
            reasons.append("EVENT_LOCAL_FALLBACK")
        for key in ("sample_id", "entity_group", "event_chain_group"):
            value = str(index.get(key) or "").strip()
            if not value:
                reasons.append(f"MISSING_{key.upper()}")
            elif value in blocked[key]:
                reasons.append(f"PRIOR_{key.upper()}_OVERLAP")
        candidate_hashes = {
            content_sha,
            *(str(index.get(key) or "").strip() for key in (
                "content_sha256",
                "provider_text_sha256",
                "provider_text_sha256_v1",
                "source_text_sha256",
                "semantic_context_sha256",
            )),
        }
        if any(value and value in blocked["hash"] for value in candidate_hashes):
            reasons.append("PRIOR_CONTENT_HASH_OVERLAP")
        if post_event_supervision_reasons(content):
            reasons.append("POST_EVENT_SUPERVISION_LEAKAGE")
        if reasons:
            exclusions.update(set(reasons))
            continue
        eligibility.append((sample_id, content, index, result))

    # Freeze membership before any target value is inspected.
    eligible_ids = [item[0] for item in eligibility]
    if len(eligible_ids) < minimum_rows:
        raise ValueError(f"fresh DEV has {len(eligible_ids)} rows; minimum is {minimum_rows}")

    output_rows: list[dict[str, Any]] = []
    materiality: Counter[str] = Counter()
    polarity: Counter[str] = Counter()
    agreement = Counter()
    provider_models: Counter[str] = Counter()
    for sample_id, content, index, result in eligibility:
        final = result.get("final")
        if not isinstance(final, dict):
            raise ValueError(f"completed result has no final target: {sample_id}")
        target = expected_semantic_payload(final.get("materiality"), final.get("polarity"))
        issues = validate_semantic_payload(target)
        if issues:
            raise ValueError(f"invalid target for {sample_id}: {','.join(issues)}")
        materiality[target["materiality"]] += 1
        polarity[target["polarity"]] += 1
        agreement["FIRST_PASS_PAIR_AGREED" if result.get("first_pass_pair_agreed") else "ARBITRATED"] += 1
        provider_models[str(result.get("model") or "UNKNOWN")] += 1
        metadata = {
            "sample_id": sample_id,
            "event_id": index.get("source_event_id") or index.get("event_id"),
            "entity_group": index["entity_group"],
            "event_chain_group": index["event_chain_group"],
            "entity_group_quality": index.get("entity_group_quality"),
            "split": "DEV",
            "target_contract": "core-v1",
            "model_output_contract": QWEN_WEAK_MODEL_OUTPUT_CONTRACT,
            "weak_supervision_version": QWEN_WEAK_SUPERVISION_VERSION,
            "prompt_version": QWEN_WEAK_PROMPT_VERSION,
            "prompt_sha256": QWEN_WEAK_PROMPT_SHA256,
            "semantic_target": target,
            "label_provenance": LABEL_PROVENANCE,
            "label_classification": LABEL_CLASSIFICATION,
            "human_gold_claimed": False,
            "qwen_prediction_included": False,
            "post_event_market_data_included": False,
            "evidence_state_used_as_model_target": False,
            "deepseek_result_sha256": sha256_bytes(stable_json(result).encode("utf-8")),
            "source_content_sha256": sha256_bytes(stable_json(content).encode("utf-8")),
        }
        output_rows.append({
            "messages": [
                {"role": "system", "content": QWEN_WEAK_SYSTEM_PROMPT},
                {"role": "user", "content": stable_json(content)},
                {"role": "assistant", "content": stable_json(target)},
            ],
            "metadata": metadata,
        })

    stage = output_dir.parent / f".{output_dir.name}.staging"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    try:
        output_raw = "".join(stable_json(row) + "\n" for row in output_rows).encode("utf-8")
        output_info = write_with_sidecar(stage / OUTPUT_NAME, output_raw)
        manifest = {
            "schema_version": 1,
            "contract_version": CONTRACT_VERSION,
            "purpose": "fresh_group_isolated_development_selection_only",
            "dataset_role": "DEV_SELECTION_ONLY",
            "label_classification": LABEL_CLASSIFICATION,
            "label_provenance": LABEL_PROVENANCE,
            "human_gold_claimed": False,
            "eligibility_decided_before_target_copy": True,
            "minimum_rows": minimum_rows,
            "row_count": len(output_rows),
            "eligible_sample_ids_sha256": sha256_bytes(stable_json(eligible_ids).encode("utf-8")),
            "exclusions": dict(sorted(exclusions.items())),
            "distributions": {
                "materiality": dict(sorted(materiality.items())),
                "polarity": dict(sorted(polarity.items())),
                "review_resolution": dict(sorted(agreement.items())),
                "provider_model": dict(sorted(provider_models.items())),
            },
            "inputs": {
                "provider_input": {"path": str(provider_input), "sha256": sha256_bytes(provider_raw)},
                "source_index": {"path": str(source_index), "sha256": sha256_bytes(index_raw)},
                "progress": {"path": str(progress), "sha256": sha256_bytes(progress_raw)},
                "exposure_indexes": [
                    {"path": str(path), "sha256": sha256_file(path)} for path in exposure_indexes
                ],
            },
            "isolation": {
                "qwen_predictions_read": False,
                "market_results_read": False,
                "strict_test_provider_payload_read": False,
                "prior_sample_entity_chain_and_hash_overlap": 0,
                "event_local_fallback_included": False,
            },
            "prompt": {
                "version": QWEN_WEAK_PROMPT_VERSION,
                "sha256": QWEN_WEAK_PROMPT_SHA256,
                "model_output_contract": QWEN_WEAK_MODEL_OUTPUT_CONTRACT,
            },
            "output": output_info,
        }
        manifest_raw = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        write_with_sidecar(stage / MANIFEST_NAME, manifest_raw)
        os.replace(stage, output_dir)
    except Exception:
        if stage.exists():
            shutil.rmtree(stage)
        raise
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider-input", type=Path, required=True)
    parser.add_argument("--source-index", type=Path, required=True)
    parser.add_argument("--progress", type=Path, required=True)
    parser.add_argument("--exposure-index", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--minimum-rows", type=int, default=MINIMUM_ROWS)
    args = parser.parse_args(argv)
    manifest = freeze_dev(
        provider_input=args.provider_input.resolve(),
        source_index=args.source_index.resolve(),
        progress=args.progress.resolve(),
        exposure_indexes=[path.resolve() for path in args.exposure_index],
        output_dir=args.output_dir.resolve(),
        minimum_rows=args.minimum_rows,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
