#!/usr/bin/env python3
"""Audit source-text similarity from Qwen train_unique into DEV.

Only source text from the single user message is compared: headline, summary,
and passage text.  Assistant-message content, labels, predictions, market
outcomes, and label-provenance metadata are never consumed or emitted.

The output directory is an atomic, immutable publication.  It must not already
exist and contains a hash-bound JSON audit plus a DEV-only quality-exclusion
JSONL.  Leakage is reported with exit code 1 after the artifacts are published.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
AUDIT_CONTRACT = "qwen-train-dev-source-similarity-audit-v1"
DEFAULT_THRESHOLD = 0.8
SHINGLE_SIZE = 3
MIN_NORMALIZED_HEADLINE_LENGTH = 20

AUDIT_NAME = "train_dev_source_similarity_audit.json"
EXCLUSIONS_NAME = "quality_exclusions.jsonl"

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
        "reason_codes",
        "reviewer_label",
        "reviewer_labels",
        "semantic_priority",
        "target",
        "target_label",
    }
)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def normalize_text(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def shingles(value: Any, size: int = SHINGLE_SIZE) -> set[tuple[str, ...]]:
    tokens = normalize_text(value).split()
    if len(tokens) < size:
        return {tuple(tokens)} if tokens else set()
    return {
        tuple(tokens[index:index + size])
        for index in range(len(tokens) - size + 1)
    }


def jaccard(left: set[Any], right: set[Any]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


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


def _source_payload(content: dict[str, Any], *, location: str) -> dict[str, Any]:
    prohibited = sorted(_walk_keys(content) & PROHIBITED_SOURCE_KEYS)
    if prohibited:
        raise ValueError(
            f"{location}: user source contains prohibited supervision keys: "
            + ",".join(prohibited)
        )

    passages = content.get("passages")
    if passages is not None and not isinstance(passages, list):
        raise ValueError(f"{location}: content.passages must be a list when present")
    passage_texts: list[str] = []
    for index, passage in enumerate(passages or [], 1):
        if not isinstance(passage, dict):
            raise ValueError(
                f"{location}: content.passages row {index} must be an object"
            )
        passage_texts.append(str(passage.get("passage") or ""))

    payload = {
        "headline": str(content.get("headline") or ""),
        "summary": str(content.get("summary") or ""),
        "passages": passage_texts,
    }
    if not any(value.strip() for value in [payload["headline"], payload["summary"], *passage_texts]):
        raise ValueError(f"{location}: source text is empty")
    return payload


def _source_text(payload: dict[str, Any]) -> str:
    return " ".join(
        [payload["headline"], payload["summary"], *payload["passages"]]
    )


def _read_dataset_rows(path: Path, *, split: str) -> tuple[list[dict[str, Any]], str, int]:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: input is not UTF-8") from exc

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: invalid JSON") from exc
        if not isinstance(row, dict) or set(row) != {"messages", "metadata"}:
            raise ValueError(
                f"{path}:{number}: row must contain exactly messages and metadata"
            )

        metadata = row.get("metadata")
        if not isinstance(metadata, dict):
            raise ValueError(f"{path}:{number}: metadata is not an object")
        sample_id = str(metadata.get("sample_id") or "").strip()
        event_id = str(metadata.get("event_id") or "").strip()
        if not sample_id or not event_id:
            raise ValueError(
                f"{path}:{number}: metadata.sample_id and metadata.event_id are required"
            )
        if sample_id in seen:
            raise ValueError(f"{path}:{number}: duplicate sample_id: {sample_id}")
        seen.add(sample_id)

        messages = row.get("messages")
        if not isinstance(messages, list):
            raise ValueError(f"{path}:{number}: messages must be a list")
        # Deliberately inspect only the role marker and the one user message.
        # Assistant content is never selected, parsed as a target, interpreted,
        # or emitted by this audit.
        user_messages = [
            message
            for message in messages
            if isinstance(message, dict) and message.get("role") == "user"
        ]
        if len(user_messages) != 1:
            raise ValueError(f"{path}:{number}: expected exactly one user message")
        raw_user_content = user_messages[0].get("content")
        if not isinstance(raw_user_content, str):
            raise ValueError(f"{path}:{number}: user message content must be a string")
        try:
            content = json.loads(raw_user_content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number}: user content is not valid JSON") from exc
        if not isinstance(content, dict):
            raise ValueError(f"{path}:{number}: user content is not an object")

        location = f"{path}:{number}"
        source = _source_payload(content, location=location)
        source_text = _source_text(source)
        result.append(
            {
                "sample_id": sample_id,
                "event_id": event_id,
                "split": split,
                "normalized_headline": normalize_text(source["headline"]),
                "normalized_text": normalize_text(source_text),
                "shingles": shingles(source_text),
                "source_sha256": _sha256_bytes(
                    stable_json(source).encode("utf-8")
                ),
            }
        )
    if not result:
        raise ValueError(f"{path}: dataset is empty")
    return result, _sha256_bytes(raw), len(raw)


def _quality_reason(reason_codes: set[str]) -> str:
    headline = bool(
        reason_codes
        & {"TRAIN_HEADLINE_IN_DEV_SOURCE", "DEV_HEADLINE_IN_TRAIN_SOURCE"}
    )
    shingle = "SHINGLE_JACCARD_AT_OR_ABOVE_THRESHOLD" in reason_codes
    if headline and shingle:
        return "TRAIN_DEV_HEADLINE_AND_SHINGLE_SIMILARITY"
    if headline:
        return "TRAIN_DEV_HEADLINE_OVERLAP"
    return "TRAIN_DEV_SHINGLE_JACCARD_AT_OR_ABOVE_THRESHOLD"


def _render_json(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _render_jsonl(rows: list[dict[str, Any]]) -> bytes:
    return b"".join((stable_json(row) + "\n").encode("utf-8") for row in rows)


def _sidecar_bytes(digest: str, filename: str) -> bytes:
    return f"{digest}  {filename}\n".encode("ascii")


def _write_bytes_sync(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _publish_output_directory(output_dir: Path, files: dict[str, bytes]) -> None:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.",
            suffix=".in-progress",
            dir=output_dir.parent,
        )
    )
    try:
        for filename, payload in files.items():
            _write_bytes_sync(stage / filename, payload)
        if output_dir.exists():
            raise FileExistsError(
                f"output directory appeared during publication: {output_dir}"
            )
        os.replace(stage, output_dir)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def audit_qwen_train_dev_source_similarity(
    *,
    train_unique: Path,
    dev: Path,
    output_dir: Path,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    """Compare train_unique source text with DEV and atomically publish an audit."""

    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir.resolve()}")

    train_rows, train_sha256, train_bytes = _read_dataset_rows(
        train_unique, split="TRAIN"
    )
    dev_rows, dev_sha256, dev_bytes = _read_dataset_rows(dev, split="DEV")

    violations: list[dict[str, Any]] = []
    max_pair: dict[str, Any] | None = None
    max_score = -1.0
    headline_pair_count = 0
    shingle_pair_count = 0

    for dev_row in dev_rows:
        for train_row in train_rows:
            score = jaccard(train_row["shingles"], dev_row["shingles"])
            if score > max_score:
                max_score = score
                max_pair = {
                    "dev_event_id": dev_row["event_id"],
                    "dev_sample_id": dev_row["sample_id"],
                    "jaccard": round(score, 12),
                    "train_event_id": train_row["event_id"],
                    "train_sample_id": train_row["sample_id"],
                }

            reasons: list[str] = []
            train_title = train_row["normalized_headline"]
            dev_title = dev_row["normalized_headline"]
            train_in_dev = (
                len(train_title) >= MIN_NORMALIZED_HEADLINE_LENGTH
                and train_title in dev_row["normalized_text"]
            )
            dev_in_train = (
                len(dev_title) >= MIN_NORMALIZED_HEADLINE_LENGTH
                and dev_title in train_row["normalized_text"]
            )
            if train_in_dev:
                reasons.append("TRAIN_HEADLINE_IN_DEV_SOURCE")
            if dev_in_train:
                reasons.append("DEV_HEADLINE_IN_TRAIN_SOURCE")
            if train_in_dev or dev_in_train:
                headline_pair_count += 1
            if score >= threshold:
                reasons.append("SHINGLE_JACCARD_AT_OR_ABOVE_THRESHOLD")
                shingle_pair_count += 1
            if not reasons:
                continue

            violations.append(
                {
                    "dev_event_id": dev_row["event_id"],
                    "dev_sample_id": dev_row["sample_id"],
                    "dev_source_sha256": dev_row["source_sha256"],
                    "jaccard": round(score, 12),
                    "reasons": reasons,
                    "train_event_id": train_row["event_id"],
                    "train_sample_id": train_row["sample_id"],
                    "train_source_sha256": train_row["source_sha256"],
                }
            )

    violations.sort(
        key=lambda row: (str(row["dev_sample_id"]), str(row["train_sample_id"]))
    )
    reasons_by_dev: dict[tuple[str, str], set[str]] = {}
    for violation in violations:
        key = (str(violation["dev_sample_id"]), str(violation["dev_event_id"]))
        reasons_by_dev.setdefault(key, set()).update(violation["reasons"])
    exclusions = [
        {
            "event_id": event_id,
            "reason": _quality_reason(reason_codes),
            "sample_id": sample_id,
        }
        for (sample_id, event_id), reason_codes in sorted(reasons_by_dev.items())
    ]
    exclusion_bytes = _render_jsonl(exclusions)
    exclusion_sha256 = _sha256_bytes(exclusion_bytes)
    exclusion_sidecar = _sidecar_bytes(exclusion_sha256, EXCLUSIONS_NAME)

    report = {
        "schema_version": SCHEMA_VERSION,
        "audit_contract": AUDIT_CONTRACT,
        "result": "LEAKAGE_DETECTED" if violations else "PASS",
        "labels_read": False,
        "assistant_message_content_consumed": False,
        "fields_consumed": [
            "metadata.sample_id",
            "metadata.event_id",
            "messages[role=user].content.headline",
            "messages[role=user].content.summary",
            "messages[role=user].content.passages[].passage",
        ],
        "settings": {
            "headline_substring_bidirectional": True,
            "minimum_normalized_headline_length": MIN_NORMALIZED_HEADLINE_LENGTH,
            "normalization": "lowercase ASCII alphanumeric tokens joined by one space",
            "shingle_size": SHINGLE_SIZE,
            "threshold": threshold,
        },
        "inputs": {
            "train_unique": {
                "path": str(train_unique),
                "sha256": train_sha256,
                "byte_count": train_bytes,
                "row_count": len(train_rows),
            },
            "dev": {
                "path": str(dev),
                "sha256": dev_sha256,
                "byte_count": dev_bytes,
                "row_count": len(dev_rows),
            },
        },
        "counts": {
            "train_rows": len(train_rows),
            "dev_rows": len(dev_rows),
            "pair_comparisons": len(train_rows) * len(dev_rows),
            "headline_overlap_pairs": headline_pair_count,
            "shingle_threshold_pairs": shingle_pair_count,
            "violating_pairs": len(violations),
            "train_samples_with_violations": len(
                {row["train_sample_id"] for row in violations}
            ),
            "dev_samples_with_violations": len(
                {row["dev_sample_id"] for row in violations}
            ),
            "quality_exclusion_rows": len(exclusions),
        },
        "max_jaccard_pair": max_pair,
        "outputs": {
            "audit": {
                "filename": AUDIT_NAME,
                "sidecar": AUDIT_NAME + ".sha256",
            },
            "quality_exclusions": {
                "filename": EXCLUSIONS_NAME,
                "row_count": len(exclusions),
                "sha256": exclusion_sha256,
                "sidecar": EXCLUSIONS_NAME + ".sha256",
                "sidecar_sha256": _sha256_bytes(exclusion_sidecar),
            },
        },
        "violations": violations,
    }
    audit_bytes = _render_json(report)
    audit_sha256 = _sha256_bytes(audit_bytes)
    files = {
        AUDIT_NAME: audit_bytes,
        AUDIT_NAME + ".sha256": _sidecar_bytes(audit_sha256, AUDIT_NAME),
        EXCLUSIONS_NAME: exclusion_bytes,
        EXCLUSIONS_NAME + ".sha256": exclusion_sidecar,
    }
    _publish_output_directory(output_dir, files)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-unique", type=Path, required=True)
    parser.add_argument("--dev", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="fresh directory for the atomic audit publication; overwrite is refused",
    )
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = args.output_dir.resolve()
    report = audit_qwen_train_dev_source_similarity(
        train_unique=args.train_unique.resolve(),
        dev=args.dev.resolve(),
        output_dir=output_dir,
        threshold=args.threshold,
    )
    print(
        stable_json(
            {
                "audit": str(output_dir / AUDIT_NAME),
                "counts": report["counts"],
                "max_jaccard_pair": report["max_jaccard_pair"],
                "quality_exclusions": str(output_dir / EXCLUSIONS_NAME),
                "result": report["result"],
            }
        )
    )
    return 1 if report["result"] == "LEAKAGE_DETECTED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
