"""Deterministically freeze returned human-only gold annotations.

This is an artifact-only gate.  It does not import reviews into production,
change canonical events, retrain a model or promote a model.  The split keeps
the non-holdout core chronological and reserves one source family, selected
only from pre-label source metadata, for HUMAN_BLIND.  Issuer, event-chain,
exact-text and near-duplicate overlap are required to be zero.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

from app.models.risk_label_contract import LABELS, validate_annotation
from app.services.adjudication import AdjudicationService, normalize_source_family
from app.services.human_gold_review import OFFLINE_GOLD_CONTRACT_VERSION


FREEZE_CONTRACT_VERSION = "human-gold-chronological-source-holdout-freeze-v2"
DEFAULT_SPLIT_SIZES = {"TRAIN": 420, "VALIDATION": 120, "HUMAN_BLIND": 180}
DEFAULT_LABEL_MINIMUMS = {
    "TRAIN": {"RISK_REVIEW": 120, "NON_TARGET": 120, "ABSTAIN": 40},
    "VALIDATION": {"RISK_REVIEW": 30, "NON_TARGET": 30, "ABSTAIN": 10},
    "HUMAN_BLIND": {"RISK_REVIEW": 50, "NON_TARGET": 50, "ABSTAIN": 20},
}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _as_of(row: dict[str, Any]) -> datetime:
    content = row.get("content")
    if not isinstance(content, dict):
        raise ValueError("annotation content must be an object")
    raw = str(content.get("as_of") or "").strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("annotation content.as_of must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("annotation content.as_of must include a timezone")
    return parsed.astimezone(timezone.utc)


def _content_boundaries(row: dict[str, Any]) -> list[str]:
    content = row.get("content") or {}
    issues: list[str] = []
    if content.get("contract_version") != "human-blind-v3.1":
        issues.append("content contract is not human-blind-v3.1")
    if content.get("post_event_market_data_included") is not False:
        issues.append("post-event market data is present")
    if content.get("model_output_included") is not False:
        issues.append("model output is present")
    if content.get("target_label_hidden") is not True:
        issues.append("target label was not hidden during review")
    if content.get("source_identity_hidden") is not True:
        issues.append("source identity was not hidden during review")
    return issues


def _select_source_holdout_family(
    rows: list[dict[str, Any]],
    *,
    blind_size: int,
    requested_family: str | None,
    minimum_rows: int,
) -> tuple[str | None, dict[str, int], list[str]]:
    """Choose a blind-only family without consulting any human label.

    A purely chronological split can accidentally place the same dominant
    provider in all three partitions.  That makes a source-family holdout
    impossible even when the batch deliberately contains smaller independent
    providers.  Selection therefore uses source identity and row count only,
    before labels are inspected.  The largest eligible non-dominant family is
    deterministic; callers may pin the already declared family explicitly.
    """

    counts = Counter(
        normalize_source_family(row.get("source_id"))
        for row in rows
        if normalize_source_family(row.get("source_id"))
    )
    normalized_requested = normalize_source_family(requested_family)
    if requested_family and not normalized_requested:
        return None, dict(sorted(counts.items())), ["requested source holdout family is invalid"]
    if normalized_requested:
        candidates = [normalized_requested]
    else:
        candidates = [
            family
            for family, count in counts.items()
            if int(minimum_rows) <= count <= int(blind_size) and count < len(rows)
        ]
        candidates.sort(key=lambda family: (-counts[family], family))
    if not candidates:
        return None, dict(sorted(counts.items())), [
            "no source family is eligible for a metadata-only HUMAN_BLIND holdout"
        ]
    selected = candidates[0]
    selected_count = int(counts.get(selected, 0))
    issues: list[str] = []
    if selected_count < int(minimum_rows):
        issues.append(
            f"source holdout family {selected} has {selected_count} rows; minimum is {minimum_rows}"
        )
    if selected_count > int(blind_size):
        issues.append(
            f"source holdout family {selected} has {selected_count} rows; HUMAN_BLIND has {blind_size}"
        )
    if selected_count == len(rows):
        issues.append("source holdout family cannot contain the entire dataset")
    return selected, dict(sorted(counts.items())), issues


def _assign_splits(
    rows: list[dict[str, Any]],
    sizes: dict[str, int],
    *,
    holdout_source_family: str | None,
    minimum_holdout_family_rows: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], list[str]]:
    blind_size = int(sizes["HUMAN_BLIND"])
    selected, family_counts, issues = _select_source_holdout_family(
        rows,
        blind_size=blind_size,
        requested_family=holdout_source_family,
        minimum_rows=minimum_holdout_family_rows,
    )
    if selected is None or issues:
        return {split: [] for split in sizes}, {
            "selected_source_family": selected,
            "source_family_counts": family_counts,
            "selection_basis": "SOURCE_METADATA_ONLY_PRE_LABELS",
            "minimum_rows": int(minimum_holdout_family_rows),
        }, issues

    holdout_rows = [
        row for row in rows if normalize_source_family(row.get("source_id")) == selected
    ]
    chronological_core = [
        row for row in rows if normalize_source_family(row.get("source_id")) != selected
    ]
    train_size = int(sizes["TRAIN"])
    validation_size = int(sizes["VALIDATION"])
    blind_core_size = blind_size - len(holdout_rows)
    expected_core_size = train_size + validation_size + blind_core_size
    if len(chronological_core) != expected_core_size or blind_core_size < 0:
        issues.append("source holdout allocation does not match requested split sizes")
        return {split: [] for split in sizes}, {
            "selected_source_family": selected,
            "selected_source_family_rows": len(holdout_rows),
            "source_family_counts": family_counts,
            "selection_basis": "SOURCE_METADATA_ONLY_PRE_LABELS",
            "minimum_rows": int(minimum_holdout_family_rows),
        }, issues

    train_rows = chronological_core[:train_size]
    validation_rows = chronological_core[train_size : train_size + validation_size]
    blind_core_rows = chronological_core[train_size + validation_size :]

    def bounds(selected: list[dict[str, Any]]) -> dict[str, str | None]:
        return {
            "min_as_of": min(
                (row["_as_of"].isoformat() for row in selected), default=None
            ),
            "max_as_of": max(
                (row["_as_of"].isoformat() for row in selected), default=None
            ),
        }

    blind_rows = sorted(
        [*holdout_rows, *blind_core_rows],
        key=lambda row: (row.get("_as_of"), str(row.get("sample_id") or "")),
    )
    split_rows = {
        "TRAIN": train_rows,
        "VALIDATION": validation_rows,
        "HUMAN_BLIND": blind_rows,
    }
    policy = {
        "selected_source_family": selected,
        "selected_source_family_rows": len(holdout_rows),
        "blind_chronological_core_rows": len(blind_core_rows),
        "source_family_counts": family_counts,
        "selection_basis": "SOURCE_METADATA_ONLY_PRE_LABELS",
        "minimum_rows": int(minimum_holdout_family_rows),
        "non_holdout_core_is_chronological": True,
        "chronological_core_bounds": {
            "TRAIN": bounds(train_rows),
            "VALIDATION": bounds(validation_rows),
            "HUMAN_BLIND": bounds(blind_core_rows),
        },
    }
    return split_rows, policy, issues


def assess_freeze_readiness(
    annotations: Iterable[dict[str, Any]],
    *,
    split_sizes: dict[str, int] | None = None,
    label_minimums: dict[str, dict[str, int]] | None = None,
    minimum_source_families: int = 4,
    holdout_source_family: str | None = None,
    minimum_holdout_family_rows: int | None = None,
) -> dict[str, Any]:
    sizes = dict(split_sizes or DEFAULT_SPLIT_SIZES)
    minimums = {
        split: dict(values)
        for split, values in (label_minimums or DEFAULT_LABEL_MINIMUMS).items()
    }
    if tuple(sizes) != ("TRAIN", "VALIDATION", "HUMAN_BLIND"):
        raise ValueError("split_sizes must be ordered TRAIN, VALIDATION, HUMAN_BLIND")
    if any(int(value) < 1 for value in sizes.values()):
        raise ValueError("every split size must be positive")
    holdout_minimum = (
        int(minimum_holdout_family_rows)
        if minimum_holdout_family_rows is not None
        else min(5, max(1, int(sizes["HUMAN_BLIND"]) // 10))
    )
    if holdout_minimum < 1:
        raise ValueError("minimum_holdout_family_rows must be positive")
    rows = [dict(row) for row in annotations]
    issues: list[str] = []
    seen_samples: set[str] = set()
    seen_events: set[str] = set()
    seen_entities: set[str] = set()
    seen_chains: set[str] = set()
    seen_text: set[str] = set()
    signatures: list[tuple[frozenset[str], frozenset[str]]] = []
    for index, row in enumerate(rows):
        prefix = f"annotations[{index}]"
        annotation_issues = validate_annotation(row)
        issues.extend(f"{prefix}:{issue}" for issue in annotation_issues)
        issues.extend(f"{prefix}:{issue}" for issue in _content_boundaries(row))
        for field, seen in (
            ("sample_id", seen_samples),
            ("event_id", seen_events),
            ("entity_group", seen_entities),
            ("event_chain_group", seen_chains),
            ("text_sha256", seen_text),
        ):
            value = str(row.get(field) or "")
            if value in seen:
                issues.append(f"{prefix}:duplicate {field}")
            seen.add(value)
        signature = AdjudicationService._near_duplicate_signature(row)
        if any(AdjudicationService._is_near_duplicate(signature, prior) for prior in signatures):
            issues.append(f"{prefix}:near-duplicate content")
        signatures.append(signature)
        try:
            row["_as_of"] = _as_of(row)
        except ValueError as exc:
            issues.append(f"{prefix}:{exc}")

    expected_count = sum(int(value) for value in sizes.values())
    if len(rows) != expected_count:
        issues.append(f"annotation count must equal {expected_count}, got {len(rows)}")
    rows.sort(
        key=lambda row: (
            row.get("_as_of") or datetime.max.replace(tzinfo=timezone.utc),
            str(row.get("sample_id") or ""),
        )
    )
    split_rows, source_holdout_policy, split_issues = _assign_splits(
        rows,
        sizes,
        holdout_source_family=holdout_source_family,
        minimum_holdout_family_rows=holdout_minimum,
    )
    issues.extend(split_issues)
    assigned: list[dict[str, Any]] = []
    for split in sizes:
        assigned.extend(
            {
                **{key: value for key, value in row.items() if key != "_as_of"},
                "split": split,
            }
            for row in split_rows[split]
        )

    label_counts = {
        split: dict(sorted(Counter(row.get("label") for row in selected).items()))
        for split, selected in split_rows.items()
    }
    label_deficits: dict[str, dict[str, int]] = {}
    for split, required in minimums.items():
        actual = label_counts.get(split, {})
        label_deficits[split] = {
            label: max(0, int(count) - int(actual.get(label, 0)))
            for label, count in required.items()
        }
        for label in set(actual) - LABELS:
            issues.append(f"{split}:unsupported label {label}")

    source_families = {
        split: {
            normalize_source_family(row.get("source_id"))
            for row in selected
            if normalize_source_family(row.get("source_id"))
        }
        for split, selected in split_rows.items()
    }
    all_source_families = set().union(*source_families.values()) if source_families else set()
    held_out = source_families.get("HUMAN_BLIND", set()) - (
        source_families.get("TRAIN", set()) | source_families.get("VALIDATION", set())
    )
    if len(all_source_families) < int(minimum_source_families):
        issues.append(
            f"source families must be at least {minimum_source_families}, got {len(all_source_families)}"
        )
    if source_holdout_policy.get("selected_source_family") not in held_out:
        issues.append("HUMAN_BLIND source-family holdout policy was not satisfied")
    for split, deficits in label_deficits.items():
        for label, deficit in deficits.items():
            if deficit:
                issues.append(f"{split}:{label} deficit={deficit}")

    temporal = {
        split: {
            "min_as_of": min((row.get("_as_of") for row in selected), default=None),
            "max_as_of": max((row.get("_as_of") for row in selected), default=None),
        }
        for split, selected in split_rows.items()
    }
    temporal_serialized = {
        split: {
            key: value.isoformat() if value is not None else None
            for key, value in bounds.items()
        }
        for split, bounds in temporal.items()
    }
    ready = not issues
    freeze_payload = {
        "contract_version": FREEZE_CONTRACT_VERSION,
        "source_contract_version": OFFLINE_GOLD_CONTRACT_VERSION,
        "rows": assigned,
    }
    dataset_sha256 = sha256_json(freeze_payload) if ready else None
    return {
        "schema_version": 1,
        "contract_version": FREEZE_CONTRACT_VERSION,
        "status": "READY_TO_FREEZE" if ready else "NOT_READY_TO_FREEZE",
        "issues": issues,
        "annotation_count": len(rows),
        "expected_annotation_count": expected_count,
        "split_sizes": sizes,
        "label_counts": label_counts,
        "label_minimums": minimums,
        "label_deficits": label_deficits,
        "source_families": {split: sorted(values) for split, values in source_families.items()},
        "source_holdout_policy": source_holdout_policy,
        "all_source_family_count": len(all_source_families),
        "fully_held_out_blind_source_families": sorted(held_out),
        "temporal_bounds": temporal_serialized,
        "issuer_overlap_count": len(rows) - len(seen_entities),
        "event_chain_overlap_count": len(rows) - len(seen_chains),
        "exact_text_overlap_count": len(rows) - len(seen_text),
        "near_duplicate_overlap_count": sum("near-duplicate content" in issue for issue in issues),
        "dataset_sha256": dataset_sha256,
        "freeze_id": f"human-gold-{dataset_sha256[:12]}" if dataset_sha256 else None,
        "rows": assigned if ready else [],
        "human_only": True,
        "ai_labels_included": False,
        "post_event_market_data_included": False,
        "canonical_event_state_changed": False,
        "production_model_changed": False,
        "no_trading": True,
    }
