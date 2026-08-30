#!/usr/bin/env python3
"""Build a DEV SFT overlay from independent AI reviews only.

The clean DEV SFT supplies membership, prompt bytes, source identity and source
text.  Its existing assistant target and ``metadata.semantic_target`` are
deliberately ignored.  Reviewer A and B decide the target pair when they agree;
reviewer C must cover exactly the A/B pair-disagreement set and is the only
decision source for those rows.  Frozen five-field review rows are validated
in place and represented in the output only by their stable hashes.

This command accepts no Qwen prediction or market-result input.  It publishes a
new directory atomically and refuses to overwrite any existing directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.qwen_risk_contract import (  # noqa: E402
    expected_semantic_payload,
    validate_semantic_payload,
)
from app.models.risk_label_contract import MATERIALITY, POLARITIES  # noqa: E402
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


CONTRACT_VERSION = "qwen-core-dev-independent-ai-review-overlay-v1"
SOURCE_CONTENT_EQUIVALENCE_CONTRACT_VERSION = (
    "source-content-aware-iso8601-utc-v1"
)
TARGET_CONTRACT = "core-v1"
MODEL_OUTPUT_CONTRACT = QWEN_WEAK_MODEL_OUTPUT_CONTRACT
LABEL_PROVENANCE = "INDEPENDENT_AI_REVIEW_CONSENSUS"
LABEL_CLASSIFICATION = "AI_REVIEW_NOT_HUMAN_GOLD"
EXPECTED_ROW_COUNT = 138
OUTPUT_NAME = "qwen_core_v11_dev_ai_review_overlay.jsonl"
MANIFEST_NAME = "manifest.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AWARE_ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$"
)
SOURCE_CONTENT_TIME_KEYS = frozenset({"as_of", "published_at"})
REVIEW_ROW_FIELDS = frozenset(
    {"sample_id", "materiality", "polarity", "reason", "review_class"}
)
REVIEW_CLASS_BY_SLOT = {
    "A": "INDEPENDENT_AI_REVIEW_NOT_HUMAN_GOLD",
    "B": "INDEPENDENT_AI_REVIEW_NOT_HUMAN_GOLD",
    "C": "INDEPENDENT_AI_ARBITRATION_NOT_HUMAN_GOLD",
}

SAFE_BASE_METADATA_FIELDS = (
    "event_id",
    "entity_group",
    "canonical_issuer_key",
    "event_chain_group",
    "benchmark_stratum",
)
REQUIRED_FALSE_BOUNDARIES = (
    "human_gold_claimed",
    "qwen_prediction_included",
    "post_event_market_data_included",
    "evidence_state_used_as_model_target",
)
PROHIBITED_SOURCE_KEYS = frozenset(
    {
        "adverse_strength",
        "assistant",
        "candidate_prediction",
        "expected",
        "expected_output",
        "human_label",
        "label",
        "labels",
        "market_outcome",
        "market_results",
        "market_return",
        "materiality",
        "model_output",
        "model_prediction",
        "old_label",
        "polarity",
        "post_event_price",
        "price_audit",
        "qwen_output",
        "qwen_prediction",
        "qwen_predictions",
        "reason_codes",
        "reviewer_label",
        "reviewer_labels",
        "semantic_priority",
        "semantic_target",
        "target",
        "target_label",
        "weak_rule",
        "weak_truth",
    }
)
PROHIBITED_SOURCE_TEXT = (
    re.compile(
        r"\b(?:MATERIAL_ADVERSE|NOT_MATERIAL_ADVERSE|PRIORITY_REVIEW|"
        r"UNDECIDABLE)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:qwen|model|candidate|reviewer)[ _-]prediction\s*[:=]",
        re.I,
    ),
    re.compile(r"\bmarket[_ -]outcome\s*[:=]", re.I),
    re.compile(r"\bret_(?:1d|3d|5d|10d|20d|21d)\s*(?:<=|>=|=)\s*-?\d", re.I),
    re.compile(
        r"\b(?:one|three|five|ten|twenty[_ -]?one|1|3|5|10|20|21)"
        r"[_ -]?day[_ -]?crash\s+candidate\b",
        re.I,
    ),
    re.compile(r"\bvolume[_ -]?crash\s+candidate\b", re.I),
    re.compile(r"\bvolume_ratio\s*=", re.I),
)
PROHIBITED_REVIEW_TEXT = (
    re.compile(r"\bqwen\b", re.I),
    re.compile(r"\b(?:model|candidate|reviewer) prediction\b", re.I),
    re.compile(r"\bmarket outcome\b", re.I),
    re.compile(r"\b(?:post[- ]event|subsequent) (?:price|return|market)\b", re.I),
    re.compile(r"\bprice (?:reaction|return|moved?|moves?)\b", re.I),
    re.compile(r"\bret_(?:1d|3d|5d|10d|20d|21d)\b", re.I),
)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _aware_datetime_as_utc(value: Any, *, field_path: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not AWARE_ISO8601_RE.fullmatch(value)
    ):
        raise ValueError(
            f"{field_path} must be a strict timezone-aware ISO-8601 datetime"
        )
    parse_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(parse_value)
    except ValueError as exc:
        raise ValueError(
            f"{field_path} must be a strict timezone-aware ISO-8601 datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"{field_path} must be a strict timezone-aware ISO-8601 datetime"
        )
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _normalized_published_at(value: Any, *, field_path: str) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and DATE_ONLY_RE.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field_path} has an invalid ISO-8601 date") from exc
        return value
    return _aware_datetime_as_utc(value, field_path=field_path)


def _normalize_content_timestamps(value: Any, *, path: str = "content") -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            field_path = f"{path}.{key}"
            if key == "as_of":
                normalized[key] = _aware_datetime_as_utc(
                    child, field_path=field_path
                )
            elif key == "published_at":
                normalized[key] = _normalized_published_at(
                    child, field_path=field_path
                )
            else:
                normalized[key] = _normalize_content_timestamps(
                    child, path=field_path
                )
        return normalized
    if isinstance(value, list):
        return [
            _normalize_content_timestamps(child, path=f"{path}[{index}]")
            for index, child in enumerate(value)
        ]
    return value


def _bind_source_content(
    *, source_content: dict[str, Any], dev_content: dict[str, Any], sample_id: str
) -> dict[str, str]:
    source_json = stable_json(source_content)
    dev_json = stable_json(dev_content)
    raw_match = source_json == dev_json
    try:
        normalized_dev = _normalize_content_timestamps(dev_content)
        normalized_source = (
            normalized_dev
            if raw_match
            else _normalize_content_timestamps(source_content)
        )
    except ValueError as exc:
        raise ValueError(
            f"source-only timestamp normalization failed for {sample_id}: {exc}"
        ) from exc
    normalized_json = stable_json(normalized_dev)
    if not raw_match and stable_json(normalized_source) != normalized_json:
        raise ValueError(
            "source-only content does not match clean DEV user payload under "
            f"{SOURCE_CONTENT_EQUIVALENCE_CONTRACT_VERSION}: {sample_id}"
        )
    return {
        "match_method": "RAW_EXACT" if raw_match else "TIMEZONE_NORMALIZED",
        "raw_source_content_sha256": sha256_bytes(source_json.encode("utf-8")),
        "raw_dev_content_sha256": sha256_bytes(dev_json.encode("utf-8")),
        "normalized_content_sha256": sha256_bytes(
            normalized_json.encode("utf-8")
        ),
    }


def _read_jsonl_objects(
    path: Path, *, label: str, allow_empty: bool = False
) -> tuple[list[dict[str, Any]], bytes]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} input is missing: {path}")
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} input is not UTF-8: {path}") from exc
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{label} line {line_number} is not valid JSON"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(f"{label} line {line_number} is not an object")
        rows.append(value)
    if not rows and not allow_empty:
        raise ValueError(f"{label} input is empty")
    return rows, raw


def _strict_sample_id(value: Any, *, label: str, line_number: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} line {line_number} has invalid sample_id")
    if len(value) > 200:
        raise ValueError(f"{label} line {line_number} sample_id is too long")
    return value


def _walk_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key).strip().casefold())
            keys.update(_walk_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_walk_keys(child))
    return keys


def _walk_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, dict):
        for child in value.values():
            strings.extend(_walk_strings(child))
    elif isinstance(value, list):
        for child in value:
            strings.extend(_walk_strings(child))
    elif isinstance(value, str):
        strings.append(value)
    return strings


def _load_source_only(
    path: Path,
) -> tuple[dict[str, dict[str, Any]], bytes]:
    rows, raw = _read_jsonl_objects(path, label="source-only")
    mapped: dict[str, dict[str, Any]] = {}
    for line_number, row in enumerate(rows, start=1):
        if set(row) != {"sample_id", "content"}:
            raise ValueError(
                f"source-only line {line_number} must contain only sample_id and content"
            )
        sample_id = _strict_sample_id(
            row.get("sample_id"), label="source-only", line_number=line_number
        )
        if sample_id in mapped:
            raise ValueError(f"source-only duplicate sample_id: {sample_id}")
        content = row.get("content")
        if not isinstance(content, dict) or not content:
            raise ValueError(f"source-only line {line_number} has invalid content")
        prohibited = sorted(_walk_keys(content) & PROHIBITED_SOURCE_KEYS)
        if prohibited:
            raise ValueError(
                f"source-only line {line_number} contains prohibited supervision keys: "
                + ",".join(prohibited)
            )
        if any(
            pattern.search(text)
            for text in _walk_strings(content)
            for pattern in PROHIBITED_SOURCE_TEXT
        ):
            raise ValueError(
                f"source-only line {line_number} contains prohibited post-event supervision text"
            )
        outcome_reasons = post_event_supervision_reasons(content)
        if outcome_reasons:
            raise ValueError(
                f"source-only line {line_number} contains prohibited post-event "
                "supervision: " + ",".join(outcome_reasons)
            )
        mapped[sample_id] = content
    return mapped, raw


def _load_reviews(
    path: Path,
    *,
    slot: str,
    allow_empty: bool = False,
) -> tuple[dict[str, dict[str, Any]], bytes]:
    rows, raw = _read_jsonl_objects(
        path, label=f"review {slot}", allow_empty=allow_empty
    )
    mapped: dict[str, dict[str, Any]] = {}
    expected_review_class = REVIEW_CLASS_BY_SLOT[slot]
    for line_number, row in enumerate(rows, start=1):
        if set(row) != REVIEW_ROW_FIELDS:
            raise ValueError(
                f"review {slot} line {line_number} has invalid flat review fields"
            )
        sample_id = _strict_sample_id(
            row.get("sample_id"), label=f"review {slot}", line_number=line_number
        )
        if sample_id in mapped:
            raise ValueError(f"review {slot} duplicate sample_id: {sample_id}")
        materiality = row.get("materiality")
        polarity = row.get("polarity")
        reason = row.get("reason")
        review_class = row.get("review_class")
        if not isinstance(materiality, str) or materiality not in MATERIALITY:
            raise ValueError(
                f"review {slot} has invalid materiality for {sample_id}"
            )
        if not isinstance(polarity, str) or polarity not in POLARITIES:
            raise ValueError(f"review {slot} has invalid polarity for {sample_id}")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"review {slot} has empty reason for {sample_id}")
        if review_class != expected_review_class:
            raise ValueError(
                f"review {slot} review_class mismatch for {sample_id}"
            )
        outcome_reasons = post_event_supervision_reasons(
            reason, review_reason=True
        )
        if any(pattern.search(reason) for pattern in PROHIBITED_REVIEW_TEXT) or (
            outcome_reasons
        ):
            raise ValueError(
                f"review {slot} contains prohibited prediction or market text: "
                f"{sample_id}"
                + (
                    " (" + ",".join(outcome_reasons) + ")"
                    if outcome_reasons
                    else ""
                )
            )
        mapped[sample_id] = {
            "pair": (materiality, polarity),
            "row_sha256": sha256_bytes(stable_json(row).encode("utf-8")),
        }
    return mapped, raw


def _load_dev_sft(
    path: Path,
) -> tuple[list[dict[str, Any]], bytes, dict[str, str]]:
    rows, raw = _read_jsonl_objects(path, label="clean DEV SFT")
    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    common_prompt: tuple[str, str] | None = None
    for line_number, row in enumerate(rows, start=1):
        if set(row) != {"messages", "metadata"}:
            raise ValueError(
                f"clean DEV SFT line {line_number} must contain only messages and metadata"
            )
        messages = row.get("messages")
        if not isinstance(messages, list) or len(messages) != 3:
            raise ValueError(f"clean DEV SFT line {line_number} has invalid messages")
        expected_roles = ("system", "user", "assistant")
        for index, (message, expected_role) in enumerate(
            zip(messages, expected_roles), start=1
        ):
            if (
                not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or message.get("role") != expected_role
                or not isinstance(message.get("content"), str)
            ):
                raise ValueError(
                    f"clean DEV SFT line {line_number} message {index} is invalid"
                )
        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError(f"clean DEV SFT line {line_number} metadata is invalid")
        sample_id = _strict_sample_id(
            metadata.get("sample_id"),
            label="clean DEV SFT",
            line_number=line_number,
        )
        if sample_id in seen:
            raise ValueError(f"clean DEV SFT duplicate sample_id: {sample_id}")
        seen.add(sample_id)
        if str(metadata.get("split") or "").strip().upper() != "DEV":
            raise ValueError(f"clean DEV SFT row is not DEV: {sample_id}")
        if metadata.get("target_contract") != TARGET_CONTRACT:
            raise ValueError(f"clean DEV SFT target_contract mismatch: {sample_id}")
        if metadata.get("model_output_contract") != MODEL_OUTPUT_CONTRACT:
            raise ValueError(
                f"clean DEV SFT model_output_contract mismatch: {sample_id}"
            )
        if metadata.get("weak_supervision_version") != QWEN_WEAK_SUPERVISION_VERSION:
            raise ValueError(
                f"clean DEV SFT weak supervision version mismatch: {sample_id}"
            )
        for field in REQUIRED_FALSE_BOUNDARIES:
            if metadata.get(field) is not False:
                raise ValueError(
                    f"clean DEV SFT {field} must be false: {sample_id}"
                )

        prompt_version = metadata.get("prompt_version")
        prompt_sha256 = metadata.get("prompt_sha256")
        if prompt_version != QWEN_WEAK_PROMPT_VERSION:
            raise ValueError(f"clean DEV SFT prompt_version mismatch: {sample_id}")
        if not isinstance(prompt_sha256, str):
            raise ValueError(f"clean DEV SFT prompt_sha256 is invalid: {sample_id}")
        prompt_sha256 = prompt_sha256.strip().lower()
        if not SHA256_RE.fullmatch(prompt_sha256):
            raise ValueError(f"clean DEV SFT prompt_sha256 is invalid: {sample_id}")
        if prompt_sha256 != QWEN_WEAK_PROMPT_SHA256:
            raise ValueError(f"clean DEV SFT prompt contract SHA mismatch: {sample_id}")
        if messages[0]["content"] != QWEN_WEAK_SYSTEM_PROMPT:
            raise ValueError(f"clean DEV SFT system prompt text mismatch: {sample_id}")
        actual_prompt_sha256 = sha256_bytes(messages[0]["content"].encode("utf-8"))
        if actual_prompt_sha256 != prompt_sha256:
            raise ValueError(f"clean DEV SFT prompt SHA mismatch: {sample_id}")
        prompt_binding = (prompt_version.strip(), prompt_sha256)
        if common_prompt is None:
            common_prompt = prompt_binding
        elif prompt_binding != common_prompt:
            raise ValueError(f"clean DEV SFT prompt binding is mixed: {sample_id}")

        try:
            content = json.loads(messages[1]["content"])
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"clean DEV SFT user content is not JSON: {sample_id}"
            ) from exc
        if not isinstance(content, dict) or not content:
            raise ValueError(f"clean DEV SFT user content is invalid: {sample_id}")
        content_sha256 = sha256_bytes(stable_json(content).encode("utf-8"))
        stored_content_sha256 = metadata.get("content_sha256")
        if not isinstance(stored_content_sha256, str) or (
            stored_content_sha256.strip().lower() != content_sha256
        ):
            raise ValueError(f"clean DEV SFT content SHA mismatch: {sample_id}")

        base_metadata = {
            field: metadata[field]
            for field in SAFE_BASE_METADATA_FIELDS
            if field in metadata
        }
        prepared.append(
            {
                "sample_id": sample_id,
                "system_message": dict(messages[0]),
                "user_message": dict(messages[1]),
                "content": content,
                "content_sha256": content_sha256,
                "base_metadata": base_metadata,
            }
        )
    assert common_prompt is not None
    return prepared, raw, {
        "version": common_prompt[0],
        "sha256": common_prompt[1],
    }


def _require_coverage(
    *, label: str, actual: set[str], expected: set[str]
) -> None:
    if actual == expected:
        return
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    details = []
    if missing:
        details.append("missing=" + ",".join(missing[:3]))
    if unexpected:
        details.append("unexpected=" + ",".join(unexpected[:3]))
    raise ValueError(f"{label} sample_id coverage mismatch: " + "; ".join(details))


def _write_new_file(path: Path, raw: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _publish_atomic(output_dir: Path, files: dict[str, bytes]) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = output_dir.parent / f".{output_dir.name}.{uuid.uuid4().hex}.tmp"
    stage.mkdir(exist_ok=False)
    try:
        for filename, raw in files.items():
            _write_new_file(stage / filename, raw)
        if os.path.lexists(output_dir):
            raise FileExistsError(f"output directory appeared during write: {output_dir}")
        stage.rename(output_dir)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def build_overlay(
    *,
    dev_sft: Path,
    source_only: Path,
    review_a: Path,
    review_b: Path,
    review_c: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Validate all bindings and atomically publish the 138-row overlay."""

    if os.path.lexists(output_dir):
        raise FileExistsError(f"output directory already exists: {output_dir}")
    output_dir = output_dir.resolve()
    if os.path.lexists(output_dir):
        raise FileExistsError(f"output directory already exists: {output_dir}")
    paths = {
        "dev_sft": dev_sft.resolve(),
        "source_only": source_only.resolve(),
        "review_a": review_a.resolve(),
        "review_b": review_b.resolve(),
        "review_c": review_c.resolve(),
    }
    missing = [f"{name}={path}" for name, path in paths.items() if not path.is_file()]
    if missing:
        raise FileNotFoundError("required input is missing: " + ", ".join(missing))

    dev_rows, dev_raw, prompt = _load_dev_sft(paths["dev_sft"])
    if len(dev_rows) != EXPECTED_ROW_COUNT:
        raise ValueError(
            f"clean DEV SFT must contain exactly {EXPECTED_ROW_COUNT} rows; "
            f"found {len(dev_rows)}"
        )
    ordered_ids = [row["sample_id"] for row in dev_rows]
    expected_ids = set(ordered_ids)

    source_by_id, source_raw = _load_source_only(paths["source_only"])
    review_a_by_id, review_a_raw = _load_reviews(paths["review_a"], slot="A")
    review_b_by_id, review_b_raw = _load_reviews(paths["review_b"], slot="B")
    _require_coverage(
        label="source-only", actual=set(source_by_id), expected=expected_ids
    )
    _require_coverage(
        label="review A", actual=set(review_a_by_id), expected=expected_ids
    )
    _require_coverage(
        label="review B", actual=set(review_b_by_id), expected=expected_ids
    )

    source_binding_by_id: dict[str, dict[str, str]] = {}
    source_match_counts: Counter[str] = Counter()
    for dev_row in dev_rows:
        sample_id = dev_row["sample_id"]
        source_content = source_by_id[sample_id]
        binding = _bind_source_content(
            source_content=source_content,
            dev_content=dev_row["content"],
            sample_id=sample_id,
        )
        if binding["raw_dev_content_sha256"] != dev_row["content_sha256"]:
            raise RuntimeError(f"clean DEV content binding changed: {sample_id}")
        source_binding_by_id[sample_id] = binding
        source_match_counts[binding["match_method"]] += 1

    raw_match_count = source_match_counts["RAW_EXACT"]
    timezone_normalized_match_count = source_match_counts["TIMEZONE_NORMALIZED"]
    if raw_match_count + timezone_normalized_match_count != EXPECTED_ROW_COUNT:
        raise RuntimeError("source content equivalence accounting is incomplete")

    disagreement_ids = {
        sample_id
        for sample_id in expected_ids
        if review_a_by_id[sample_id]["pair"] != review_b_by_id[sample_id]["pair"]
    }
    review_c_by_id, review_c_raw = _load_reviews(
        paths["review_c"], slot="C", allow_empty=True
    )
    _require_coverage(
        label="review C arbitration",
        actual=set(review_c_by_id),
        expected=disagreement_ids,
    )

    input_raw = {
        "dev_sft": dev_raw,
        "source_only": source_raw,
        "review_a": review_a_raw,
        "review_b": review_b_raw,
        "review_c": review_c_raw,
    }
    input_sha256 = {name: sha256_bytes(raw) for name, raw in input_raw.items()}
    input_rows = {
        "dev_sft": len(dev_rows),
        "source_only": len(source_by_id),
        "review_a": len(review_a_by_id),
        "review_b": len(review_b_by_id),
        "review_c": len(review_c_by_id),
    }

    output_rows: list[dict[str, Any]] = []
    resolution_counts: Counter[str] = Counter()
    materiality_counts: Counter[str] = Counter()
    polarity_counts: Counter[str] = Counter()
    priority_counts: Counter[str] = Counter()
    for dev_row in dev_rows:
        sample_id = dev_row["sample_id"]
        source_binding = source_binding_by_id[sample_id]
        a = review_a_by_id[sample_id]
        b = review_b_by_id[sample_id]
        if a["pair"] == b["pair"]:
            selected_pair = a["pair"]
            decision_source = "A_B_CONSENSUS"
            selected_slot = "A+B"
            review_hashes = {
                "A": a["row_sha256"],
                "B": b["row_sha256"],
            }
        else:
            c = review_c_by_id[sample_id]
            selected_pair = c["pair"]
            decision_source = "C_ARBITRATION"
            selected_slot = "C"
            review_hashes = {
                "A": a["row_sha256"],
                "B": b["row_sha256"],
                "C": c["row_sha256"],
            }
        materiality, polarity = selected_pair
        semantic_target = expected_semantic_payload(materiality, polarity)
        semantic_issues = validate_semantic_payload(semantic_target)
        if semantic_issues:
            raise RuntimeError(
                f"derived core-v1 payload is invalid for {sample_id}: {semantic_issues}"
            )
        model_target = {"materiality": materiality, "polarity": polarity}
        metadata = {
            **dev_row["base_metadata"],
            "sample_id": sample_id,
            "content_sha256": dev_row["content_sha256"],
            "split": "DEV",
            "target_contract": TARGET_CONTRACT,
            "model_output_contract": MODEL_OUTPUT_CONTRACT,
            "semantic_target": semantic_target,
            "prompt_version": prompt["version"],
            "prompt_sha256": prompt["sha256"],
            "overlay_contract_version": CONTRACT_VERSION,
            "source_dataset_contract": QWEN_WEAK_SUPERVISION_VERSION,
            "label_provenance": LABEL_PROVENANCE,
            "label_classification": LABEL_CLASSIFICATION,
            "human_gold_claimed": False,
            "qwen_prediction_included": False,
            "post_event_market_data_included": False,
            "evidence_state_used_as_model_target": False,
            "original_weak_truth_used": False,
            "source_payload_binding_verified": True,
            "source_payload_sha256": source_binding[
                "raw_source_content_sha256"
            ],
            "source_content_equivalence": {
                "contract_version": (
                    SOURCE_CONTENT_EQUIVALENCE_CONTRACT_VERSION
                ),
                "match_method": source_binding["match_method"],
                "raw_dev_content_sha256": source_binding[
                    "raw_dev_content_sha256"
                ],
                "raw_source_content_sha256": source_binding[
                    "raw_source_content_sha256"
                ],
                "normalized_content_sha256": source_binding[
                    "normalized_content_sha256"
                ],
                "raw_match_count": raw_match_count,
                "timezone_normalized_match_count": (
                    timezone_normalized_match_count
                ),
            },
            "overlay_input_sha256": dict(input_sha256),
            "review_resolution": {
                "policy": "A_B_CONSENSUS_ELSE_C_ONLY",
                "agreement_basis": "COMPLETE_MATERIALITY_POLARITY_PAIR",
                "decision_source": decision_source,
                "selected_review_slot": selected_slot,
                "a_b_target_pair_agreed": decision_source == "A_B_CONSENSUS",
                "original_review_row_sha256": review_hashes,
                "original_review_file_sha256": {
                    "A": input_sha256["review_a"],
                    "B": input_sha256["review_b"],
                    "C": input_sha256["review_c"],
                },
            },
        }
        output_rows.append(
            {
                "messages": [
                    dev_row["system_message"],
                    dev_row["user_message"],
                    {"role": "assistant", "content": stable_json(model_target)},
                ],
                "metadata": metadata,
            }
        )
        resolution_counts[decision_source] += 1
        materiality_counts[materiality] += 1
        polarity_counts[polarity] += 1
        priority_counts[semantic_target["semantic_priority"]] += 1

    if len(output_rows) != EXPECTED_ROW_COUNT:
        raise RuntimeError("overlay row count changed during construction")
    output_bytes = b"".join(
        (stable_json(row) + "\n").encode("utf-8") for row in output_rows
    )
    output_sha256 = sha256_bytes(output_bytes)
    output_sidecar = f"{output_sha256}  {OUTPUT_NAME}\n".encode("ascii")
    sorted_disagreements = sorted(disagreement_ids)
    normalized_content_index = [
        {
            "sample_id": sample_id,
            "normalized_content_sha256": source_binding_by_id[sample_id][
                "normalized_content_sha256"
            ],
        }
        for sample_id in ordered_ids
    ]
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contract_version": CONTRACT_VERSION,
        "source_dataset_contract": QWEN_WEAK_SUPERVISION_VERSION,
        "target_contract": TARGET_CONTRACT,
        "model_output_contract": MODEL_OUTPUT_CONTRACT,
        "review_input_schema": {
            "fields": sorted(REVIEW_ROW_FIELDS),
            "review_class_by_slot": dict(REVIEW_CLASS_BY_SLOT),
            "semantic_v2_fields_required_or_derived": False,
        },
        "label_provenance": LABEL_PROVENANCE,
        "label_classification": LABEL_CLASSIFICATION,
        "human_gold_claimed": False,
        "expected_row_count": EXPECTED_ROW_COUNT,
        "row_count": len(output_rows),
        "prompt": {
            "version": prompt["version"],
            "sha256": prompt["sha256"],
            "system_message_binding_verified": True,
        },
        "inputs": {
            name: {
                "filename": paths[name].name,
                "sha256": input_sha256[name],
                "row_count": input_rows[name],
            }
            for name in paths
        },
        "coverage": {
            "dev_source_a_b_sample_id_coverage_exact": True,
            "review_c_equals_a_b_disagreement_set": True,
            "a_b_disagreement_count": len(disagreement_ids),
            "a_b_disagreement_sample_ids_sha256": sha256_bytes(
                stable_json(sorted_disagreements).encode("utf-8")
            ),
            "source_payload_binding_verified_rows": len(output_rows),
        },
        "source_content_equivalence": {
            "contract_version": SOURCE_CONTENT_EQUIVALENCE_CONTRACT_VERSION,
            "raw_match_fast_path": True,
            "timezone_normalized_fallback": True,
            "normalized_time_keys": sorted(SOURCE_CONTENT_TIME_KEYS),
            "aware_datetime_canonical_timezone": "UTC",
            "published_at_date_only_preserved": True,
            "naive_or_unparseable_datetime_allowed": False,
            "raw_match_count": raw_match_count,
            "timezone_normalized_match_count": timezone_normalized_match_count,
            "normalized_content_sha256_index_sha256": sha256_bytes(
                stable_json(normalized_content_index).encode("utf-8")
            ),
        },
        "resolution_policy": {
            "name": "A_B_CONSENSUS_ELSE_C_ONLY",
            "agreement_basis": "COMPLETE_MATERIALITY_POLARITY_PAIR",
            "axis_wise_label_mixing_allowed": False,
            "c_used_only_for_a_b_pair_disagreements": True,
        },
        "resolution_counts": dict(sorted(resolution_counts.items())),
        "label_distribution": {
            "materiality": dict(sorted(materiality_counts.items())),
            "polarity": dict(sorted(polarity_counts.items())),
            "semantic_priority": dict(sorted(priority_counts.items())),
        },
        "output": {
            "filename": OUTPUT_NAME,
            "row_count": len(output_rows),
            "sha256": output_sha256,
            "sidecar": OUTPUT_NAME + ".sha256",
            "sidecar_sha256": sha256_bytes(output_sidecar),
        },
        "isolation": {
            "source_payloads_match_under_declared_equivalence_contract": True,
            "sft_assistant_target_ignored": True,
            "sft_metadata_semantic_target_ignored": True,
            "original_weak_truth_decoded_as_semantic_payload": False,
            "original_weak_truth_used": False,
            "qwen_predictions_read": False,
            "market_results_read": False,
            "external_facts_read": False,
            "frozen_review_rows_rewritten": False,
            "input_sha256_embedded_in_each_output_row": True,
        },
    }
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    manifest_sha256 = sha256_bytes(manifest_bytes)
    manifest_sidecar = f"{manifest_sha256}  {MANIFEST_NAME}\n".encode("ascii")
    _publish_atomic(
        output_dir,
        {
            OUTPUT_NAME: output_bytes,
            OUTPUT_NAME + ".sha256": output_sidecar,
            MANIFEST_NAME: manifest_bytes,
            MANIFEST_NAME + ".sha256": manifest_sidecar,
        },
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-sft", type=Path, required=True)
    parser.add_argument("--source-only", type=Path, required=True)
    parser.add_argument("--review-a", type=Path, required=True)
    parser.add_argument("--review-b", type=Path, required=True)
    parser.add_argument("--review-c", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_overlay(
        dev_sft=args.dev_sft,
        source_only=args.source_only,
        review_a=args.review_a,
        review_b=args.review_b,
        review_c=args.review_c,
        output_dir=args.output_dir,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
