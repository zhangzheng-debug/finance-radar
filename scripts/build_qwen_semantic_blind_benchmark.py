#!/usr/bin/env python3
"""Freeze reproducible, model-blind semantic arbitration inputs.

The benchmark is selected only from strict A/B human submissions.  Samples
already present in the declared v3 train, validation, or owner-holdout files
are excluded.  Every remaining sample is exported without either reviewer's
labels.  The A/B labels are kept in a separate hash-sealed owner mapping for a
later merge.  Nothing in this script reads or uses model predictions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.qwen_risk_contract import (  # noqa: E402
    QWEN_RISK_CONTRACT_VERSION,
    normalize_qwen_risk_content,
)
from app.services.human_gold_review import (  # noqa: E402
    OFFLINE_GOLD_CONTRACT_VERSION,
    stable_json,
    validate_submission,
)
CONTRACT_VERSION = "qwen-semantic-arbitration-input-freeze-v1"
ARBITRATION_NAME = "arbitration_inputs.jsonl"
SEALED_LABELS_NAME = "sealed_reviewer_labels.jsonl"
MANIFEST_NAME = "manifest.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_object(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON input must be an object: {path}")
    return value, raw


def _zip_object(archive: zipfile.ZipFile, suffix: str) -> dict[str, Any]:
    matches = [name for name in archive.namelist() if name.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(
            f"owner package must contain exactly one {suffix}; found {len(matches)}"
        )
    value = json.loads(archive.read(matches[0]).decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"owner package {suffix} must be an object")
    return value


def _owner_inputs(path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    with zipfile.ZipFile(path) as archive:
        owner = _zip_object(archive, "owner_manifest.json")
        assignment_a = _zip_object(archive, "assignment_A.json")
        assignment_b = _zip_object(archive, "assignment_B.json")

    if owner.get("contract_version") != OFFLINE_GOLD_CONTRACT_VERSION:
        raise ValueError("owner manifest has an unsupported contract_version")
    declared = str(owner.get("manifest_sha256") or "")
    unsigned = {key: value for key, value in owner.items() if key != "manifest_sha256"}
    if declared != _sha256_bytes(stable_json(unsigned).encode("utf-8")):
        raise ValueError("owner manifest hash is invalid")
    embedded = owner.get("assignments")
    if not isinstance(embedded, dict):
        raise ValueError("owner manifest assignments must be an object")
    for slot, assignment in (("A", assignment_a), ("B", assignment_b)):
        if stable_json(embedded.get(slot)) != stable_json(assignment):
            raise ValueError(f"assignment_{slot} does not match owner manifest")
    if assignment_a.get("reviewer_token") == assignment_b.get("reviewer_token"):
        raise ValueError("A/B reviewer identities must be distinct")
    return owner, assignment_a, assignment_b


def _sample_ids_from_v3(path: Path, expected_split: str) -> tuple[set[str], str]:
    raw = path.read_bytes()
    sample_ids: set[str] = set()
    for line_number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("metadata"), dict):
            raise ValueError(f"invalid v3 row in {path}:{line_number}")
        metadata = row["metadata"]
        sample_id = str(metadata.get("sample_id") or "").strip()
        if not sample_id:
            raise ValueError(f"missing sample_id in {path}:{line_number}")
        if sample_id in sample_ids:
            raise ValueError(f"duplicate sample_id in {path}: {sample_id}")
        if str(metadata.get("split") or "").upper() != expected_split:
            raise ValueError(f"unexpected split in {path}:{line_number}")
        sample_ids.add(sample_id)
    return sample_ids, _sha256_bytes(raw)


def _validated_reviews(
    owner: dict[str, Any],
    assignment_a: dict[str, Any],
    assignment_b: dict[str, Any],
    submission_a: dict[str, Any],
    submission_b: dict[str, Any],
) -> dict[str, dict[str, dict[str, Any]]]:
    reports = {
        "A": validate_submission(assignment_a, submission_a),
        "B": validate_submission(assignment_b, submission_b),
    }
    if not all(report["valid"] for report in reports.values()):
        raise ValueError(
            "strict A/B submission validation failed: "
            + stable_json({slot: report["issues"] for slot, report in reports.items()})
        )

    samples = owner.get("samples")
    token_maps = owner.get("token_maps")
    if not isinstance(samples, list) or not isinstance(token_maps, dict):
        raise ValueError("owner manifest samples/token_maps are invalid")
    sample_ids = [str(row.get("sample_id") or "") for row in samples if isinstance(row, dict)]
    if len(sample_ids) != len(samples) or not all(sample_ids) or len(set(sample_ids)) != len(sample_ids):
        raise ValueError("owner sample_id values must be unique and nonblank")

    reviews: dict[str, dict[str, dict[str, Any]]] = {}
    for slot, submission in (("A", submission_a), ("B", submission_b)):
        token_map = token_maps.get(slot)
        if not isinstance(token_map, dict) or set(token_map.values()) != set(sample_ids):
            raise ValueError(f"owner token map {slot} does not cover the owner samples exactly")
        mapped: dict[str, dict[str, Any]] = {}
        for row in submission["results"]:
            sample_id = token_map.get(row["sample_token"])
            if not sample_id or sample_id in mapped:
                raise ValueError(f"submission {slot} has an invalid token mapping")
            mapped[sample_id] = row
        if set(mapped) != set(sample_ids):
            raise ValueError(f"submission {slot} does not cover the owner samples exactly")
        reviews[slot] = mapped
    return reviews


def _validate_owner_sample(sample: dict[str, Any]) -> None:
    if any(key in sample for key in ("label", "model_prediction", "market_outcome")):
        raise ValueError(f"owner sample {sample.get('sample_id')} contains prohibited output")
    content = sample.get("content")
    if not isinstance(content, dict):
        raise ValueError(f"owner sample {sample.get('sample_id')} has no content object")
    flags = {
        "target_label_hidden": True,
        "source_identity_hidden": True,
        "post_event_market_data_included": False,
        "model_output_included": False,
    }
    for field, expected in flags.items():
        if content.get(field) is not expected:
            raise ValueError(
                f"owner sample {sample.get('sample_id')} violates {field}={expected}"
            )


def _arbitration_input(sample: dict[str, Any]) -> dict[str, Any]:
    review_input = normalize_qwen_risk_content(sample["content"])
    return {
        "sample_id": sample["sample_id"],
        "content": review_input,
    }


def _sealed_label_row(
    sample_id: str,
    first: dict[str, Any],
    second: dict[str, Any],
) -> dict[str, Any]:
    return {
        "sample_id": sample_id,
        "reviewer_labels": {
            "A": {
                "materiality": first["materiality"],
                "polarity": first["polarity"],
            },
            "B": {
                "materiality": second["materiality"],
                "polarity": second["polarity"],
            },
        },
    }


def _resolve_commit(value: str | None) -> str:
    commit = str(value or "").strip().lower()
    if not commit:
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip().lower()
    if not _COMMIT.fullmatch(commit):
        raise ValueError("candidate_commit must be a 40-64 character lowercase hex hash")
    return commit


def build(
    *,
    owner_package: Path,
    review_a: Path,
    review_b: Path,
    v3_train: Path,
    v3_validation: Path,
    v3_owner_holdout: Path,
    adapter: Path,
    output_dir: Path,
    candidate_commit: str | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")

    inputs = [owner_package, review_a, review_b, v3_train, v3_validation, v3_owner_holdout, adapter]
    missing = [str(path) for path in inputs if not path.is_file()]
    if missing:
        raise ValueError("required input is missing: " + ", ".join(missing))
    commit = _resolve_commit(candidate_commit)
    adapter_raw = adapter.read_bytes()
    adapter_sha256 = _sha256_bytes(adapter_raw)

    owner, assignment_a, assignment_b = _owner_inputs(owner_package)
    submission_a, review_a_raw = _read_object(review_a)
    submission_b, review_b_raw = _read_object(review_b)
    reviews = _validated_reviews(
        owner, assignment_a, assignment_b, submission_a, submission_b
    )

    split_specs = (
        ("train", v3_train, "TRAIN"),
        ("validation", v3_validation, "VALIDATION"),
        ("owner_holdout", v3_owner_holdout, "OWNER_HOLDOUT"),
    )
    used_by_split: dict[str, set[str]] = {}
    split_hashes: dict[str, str] = {}
    used: set[str] = set()
    for name, path, expected_split in split_specs:
        sample_ids, digest = _sample_ids_from_v3(path, expected_split)
        overlap = used & sample_ids
        if overlap:
            raise ValueError(f"v3 splits overlap at sample_id {sorted(overlap)[0]}")
        used_by_split[name] = sample_ids
        split_hashes[name] = digest
        used.update(sample_ids)

    owner_samples = owner["samples"]
    owner_ids = {str(row["sample_id"]) for row in owner_samples}
    unknown_used = used - owner_ids
    if unknown_used:
        raise ValueError(
            "v3 split contains sample_id outside owner package: " + sorted(unknown_used)[0]
        )
    prefreeze = sorted(
        (row for row in owner_samples if row["sample_id"] not in used),
        key=lambda row: str(row["sample_id"]),
    )

    arbitration_rows: list[dict[str, Any]] = []
    sealed_label_rows: list[dict[str, Any]] = []
    consensus_ids: list[str] = []
    disagreement_ids: list[str] = []
    disagreement_axes: Counter[str] = Counter()
    for sample in prefreeze:
        _validate_owner_sample(sample)
        sample_id = str(sample["sample_id"])
        first, second = reviews["A"][sample_id], reviews["B"][sample_id]
        pair_a = (str(first["materiality"]), str(first["polarity"]))
        pair_b = (str(second["materiality"]), str(second["polarity"]))
        arbitration_rows.append(_arbitration_input(sample))
        sealed_label_rows.append(_sealed_label_row(sample_id, first, second))
        if pair_a == pair_b:
            consensus_ids.append(sample_id)
            continue
        disagreement_ids.append(sample_id)
        if pair_a[0] != pair_b[0]:
            disagreement_axes["materiality"] += 1
        if pair_a[1] != pair_b[1]:
            disagreement_axes["polarity"] += 1

    if not arbitration_rows:
        raise ValueError("no unused owner samples remain for semantic arbitration")
    arbitration_bytes = b"".join(
        (stable_json(row) + "\n").encode("utf-8") for row in arbitration_rows
    )
    sealed_labels_bytes = b"".join(
        (stable_json(row) + "\n").encode("utf-8") for row in sealed_label_rows
    )
    arbitration_sha256 = _sha256_bytes(arbitration_bytes)
    sealed_labels_sha256 = _sha256_bytes(sealed_labels_bytes)
    arbitration_sidecar = (
        f"{arbitration_sha256}  {ARBITRATION_NAME}\n".encode("ascii")
    )
    sealed_labels_sidecar = (
        f"{sealed_labels_sha256}  {SEALED_LABELS_NAME}\n".encode("ascii")
    )
    prefreeze_ids = [str(row["sample_id"]) for row in prefreeze]

    owner_raw = owner_package.read_bytes()
    freeze_basis = {
        "contract_version": CONTRACT_VERSION,
        "candidate_commit": commit,
        "adapter_sha256": adapter_sha256,
        "owner_manifest_sha256": owner["manifest_sha256"],
        "review_a_sha256": _sha256_bytes(review_a_raw),
        "review_b_sha256": _sha256_bytes(review_b_raw),
        "v3_split_sha256": split_hashes,
        "prefreeze_sample_ids": prefreeze_ids,
        "arbitration_inputs_sha256": arbitration_sha256,
        "sealed_reviewer_labels_sha256": sealed_labels_sha256,
    }
    freeze_id = "qwen-semantic-blind-" + _sha256_bytes(
        stable_json(freeze_basis).encode("utf-8")
    )[:20]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contract_version": CONTRACT_VERSION,
        "freeze_id": freeze_id,
        "candidate_commit": commit,
        "adapter": {
            "filename": adapter.name,
            "sha256": adapter_sha256,
            "size_bytes": len(adapter_raw),
        },
        "qwen_contract_version": QWEN_RISK_CONTRACT_VERSION,
        "inputs": {
            "owner_package": {"filename": owner_package.name, "sha256": _sha256_bytes(owner_raw)},
            "owner_manifest_sha256": owner["manifest_sha256"],
            "review_a": {"filename": review_a.name, "sha256": _sha256_bytes(review_a_raw)},
            "review_b": {"filename": review_b.name, "sha256": _sha256_bytes(review_b_raw)},
            "v3_split_sha256": split_hashes,
        },
        "owner_sample_count": len(owner_samples),
        "excluded_used_sample_count": len(used),
        "excluded_by_split": {key: len(value) for key, value in used_by_split.items()},
        "excluded_sample_ids_sha256": _sha256_bytes(stable_json(sorted(used)).encode("utf-8")),
        "prefreeze_pool_count": len(prefreeze),
        "prefreeze_sample_ids_sha256": _sha256_bytes(stable_json(prefreeze_ids).encode("utf-8")),
        "arbitration_input_count": len(arbitration_rows),
        "arbitration_inputs": {
            "filename": ARBITRATION_NAME,
            "sha256": arbitration_sha256,
            "sidecar": ARBITRATION_NAME + ".sha256",
            "sidecar_sha256": _sha256_bytes(arbitration_sidecar),
            "fields": ["sample_id", "content"],
            "reviewer_labels_included": False,
            "qwen_predictions_included": False,
        },
        "sealed_reviewer_labels": {
            "filename": SEALED_LABELS_NAME,
            "sha256": sealed_labels_sha256,
            "sidecar": SEALED_LABELS_NAME + ".sha256",
            "sidecar_sha256": _sha256_bytes(sealed_labels_sidecar),
            "row_count": len(sealed_label_rows),
            "content_included": False,
            "rationales_included": False,
            "reviewer_identities_included": False,
            "purpose": "OWNER_ONLY_LATER_MERGE",
        },
        "consensus_audit": {
            "row_count": len(consensus_ids),
            "sample_ids_sha256": _sha256_bytes(
                stable_json(sorted(consensus_ids)).encode("utf-8")
            ),
            "used_as_primary_benchmark": False,
            "used_as_human_gold": False,
        },
        "disagreements": {
            "row_count": len(disagreement_ids),
            "sample_ids_sha256": _sha256_bytes(
                stable_json(sorted(disagreement_ids)).encode("utf-8")
            ),
            "axis_counts": dict(sorted(disagreement_axes.items())),
            "model_adjudication_used": False,
            "labels_exposed_in_arbitration_inputs": False,
        },
        "benchmark_ready": False,
        "benchmark_block_reason": "INSUFFICIENT_DUAL_HUMAN_SEMANTIC_CONSENSUS",
        "minimum_consensus_rows_required": 120,
        "human_review_status": "DUAL_REVIEWED_PENDING_INDEPENDENT_ARBITRATION",
        "human_gold_claimed": False,
        "full_arbitration_gold": False,
        "model_predictions_read": False,
        "model_predictions_used_for_selection_or_adjudication": False,
        "post_event_market_data_included": False,
        "canonical_event_state_changed": False,
        "production_model_changed": False,
        "production_ledger_changed": False,
        "artifact_permission_contract": (
            "POSIX_MODE_0600"
            if os.name != "nt"
            else "WINDOWS_CALLER_ACL_REQUIRED_HASH_SEALED_NOT_ENCRYPTED"
        ),
        "no_trading": True,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.with_name(f".{output_dir.name}.{uuid.uuid4().hex}.tmp")
    staging.mkdir(exist_ok=False)
    try:
        (staging / ARBITRATION_NAME).write_bytes(arbitration_bytes)
        (staging / (ARBITRATION_NAME + ".sha256")).write_bytes(
            arbitration_sidecar
        )
        (staging / SEALED_LABELS_NAME).write_bytes(sealed_labels_bytes)
        (staging / (SEALED_LABELS_NAME + ".sha256")).write_bytes(
            sealed_labels_sidecar
        )
        (staging / MANIFEST_NAME).write_bytes(manifest_bytes)
        if os.name != "nt":
            for artifact in staging.iterdir():
                artifact.chmod(0o600)
        if output_dir.exists():
            raise FileExistsError(f"output directory appeared during write: {output_dir}")
        staging.rename(output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-package", type=Path, required=True)
    parser.add_argument("--review-a", type=Path, required=True)
    parser.add_argument("--review-b", type=Path, required=True)
    parser.add_argument("--v3-train", type=Path, required=True)
    parser.add_argument("--v3-validation", type=Path, required=True)
    parser.add_argument("--v3-owner-holdout", type=Path, required=True)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--candidate-commit")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = build(
        owner_package=args.owner_package.resolve(),
        review_a=args.review_a.resolve(),
        review_b=args.review_b.resolve(),
        v3_train=args.v3_train.resolve(),
        v3_validation=args.v3_validation.resolve(),
        v3_owner_holdout=args.v3_owner_holdout.resolve(),
        adapter=args.adapter.resolve(),
        output_dir=args.output_dir,
        candidate_commit=args.candidate_commit,
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
