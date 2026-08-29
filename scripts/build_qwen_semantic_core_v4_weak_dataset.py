#!/usr/bin/env python3
"""Build a leak-audited core-v1 Qwen SFT dataset from weak supervision.

The output is intentionally *not* human gold.  It combines three explicitly
identified supervision sources, removes rows connected to a sealed test pool,
excludes contradictory duplicates, and splits whole entity/event components so
that the development score cannot benefit from near-duplicate issuers or event
chains in training.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.qwen_risk_contract import (  # noqa: E402
    QWEN_RISK_CONTRACT_VERSION,
    expected_semantic_payload,
    normalize_qwen_risk_content,
    validate_semantic_payload,
)
from scripts.train_risk_router_ai_adjudicated import _risk_first_policy  # noqa: E402


DATASET_CONTRACT = "qwen-core-v1-weak-supervision-v4"
SPLIT_SALT = "finance-radar-qwen-core-v4-component-split-20260830"
SYSTEM_PROMPT = (
    "你是金融雷达的语义风险分类器。只根据所给文本判断对焦点资产的极性与做空风险重大性；"
    "不判断证据真假，不补充外部事实，不使用事后价格，不给投资建议。"
    "区分已发生事实、正式决定、提议、风险因素、合同定义与历史重述。"
    "破产重组、确定退市、已发生违约、重大监管处罚、关键临床失败可构成重大负面；"
    "普通风险披露、假设性条款、已解决事项、常规治理和有偿并购退出不得仅凭关键词判为重大负面。"
    "融资同时考虑获得资金与稀释，明确改善或成功结果可判正面。仅输出指定 JSON。"
)
SOURCE_PRIORITY = {
    "DUAL_REVIEW_CONSENSUS": 3,
    "AI_ASSISTED_REFERENCE": 2,
    "DETERMINISTIC_WEAK_RULE": 1,
}
PAIR_MULTIPLIERS = {
    ("NOT_MATERIAL_ADVERSE", "ADVERSE"): 2,
    ("NOT_MATERIAL_ADVERSE", "MIXED"): 4,
    ("UNCLEAR", "UNCLEAR"): 4,
}


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{number}: row is not an object")
            rows.append(value)
    return rows


def atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> str:
    payload = "".join(stable_json(row) + "\n" for row in rows).encode("utf-8")
    atomic_write(path, payload)
    return sha256_bytes(payload)


def _target(row: dict[str, Any]) -> dict[str, str]:
    raw = row.get("expected")
    if raw is None:
        messages = row.get("messages") or []
        if not messages:
            raise ValueError("row has no expected target or messages")
        raw = json.loads(str(messages[-1].get("content") or "{}"))
    issues = validate_semantic_payload(raw)
    if issues:
        raise ValueError("invalid core-v1 target: " + ",".join(issues))
    return expected_semantic_payload(str(raw["materiality"]), str(raw["polarity"]))


def _content(row: dict[str, Any]) -> dict[str, Any]:
    messages = row.get("messages") or []
    user_messages = [item for item in messages if item.get("role") == "user"]
    if not user_messages:
        raise ValueError("row has no user message")
    value = json.loads(str(user_messages[-1].get("content") or "{}"))
    if not isinstance(value, dict):
        raise ValueError("user content is not an object")
    return normalize_qwen_risk_content(value)


def _clean_identifier(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _candidate(row: dict[str, Any], source: str, source_path: Path, row_number: int) -> dict[str, Any]:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    content = _content(row)
    target = _target(row)
    content_hash = sha256_bytes(stable_json(content).encode("utf-8"))
    return {
        "content": content,
        "target": target,
        "target_key": stable_json(target),
        "content_sha256": content_hash,
        "sample_id": _clean_identifier(metadata.get("sample_id") or row.get("sample_id")),
        "event_id": _clean_identifier(
            metadata.get("event_id") or metadata.get("source_event_id") or row.get("event_id")
        ),
        "entity_group": _clean_identifier(metadata.get("entity_group")),
        "canonical_issuer_key": _clean_identifier(metadata.get("canonical_issuer_key")),
        "event_chain_group": _clean_identifier(metadata.get("event_chain_group")),
        "source": source,
        "source_priority": SOURCE_PRIORITY[source],
        "source_path": str(source_path),
        "source_row": row_number,
        "weak_rule": _clean_identifier(metadata.get("weak_rule")),
    }


class CanonicalIssuerLookup:
    """Fail-closed sample/event lookup for a frozen canonical issuer map."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.rows = read_jsonl(path)
        self.by_sample: dict[str, dict[str, Any]] = {}
        self.by_event: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.quality_counts: Counter[str] = Counter()
        for number, row in enumerate(self.rows, 1):
            sample_id = _clean_identifier(row.get("sample_id"))
            event_id = _clean_identifier(row.get("event_id") or row.get("source_event_id"))
            if not sample_id or not event_id:
                raise ValueError(f"{path}:{number}: canonical issuer row missing sample_id or event_id")
            if sample_id in self.by_sample:
                raise ValueError(f"{path}:{number}: duplicate canonical issuer sample_id {sample_id}")
            normalized = {
                **row,
                "sample_id": sample_id,
                "event_id": event_id,
                "canonical_issuer_key": _clean_identifier(row.get("canonical_issuer_key")),
            }
            self.by_sample[sample_id] = normalized
            self.by_event[event_id].append(normalized)
            self.quality_counts[str(row.get("resolution_quality") or "UNSPECIFIED")] += 1
        for event_id, matches in self.by_event.items():
            keys = {match["canonical_issuer_key"] for match in matches}
            if len(keys) > 1:
                raise ValueError(
                    f"{path}: conflicting canonical issuer keys for event_id {event_id}: "
                    f"{sorted(str(key) for key in keys)}"
                )

    def resolve(
        self, sample_id: Any, event_id: Any, *, context: str, require_resolved: bool = False,
    ) -> str | None:
        sample = _clean_identifier(sample_id)
        event = _clean_identifier(event_id)
        matches: list[dict[str, Any]] = []
        if sample and sample in self.by_sample:
            match = self.by_sample[sample]
            if event and match["event_id"] != event:
                raise ValueError(
                    f"{context}: canonical issuer sample/event conflict for {sample}: "
                    f"{match['event_id']} != {event}"
                )
            matches.append(match)
        if event:
            matches.extend(self.by_event.get(event, []))
        if not matches:
            raise ValueError(
                f"{context}: missing canonical issuer mapping for sample_id={sample!r}, event_id={event!r}"
            )
        keys = {match["canonical_issuer_key"] for match in matches}
        if len(keys) != 1:
            raise ValueError(
                f"{context}: conflicting canonical issuer mappings for "
                f"sample_id={sample!r}, event_id={event!r}: {sorted(str(key) for key in keys)}"
            )
        key = next(iter(keys))
        if require_resolved and not key:
            raise ValueError(
                f"{context}: unresolved canonical issuer for "
                f"sample_id={sample!r}, event_id={event!r}"
            )
        return key

    def resolution_quality(self, sample_id: Any, event_id: Any, *, context: str) -> str:
        sample = _clean_identifier(sample_id)
        event = _clean_identifier(event_id)
        matches: list[dict[str, Any]] = []
        if sample and sample in self.by_sample:
            match = self.by_sample[sample]
            if event and match["event_id"] != event:
                raise ValueError(
                    f"{context}: canonical issuer sample/event conflict for {sample}: "
                    f"{match['event_id']} != {event}"
                )
            matches.append(match)
        if event:
            matches.extend(self.by_event.get(event, []))
        if not matches:
            raise ValueError(
                f"{context}: missing canonical issuer mapping for "
                f"sample_id={sample!r}, event_id={event!r}"
            )
        qualities = {str(match.get("resolution_quality") or "UNSPECIFIED") for match in matches}
        if len(qualities) != 1:
            raise ValueError(
                f"{context}: conflicting canonical issuer qualities for "
                f"sample_id={sample!r}, event_id={event!r}: {sorted(qualities)}"
            )
        return next(iter(qualities))

    def summary(self) -> dict[str, Any]:
        resolved = sum(1 for row in self.rows if _clean_identifier(row.get("canonical_issuer_key")))
        return {
            "rows": len(self.rows),
            "resolved_rows": resolved,
            "unresolved_rows": len(self.rows) - resolved,
            "quality_counts": dict(sorted(self.quality_counts.items())),
        }


def _strict_sets(
    paths: Iterable[Path], canonical_issuers: CanonicalIssuerLookup | None = None,
) -> dict[str, set[str]]:
    sets = {key: set() for key in (
        "sample_id", "event_id", "entity_group", "canonical_issuer_key", "event_chain_group", "hash",
    )}
    for path in paths:
        for number, row in enumerate(read_jsonl(path), 1):
            sample_id = _clean_identifier(row.get("sample_id"))
            event_id = _clean_identifier(row.get("source_event_id") or row.get("event_id"))
            canonical_key = _clean_identifier(row.get("canonical_issuer_key"))
            if canonical_issuers is not None:
                mapped = canonical_issuers.resolve(
                    sample_id, event_id, context=f"{path}:{number} strict index",
                    require_resolved=True,
                )
                if canonical_key and mapped != canonical_key:
                    raise ValueError(
                        f"{path}:{number}: strict canonical issuer conflict: {canonical_key} != {mapped}"
                    )
                canonical_key = mapped
            mappings = {
                "sample_id": [sample_id],
                "event_id": [event_id],
                "entity_group": [row.get("entity_group")],
                "canonical_issuer_key": [canonical_key],
                "event_chain_group": [row.get("event_chain_group")],
                "hash": [
                    row.get("content_sha256"),
                    row.get("provider_text_sha256"),
                    row.get("provider_text_sha256_v1"),
                    row.get("source_text_sha256"),
                    row.get("semantic_context_sha256"),
                ],
            }
            for key, values in mappings.items():
                sets[key].update(filter(None, (_clean_identifier(value) for value in values)))
    return sets


def _leak_reasons(row: dict[str, Any], strict: dict[str, set[str]]) -> list[str]:
    checks = {
        "sample_id": row["sample_id"],
        "event_id": row["event_id"],
        "entity_group": row["entity_group"],
        "canonical_issuer_key": row.get("canonical_issuer_key"),
        "event_chain_group": row["event_chain_group"],
        "hash": row["content_sha256"],
    }
    return [key for key, value in checks.items() if value and value in strict[key]]


class UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, first: int, second: int) -> None:
        a, b = self.find(first), self.find(second)
        if a != b:
            self.parent[b] = a


def _components(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    union = UnionFind(len(rows))
    seen: dict[tuple[str, str], int] = {}
    for index, row in enumerate(rows):
        for field in (
            "sample_id", "event_id", "entity_group", "canonical_issuer_key",
            "event_chain_group", "content_sha256",
        ):
            value = row.get(field)
            if not value:
                continue
            key = (field, str(value))
            if key in seen:
                union.union(index, seen[key])
            else:
                seen[key] = index
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(rows):
        grouped[union.find(index)].append(row)
    return list(grouped.values())


def _split_component(component: list[dict[str, Any]]) -> str:
    identity = sorted(
        f"{field}:{row[field]}"
        for row in component
        for field in (
            "sample_id", "event_id", "entity_group", "canonical_issuer_key",
            "event_chain_group", "content_sha256",
        )
        if row.get(field)
    )
    rank = int(hashlib.sha256(f"{SPLIT_SALT}:{'|'.join(identity)}".encode()).hexdigest()[:16], 16)
    return "DEV" if rank % 100 < 15 else "TRAIN"


def _prepared(row: dict[str, Any], split: str) -> dict[str, Any]:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": stable_json(row["content"])},
            {"role": "assistant", "content": stable_json(row["target"])},
        ],
        "metadata": {
            "sample_id": row["sample_id"],
            "event_id": row["event_id"],
            "entity_group": row["entity_group"],
            **({"canonical_issuer_key": row["canonical_issuer_key"]}
               if row.get("canonical_issuer_key") else {}),
            "event_chain_group": row["event_chain_group"],
            "content_sha256": row["content_sha256"],
            "split": split,
            "label_provenance": row["source"],
            "label_classification": "WEAK_SUPERVISION_NOT_HUMAN_GOLD",
            "weak_rule": row["weak_rule"],
            "human_gold_claimed": False,
            "qwen_prediction_included": False,
            "post_event_market_data_included": False,
            "evidence_state_used_as_model_target": False,
        },
    }


def build_dataset(
    *,
    dual_consensus: list[Path],
    ai_assisted: list[Path],
    deterministic_weak: list[Path],
    strict_indices: list[Path],
    output_dir: Path,
    explicit_exclusions: list[Path] | None = None,
    quality_exclusions: list[Path] | None = None,
    canonical_source_map: Path | None = None,
    canonical_pool: Path | None = None,
    canonical_issuer_map: Path | None = None,
) -> dict[str, Any]:
    explicit_exclusions = explicit_exclusions or []
    quality_exclusions = quality_exclusions or []
    inputs = [
        *( (path, "DUAL_REVIEW_CONSENSUS") for path in dual_consensus ),
        *( (path, "AI_ASSISTED_REFERENCE") for path in ai_assisted ),
        *( (path, "DETERMINISTIC_WEAK_RULE") for path in deterministic_weak ),
    ]
    candidates: list[dict[str, Any]] = []
    input_counts: Counter[str] = Counter()
    for path, source in inputs:
        for row_number, row in enumerate(read_jsonl(path), 1):
            candidates.append(_candidate(row, source, path, row_number))
            input_counts[source] += 1

    canonical_rejoined = 0
    if bool(canonical_source_map) != bool(canonical_pool):
        raise ValueError("canonical_source_map and canonical_pool must be supplied together")
    if canonical_source_map and canonical_pool:
        event_to_sample = {
            str(row.get("event_id") or ""): str(row.get("sample_id") or "")
            for row in read_jsonl(canonical_source_map)
            if row.get("event_id") and row.get("sample_id")
        }
        pool_by_sample = {
            str(row.get("sample_id") or ""): row
            for row in read_jsonl(canonical_pool)
            if row.get("sample_id")
        }
        for row in candidates:
            if row["source"] != "DETERMINISTIC_WEAK_RULE" or not row.get("event_id"):
                continue
            source_sample_id = event_to_sample.get(str(row["event_id"]))
            canonical = pool_by_sample.get(str(source_sample_id or ""))
            if not canonical:
                continue
            row["canonical_source_sample_id"] = source_sample_id
            row["legacy_entity_group"] = row.get("entity_group")
            row["legacy_event_chain_group"] = row.get("event_chain_group")
            row["entity_group"] = _clean_identifier(canonical.get("entity_group"))
            row["event_chain_group"] = _clean_identifier(canonical.get("event_chain_group"))
            canonical_rejoined += 1

    canonical_issuers = CanonicalIssuerLookup(canonical_issuer_map) if canonical_issuer_map else None
    canonical_candidate_counts: Counter[str] = Counter()
    if canonical_issuers is not None:
        for row in candidates:
            mapped = canonical_issuers.resolve(
                row.get("sample_id"), row.get("event_id"),
                context=f"{row['source_path']}:{row['source_row']} candidate",
            )
            existing = _clean_identifier(row.get("canonical_issuer_key"))
            if existing and mapped != existing:
                raise ValueError(
                    f"{row['source_path']}:{row['source_row']}: candidate canonical issuer conflict: "
                    f"{existing} != {mapped}"
                )
            row["canonical_issuer_key"] = mapped
            quality = canonical_issuers.resolution_quality(
                row.get("sample_id"), row.get("event_id"),
                context=f"{row['source_path']}:{row['source_row']} candidate quality",
            )
            row["canonical_issuer_resolution_quality"] = quality
            if not mapped:
                canonical_candidate_counts["UNRESOLVED"] += 1
            elif quality.startswith("STRONG_"):
                canonical_candidate_counts["STRONG_RESOLVED"] += 1
            else:
                canonical_candidate_counts["PROVISIONAL_RESOLVED"] += 1

    strict = _strict_sets(strict_indices, canonical_issuers)
    explicit_sample_ids: set[str] = set()
    explicit_event_ids: set[str] = set()
    for path in explicit_exclusions:
        raw = path.read_text(encoding="utf-8-sig").strip()
        if not raw:
            continue
        values = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines() if line.strip()]
        for value in values:
            if not isinstance(value, dict):
                raise ValueError(f"explicit exclusion is not an object: {path}")
            sample_id = _clean_identifier(value.get("sample_id") or value.get("hardcase_sample_id"))
            event_id = _clean_identifier(value.get("event_id") or value.get("source_event_id"))
            if sample_id:
                explicit_sample_ids.add(sample_id)
            if event_id:
                explicit_event_ids.add(event_id)
    quality_sample_ids: set[str] = set()
    quality_event_ids: set[str] = set()
    for path in quality_exclusions:
        raw = path.read_text(encoding="utf-8-sig").strip()
        if not raw:
            continue
        values = json.loads(raw) if raw.startswith("[") else [json.loads(line) for line in raw.splitlines() if line.strip()]
        for value in values:
            if not isinstance(value, dict):
                raise ValueError(f"quality exclusion is not an object: {path}")
            sample_id = _clean_identifier(value.get("sample_id"))
            event_id = _clean_identifier(value.get("event_id"))
            if sample_id:
                quality_sample_ids.add(sample_id)
            if event_id:
                quality_event_ids.add(event_id)
    leakage: list[dict[str, Any]] = []
    leak_free: list[dict[str, Any]] = []
    for row in candidates:
        reasons = _leak_reasons(row, strict)
        if canonical_issuers is not None and not row.get("canonical_issuer_key"):
            reasons.append("canonical_issuer_unresolved")
        if (
            canonical_issuers is not None
            and row.get("canonical_issuer_key")
            and not str(row.get("canonical_issuer_resolution_quality") or "").startswith("STRONG_")
        ):
            reasons.append("canonical_issuer_not_strong")
        if row.get("sample_id") in explicit_sample_ids:
            reasons.append("explicit_sample_id")
        if row.get("event_id") in explicit_event_ids:
            reasons.append("explicit_event_id")
        if reasons:
            leakage.append({
                "sample_id": row["sample_id"], "event_id": row["event_id"],
                "canonical_issuer_key": row.get("canonical_issuer_key"),
                "source": row["source"], "source_path": row["source_path"],
                "source_row": row["source_row"], "reasons": reasons,
            })
        else:
            leak_free.append(row)

    quality_removed: list[dict[str, Any]] = []
    quality_free: list[dict[str, Any]] = []
    for row in leak_free:
        reasons: list[str] = []
        if row.get("sample_id") in quality_sample_ids:
            reasons.append("rationale_source_sanity_sample_id")
        if row.get("event_id") in quality_event_ids:
            reasons.append("rationale_source_sanity_event_id")
        if reasons:
            quality_removed.append({
                "sample_id": row["sample_id"], "event_id": row["event_id"],
                "source": row["source"], "reasons": reasons,
            })
        else:
            quality_free.append(row)

    policy_removed: list[dict[str, Any]] = []
    policy_free: list[dict[str, Any]] = []
    for row in quality_free:
        if row["source"] != "AI_ASSISTED_REFERENCE":
            policy_free.append(row)
            continue
        text = "\n".join([
            str(row["content"].get("headline") or ""),
            str(row["content"].get("summary") or ""),
            *[str(item.get("passage") or "") for item in row["content"].get("passages") or []],
        ])
        policy_label, policy_reason = _risk_first_policy(text)
        policy_priority = {"RISK_REVIEW": "PRIORITY_REVIEW", "NON_TARGET": "ROUTINE"}.get(str(policy_label or ""))
        target_priority = row["target"]["semantic_priority"]
        if policy_priority and target_priority in {"PRIORITY_REVIEW", "ROUTINE"} and policy_priority != target_priority:
            policy_removed.append({
                "sample_id": row["sample_id"], "event_id": row["event_id"],
                "source": row["source"], "target": row["target"],
                "policy_label": policy_label, "policy_reason": policy_reason,
            })
        else:
            policy_free.append(row)

    # Contradictions are unsafe only when they refer to the same sample, event,
    # or normalized content.  Different events for one issuer may legitimately
    # have different labels and are kept together only for split isolation.
    conflict_keys: set[tuple[str, str]] = set()
    by_identity: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in policy_free:
        for field in ("sample_id", "event_id", "content_sha256"):
            if row.get(field):
                by_identity[(field, str(row[field]))].add(row["target_key"])
    conflict_keys.update(key for key, values in by_identity.items() if len(values) > 1)
    conflicts: list[dict[str, Any]] = []
    conflict_free: list[dict[str, Any]] = []
    for row in policy_free:
        matched = [f"{field}:{row[field]}" for field in ("sample_id", "event_id", "content_sha256") if row.get(field) and (field, str(row[field])) in conflict_keys]
        if matched:
            conflicts.append({
                "sample_id": row["sample_id"], "event_id": row["event_id"],
                "source": row["source"], "target": row["target"], "conflict_keys": matched,
            })
        else:
            conflict_free.append(row)

    # Select one highest-confidence copy for exact event/content duplicates.
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    for row in sorted(conflict_free, key=lambda item: (-item["source_priority"], item["source_path"], item["source_row"])):
        key = (str(row.get("event_id") or ""), row["content_sha256"])
        if key in chosen:
            duplicates.append({
                "kept_source": chosen[key]["source"], "dropped_source": row["source"],
                "sample_id": row["sample_id"], "event_id": row["event_id"],
                "content_sha256": row["content_sha256"],
            })
        else:
            chosen[key] = row
    unique = list(chosen.values())

    prepared = {"TRAIN": [], "DEV": []}
    for component in _components(unique):
        split = _split_component(component)
        prepared[split].extend(_prepared(row, split) for row in component)
    for rows in prepared.values():
        rows.sort(key=lambda row: (str(row["metadata"].get("sample_id") or ""), row["metadata"]["content_sha256"]))

    train_canonical_issuers = {
        str(row["metadata"]["canonical_issuer_key"])
        for row in prepared["TRAIN"] if row["metadata"].get("canonical_issuer_key")
    }
    dev_canonical_issuers = {
        str(row["metadata"]["canonical_issuer_key"])
        for row in prepared["DEV"] if row["metadata"].get("canonical_issuer_key")
    }
    train_dev_canonical_overlap = train_canonical_issuers & dev_canonical_issuers
    if train_dev_canonical_overlap:
        raise RuntimeError(
            "canonical issuer component split leaked across train/dev: "
            + ",".join(sorted(train_dev_canonical_overlap)[:10])
        )
    train_dev_unresolved_canonical_rows = sum(
        1 for split in ("TRAIN", "DEV") for row in prepared[split]
        if canonical_issuers is not None and not row["metadata"].get("canonical_issuer_key")
    )
    if train_dev_unresolved_canonical_rows:
        raise RuntimeError(
            f"train/dev contains unresolved canonical issuers: {train_dev_unresolved_canonical_rows}"
        )

    exposure_rows: list[dict[str, Any]] = []
    for split in ("TRAIN", "DEV"):
        for row in prepared[split]:
            metadata = row["metadata"]
            exposure_rows.append({
                "schema_version": 1,
                "sample_id": metadata.get("sample_id"),
                "event_id": metadata.get("event_id"),
                "entity_group": metadata.get("entity_group"),
                "canonical_issuer_key": metadata.get("canonical_issuer_key"),
                "event_chain_group": metadata.get("event_chain_group"),
                "content_sha256": metadata.get("content_sha256"),
                "exposure_split": split,
            })
    exposure_rows.sort(key=lambda row: (str(row.get("sample_id") or ""), str(row.get("event_id") or "")))

    balanced: list[dict[str, Any]] = []
    for row in prepared["TRAIN"]:
        target = json.loads(row["messages"][-1]["content"])
        repeats = PAIR_MULTIPLIERS.get((target["materiality"], target["polarity"]), 1)
        for repeat in range(repeats):
            copy = json.loads(json.dumps(row))
            if repeat:
                copy["metadata"]["training_repeat"] = repeat
                copy["metadata"]["origin_sample_id"] = row["metadata"].get("sample_id")
            balanced.append(copy)

    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "train_unique": output_dir / "qwen_core_v4_train_unique.jsonl",
        "train_balanced": output_dir / "qwen_core_v4_train_balanced.jsonl",
        "dev": output_dir / "qwen_core_v4_dev.jsonl",
        "leakage_exclusions": output_dir / "leakage_exclusions.jsonl",
        "conflict_exclusions": output_dir / "conflict_exclusions.jsonl",
        "duplicate_exclusions": output_dir / "duplicate_exclusions.jsonl",
        "quality_exclusions": output_dir / "quality_exclusions.jsonl",
        "policy_exclusions": output_dir / "policy_exclusions.jsonl",
        "training_exposure_registry": output_dir / "training_exposure_registry.jsonl",
    }
    hashes = {
        "train_unique": write_jsonl(files["train_unique"], prepared["TRAIN"]),
        "train_balanced": write_jsonl(files["train_balanced"], balanced),
        "dev": write_jsonl(files["dev"], prepared["DEV"]),
        "leakage_exclusions": write_jsonl(files["leakage_exclusions"], leakage),
        "conflict_exclusions": write_jsonl(files["conflict_exclusions"], conflicts),
        "duplicate_exclusions": write_jsonl(files["duplicate_exclusions"], duplicates),
        "quality_exclusions": write_jsonl(files["quality_exclusions"], quality_removed),
        "policy_exclusions": write_jsonl(files["policy_exclusions"], policy_removed),
        "training_exposure_registry": write_jsonl(files["training_exposure_registry"], exposure_rows),
    }
    atomic_write(
        output_dir / "training_exposure_registry.jsonl.sha256",
        (hashes["training_exposure_registry"] + "  training_exposure_registry.jsonl\n").encode("ascii"),
    )

    def pair_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        return dict(sorted(Counter(
            f"{target['materiality']}|{target['polarity']}"
            for row in rows for target in [json.loads(row["messages"][-1]["content"])]
        ).items()))

    manifest = {
        "schema_version": 1,
        "dataset_contract": DATASET_CONTRACT,
        "semantic_contract": QWEN_RISK_CONTRACT_VERSION,
        "label_classification": "WEAK_SUPERVISION_NOT_HUMAN_GOLD",
        "split_salt": SPLIT_SALT,
        "input_counts": dict(sorted(input_counts.items())),
        "input_sha256": {str(path): sha256_file(path) for path, _ in inputs},
        "strict_index_sha256": {str(path): sha256_file(path) for path in strict_indices},
        "canonical_mapping_sha256": {
            **({"source_map": sha256_file(canonical_source_map)} if canonical_source_map else {}),
            **({"pool": sha256_file(canonical_pool)} if canonical_pool else {}),
            **({"issuer_map": sha256_file(canonical_issuer_map)} if canonical_issuer_map else {}),
        },
        "canonical_issuer_map": ({
            "path": str(canonical_issuer_map),
            "sha256": sha256_file(canonical_issuer_map),
            **canonical_issuers.summary(),
            "candidate_join_counts": dict(sorted(canonical_candidate_counts.items())),
        } if canonical_issuer_map and canonical_issuers else None),
        "canonical_rejoined_rows": canonical_rejoined,
        "explicit_exclusion_sha256": {str(path): sha256_file(path) for path in explicit_exclusions},
        "quality_exclusion_sha256": {str(path): sha256_file(path) for path in quality_exclusions},
        "explicit_exclusion_counts": {
            "sample_id": len(explicit_sample_ids), "event_id": len(explicit_event_ids),
        },
        "strict_set_counts": {key: len(values) for key, values in strict.items()},
        "candidate_rows": len(candidates),
        "leakage_excluded_rows": len(leakage),
        "canonical_unresolved_excluded_rows": sum(
            "canonical_issuer_unresolved" in row.get("reasons", []) for row in leakage
        ),
        "canonical_provisional_excluded_rows": sum(
            "canonical_issuer_not_strong" in row.get("reasons", []) for row in leakage
        ),
        "quality_excluded_rows": len(quality_removed),
        "policy_excluded_rows": len(policy_removed),
        "conflict_excluded_rows": len(conflicts),
        "duplicate_excluded_rows": len(duplicates),
        "unique_rows": len(unique),
        "train_unique_rows": len(prepared["TRAIN"]),
        "train_balanced_rows": len(balanced),
        "dev_rows": len(prepared["DEV"]),
        "component_count": len(_components(unique)),
        "train_dev_canonical_issuer_overlap": len(train_dev_canonical_overlap),
        "train_dev_unresolved_canonical_issuer_rows": train_dev_unresolved_canonical_rows,
        "training_exposure_registry_rows": len(exposure_rows),
        "training_exposure_registry_contract": {
            "audience": "OWNER_ONLY_BENCHMARK_ISOLATION",
            "labels_included": False,
            "message_text_included": False,
            "balanced_repeats_included": False,
            "source_dataset_sha256_omitted_due_to_manifest_cycle": True,
        },
        "train_pair_counts_unique": pair_counts(prepared["TRAIN"]),
        "train_pair_counts_effective": pair_counts(balanced),
        "dev_pair_counts": pair_counts(prepared["DEV"]),
        "pair_multipliers": {"|".join(key): value for key, value in PAIR_MULTIPLIERS.items()},
        "output_sha256": hashes,
        "leakage_policy": [
            "sample_id", "event_id", "entity_group", "canonical_issuer_key",
            "event_chain_group", "content_and_provider_hashes",
        ],
        "qwen_predictions_used": False,
        "market_outcomes_used": False,
        "evidence_state_used_as_target": False,
        "production_model_changed": False,
    }
    manifest_path = output_dir / "manifest.json"
    atomic_write(manifest_path, (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    atomic_write(output_dir / "manifest.json.sha256", (sha256_file(manifest_path) + "  manifest.json\n").encode("ascii"))
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dual-consensus", type=Path, action="append", default=[])
    parser.add_argument("--ai-assisted", type=Path, action="append", default=[])
    parser.add_argument("--deterministic-weak", type=Path, action="append", default=[])
    parser.add_argument("--strict-index", type=Path, action="append", required=True)
    parser.add_argument("--explicit-exclusion", type=Path, action="append", default=[])
    parser.add_argument("--quality-exclusion", type=Path, action="append", default=[])
    parser.add_argument("--canonical-source-map", type=Path)
    parser.add_argument("--canonical-pool", type=Path)
    parser.add_argument("--canonical-issuer-map", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = build_dataset(
        dual_consensus=[path.resolve() for path in args.dual_consensus],
        ai_assisted=[path.resolve() for path in args.ai_assisted],
        deterministic_weak=[path.resolve() for path in args.deterministic_weak],
        strict_indices=[path.resolve() for path in args.strict_index],
        output_dir=args.output_dir.resolve(),
        explicit_exclusions=[path.resolve() for path in args.explicit_exclusion],
        quality_exclusions=[path.resolve() for path in args.quality_exclusion],
        canonical_source_map=args.canonical_source_map.resolve() if args.canonical_source_map else None,
        canonical_pool=args.canonical_pool.resolve() if args.canonical_pool else None,
        canonical_issuer_map=args.canonical_issuer_map.resolve() if args.canonical_issuer_map else None,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
