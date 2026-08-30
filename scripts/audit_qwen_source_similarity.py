#!/usr/bin/env python3
"""Audit source-text overlap between Qwen supervision and a strict provider set.

The strict input is deliberately limited to provider rows containing only
``sample_id`` and ``content``.  The audit consumes source text and identifiers;
it never accepts or reads expected labels, model outputs, or market outcomes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
AUDIT_CONTRACT = "qwen-source-similarity-audit-v1"
DEFAULT_THRESHOLD = 0.8
SHINGLE_SIZE = 3
MIN_NORMALIZED_HEADLINE_LENGTH = 20


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8-sig") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number}: row is not an object")
            yield number, value


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


def source_text(content: dict[str, Any]) -> str:
    parts = [str(content.get("headline") or ""), str(content.get("summary") or "")]
    passages = content.get("passages")
    if passages is not None and not isinstance(passages, list):
        raise ValueError("content.passages must be a list when present")
    for passage in passages or []:
        if not isinstance(passage, dict):
            raise ValueError("content.passages rows must be objects")
        parts.append(str(passage.get("passage") or ""))
    return " ".join(parts)


def _training_content(path: Path, number: int, row: dict[str, Any]) -> dict[str, Any]:
    messages = row.get("messages")
    if not isinstance(messages, list):
        raise ValueError(f"{path}:{number}: messages must be a list")
    users = [item for item in messages if isinstance(item, dict) and item.get("role") == "user"]
    if len(users) != 1:
        raise ValueError(f"{path}:{number}: expected exactly one user message")
    content = json.loads(str(users[0].get("content") or ""))
    if not isinstance(content, dict):
        raise ValueError(f"{path}:{number}: user content is not an object")
    return content


def _training_rows(paths: list[tuple[str, Path]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for split, path in paths:
        for number, row in read_jsonl(path):
            metadata = row.get("metadata")
            if not isinstance(metadata, dict):
                raise ValueError(f"{path}:{number}: metadata is not an object")
            sample_id = str(metadata.get("sample_id") or "").strip()
            if not sample_id:
                raise ValueError(f"{path}:{number}: missing metadata.sample_id")
            if sample_id in seen:
                raise ValueError(f"duplicate training sample_id: {sample_id}")
            seen.add(sample_id)
            content = _training_content(path, number, row)
            text = source_text(content)
            result.append(
                {
                    "sample_id": sample_id,
                    "event_id": str(metadata.get("event_id") or "").strip() or None,
                    "canonical_issuer_key": (
                        str(metadata.get("canonical_issuer_key") or "").strip() or None
                    ),
                    "split": split,
                    "headline": str(content.get("headline") or ""),
                    "normalized_headline": normalize_text(content.get("headline")),
                    "normalized_text": normalize_text(text),
                    "shingles": shingles(text),
                    "content_sha256": hashlib.sha256(
                        stable_json(content).encode("utf-8")
                    ).hexdigest(),
                }
            )
    return result


def _strict_rows(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for number, row in read_jsonl(path):
        # Exact keys make it impossible to accidentally consume an answer or
        # model-output field from a mislabeled strict file.
        if set(row) != {"sample_id", "content"}:
            raise ValueError(
                f"{path}:{number}: strict provider rows must contain exactly "
                "sample_id and content"
            )
        sample_id = str(row.get("sample_id") or "").strip()
        content = row.get("content")
        if not sample_id or not isinstance(content, dict):
            raise ValueError(f"{path}:{number}: invalid strict provider row")
        if sample_id in seen:
            raise ValueError(f"duplicate strict sample_id: {sample_id}")
        seen.add(sample_id)
        text = source_text(content)
        result.append(
            {
                "sample_id": sample_id,
                "headline": str(content.get("headline") or ""),
                "normalized_headline": normalize_text(content.get("headline")),
                "normalized_text": normalize_text(text),
                "shingles": shingles(text),
                "content_sha256": hashlib.sha256(
                    stable_json(content).encode("utf-8")
                ).hexdigest(),
            }
        )
    return result


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def audit_qwen_source_similarity(
    *,
    train_unique: Path,
    dev: Path,
    strict_provider: Path,
    output: Path,
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[str, Any]:
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")

    training = _training_rows([("TRAIN", train_unique), ("DEV", dev)])
    strict = _strict_rows(strict_provider)
    violations: list[dict[str, Any]] = []
    max_pair: dict[str, Any] | None = None
    max_score = -1.0
    headline_pair_count = 0
    shingle_pair_count = 0

    for strict_row in strict:
        for training_row in training:
            score = jaccard(strict_row["shingles"], training_row["shingles"])
            if score > max_score:
                max_score = score
                max_pair = {
                    "strict_sample_id": strict_row["sample_id"],
                    "training_sample_id": training_row["sample_id"],
                    "training_split": training_row["split"],
                    "jaccard": round(score, 12),
                }

            reasons: list[str] = []
            strict_title = strict_row["normalized_headline"]
            training_title = training_row["normalized_headline"]
            strict_in_training = (
                len(strict_title) >= MIN_NORMALIZED_HEADLINE_LENGTH
                and strict_title in training_row["normalized_text"]
            )
            training_in_strict = (
                len(training_title) >= MIN_NORMALIZED_HEADLINE_LENGTH
                and training_title in strict_row["normalized_text"]
            )
            if strict_in_training:
                reasons.append("STRICT_HEADLINE_IN_TRAINING_SOURCE")
            if training_in_strict:
                reasons.append("TRAINING_HEADLINE_IN_STRICT_SOURCE")
            if strict_in_training or training_in_strict:
                headline_pair_count += 1
            if score >= threshold:
                reasons.append("SHINGLE_JACCARD_AT_OR_ABOVE_THRESHOLD")
                shingle_pair_count += 1
            if not reasons:
                continue

            violations.append(
                {
                    "canonical_issuer_key": training_row["canonical_issuer_key"],
                    "jaccard": round(score, 12),
                    "reasons": reasons,
                    "strict_content_sha256": strict_row["content_sha256"],
                    "strict_headline": strict_row["headline"],
                    "strict_sample_id": strict_row["sample_id"],
                    "training_content_sha256": training_row["content_sha256"],
                    "training_event_id": training_row["event_id"],
                    "training_headline": training_row["headline"],
                    "training_sample_id": training_row["sample_id"],
                    "training_split": training_row["split"],
                }
            )

    violations.sort(
        key=lambda row: (
            str(row["strict_sample_id"]),
            str(row["training_sample_id"]),
        )
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "audit_contract": AUDIT_CONTRACT,
        "result": "LEAKAGE_DETECTED" if violations else "PASS",
        "strict_labels_read": False,
        "strict_fields_consumed": [
            "sample_id",
            "content.headline",
            "content.summary",
            "content.passages[].passage",
        ],
        "settings": {
            "headline_substring_bidirectional": True,
            "minimum_normalized_headline_length": MIN_NORMALIZED_HEADLINE_LENGTH,
            "normalization": "lowercase ASCII alphanumeric tokens joined by one space",
            "shingle_size": SHINGLE_SIZE,
            "threshold": threshold,
        },
        "inputs": {
            "train_unique": {"path": str(train_unique), "sha256": sha256_file(train_unique)},
            "dev": {"path": str(dev), "sha256": sha256_file(dev)},
            "strict_provider": {
                "path": str(strict_provider),
                "sha256": sha256_file(strict_provider),
            },
        },
        "counts": {
            "training_rows": len(training),
            "strict_rows": len(strict),
            "pair_comparisons": len(training) * len(strict),
            "headline_overlap_pairs": headline_pair_count,
            "shingle_threshold_pairs": shingle_pair_count,
            "violating_pairs": len(violations),
            "training_samples_with_violations": len(
                {row["training_sample_id"] for row in violations}
            ),
            "strict_samples_with_violations": len(
                {row["strict_sample_id"] for row in violations}
            ),
        },
        "max_jaccard_pair": max_pair,
        "violations": violations,
    }
    _atomic_write_json(output, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-unique", type=Path, required=True)
    parser.add_argument("--dev", type=Path, required=True)
    parser.add_argument("--strict-provider", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = audit_qwen_source_similarity(
        train_unique=args.train_unique.resolve(),
        dev=args.dev.resolve(),
        strict_provider=args.strict_provider.resolve(),
        output=args.output.resolve(),
        threshold=args.threshold,
    )
    print(stable_json({
        "output": str(args.output.resolve()),
        "result": report["result"],
        "counts": report["counts"],
        "max_jaccard_pair": report["max_jaccard_pair"],
    }))
    return 1 if report["result"] == "LEAKAGE_DETECTED" else 0


if __name__ == "__main__":
    raise SystemExit(main())
