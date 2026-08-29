#!/usr/bin/env python3
"""Build a leakage-resistant ms-swift SFT corpus from AI adjudications.

The input is an independent adjudication JSONL.  It must contain the exact
semantic source content and final materiality/polarity labels, but it must not
contain any prior Qwen prediction.  Entity, event-chain, and exact normalized
content links are collapsed into connected components before splitting, so a
transitive relationship can never straddle TRAIN, DEV, and TEST.

Only TRAIN is resampled.  DEV and TEST are immutable unique-row evaluations.
The output is an experiment corpus, not a human-gold claim and not a production
model mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import unicodedata
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.qwen_risk_contract import (  # noqa: E402
    QWEN_RISK_CONTRACT_VERSION,
    QWEN_RISK_SYSTEM_PROMPT,
    expected_semantic_payload,
    normalize_qwen_risk_content,
)
from app.models.qwen_risk_contract_v2 import (  # noqa: E402
    QWEN_RISK_CONTRACT_V2_VERSION,
    V2_REQUIRED_FIELDS,
    semantic_priority_v2,
    validate_semantic_v2_payload,
)


CONTRACT_VERSION = "qwen-semantic-ai-adjudicated-group-split-v4"
TARGET_CONTRACTS = frozenset({"core-v1", "full-v2"})
AGREEMENT_POLICIES = frozenset({"core", "all", "none"})
AGREEMENT_FIELDS = (
    "materiality",
    "polarity",
    "impact_strength",
    "event_realization",
    "subject_relation",
    "risk_status",
    "novelty",
)
SPLIT_ALGORITHM = "connected-component-greedy-stratification-v1"
RESAMPLING_POLICY = "TRAIN_ONLY_SEMANTIC_PAIR_CAPPED_REPEAT_V1"
DEFAULT_SPLIT_SALT = "finance-radar-qwen-semantic-v4-20260829"
DEFAULT_SPLIT_RATIOS = {"TRAIN": 0.70, "DEV": 0.15, "TEST": 0.15}
DEFAULT_MULTIPLIERS = {
    "material_adverse": 3,
    "positive": 4,
    "mixed": 4,
}
OUTPUT_NAMES = {
    "train": "qwen_risk_sft_train.jsonl",
    "train_balanced": "qwen_risk_sft_train_balanced.jsonl",
    "dev": "qwen_risk_sft_dev.jsonl",
    "test": "qwen_risk_sft_test.jsonl",
    "split_audit": "qwen_risk_v4_split_audit.jsonl",
}
MANIFEST_NAME = "qwen_risk_v4_manifest.json"
PROHIBITED_KEYS = frozenset(
    {
        "qwen_prediction",
        "qwen_predictions",
        "qwen_output",
        "prior_qwen_output",
        "candidate_prediction",
        "old_model_output",
        "prior_model_output",
        "model_prediction",
        "model_predictions",
        "predicted_materiality",
        "predicted_polarity",
        "semantic_prediction",
    }
)
V4_SYSTEM_PROMPT = (
    "你是金融雷达的语义风险分类器。只根据给定来源文本判断主要受影响主体的事件实现状态、"
    "主体关系、风险状态、新颖性、独立影响强度、做空风险重大性与极性；不判断证据真假，不补充外部事实，"
    "不使用价格结果，不给投资建议。假设条款、第三方事件、整改期、已解决事项与无新事实的"
    "重复报道不能仅凭关键词判成重大负面。只输出指定的严格 JSON。"
)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _normalized_group(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.split()).casefold()


def _walk_keys(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        for key, nested in value.items():
            yield str(key).strip().casefold()
            yield from _walk_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_keys(nested)


def _read_input(path: Path) -> tuple[list[dict[str, Any]], bytes]:
    raw = path.read_bytes()
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"input line {number} is not an object")
        prohibited = sorted(set(_walk_keys(value)) & PROHIBITED_KEYS)
        if prohibited:
            raise ValueError(
                f"input line {number} contains prohibited prior-model fields: "
                + ",".join(prohibited)
            )
        rows.append(value)
    if not rows:
        raise ValueError("input contains no adjudicated rows")
    return rows, raw


def _read_provider_input(path: Path) -> tuple[dict[str, dict[str, Any]], bytes]:
    raw = path.read_bytes()
    result: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"provider input line {number} is not an object")
        sample_id = str(row.get("sample_id") or "").strip()
        if not sample_id or sample_id in result:
            raise ValueError(
                f"provider input line {number} has duplicate or missing sample_id"
            )
        content = row.get("content")
        if not isinstance(content, dict) or not content:
            raise ValueError(f"provider input line {number} has invalid content")
        prohibited = sorted(set(_walk_keys(content)) & PROHIBITED_KEYS)
        if prohibited:
            raise ValueError(
                f"provider input line {number} contains prohibited prior-model fields: "
                + ",".join(prohibited)
            )
        computed = _sha256_bytes(stable_json(content).encode("utf-8"))
        for field in ("provider_text_sha256", "text_sha256", "input_sha256"):
            declared = str(row.get(field) or "").strip().lower()
            if declared and declared != computed:
                raise ValueError(f"provider input {field} mismatch: {sample_id}")
        result[sample_id] = {
            "sample_id": sample_id,
            "content": content,
            "provider_text_sha256": computed,
        }
    if not result:
        raise ValueError("provider input contains no rows")
    return result, raw


def _read_source_index(path: Path) -> tuple[dict[str, dict[str, Any]], bytes]:
    raw = path.read_bytes()
    result: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(raw.decode("utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"source index line {number} is not an object")
        sample_id = str(row.get("sample_id") or "").strip()
        if not sample_id or sample_id in result:
            raise ValueError(f"source index line {number} has duplicate or missing sample_id")
        content = row.get("content")
        if content is not None and (not isinstance(content, dict) or not content):
            raise ValueError(f"source index line {number} has invalid content")
        if isinstance(content, dict):
            prohibited = sorted(set(_walk_keys(content)) & PROHIBITED_KEYS)
            if prohibited:
                raise ValueError(
                    f"source index line {number} contains prohibited prior-model fields: "
                    + ",".join(prohibited)
                )
        entity_group = _normalized_group(row.get("entity_group"))
        event_chain_group = _normalized_group(row.get("event_chain_group"))
        if not entity_group or not event_chain_group:
            raise ValueError(f"source index line {number} has missing group identity")
        legacy_hash = str(row.get("text_sha256") or "").strip().lower()
        provider_text_sha256 = str(
            row.get("provider_text_sha256") or legacy_hash
        ).strip().lower()
        source_text_sha256 = str(
            row.get("source_text_sha256") or legacy_hash
        ).strip().lower()
        if not provider_text_sha256 or not source_text_sha256:
            raise ValueError(
                f"source index line {number} is missing provider/source text hashes"
            )
        if isinstance(content, dict):
            computed = _sha256_bytes(stable_json(content).encode("utf-8"))
            if provider_text_sha256 != computed:
                raise ValueError(f"source index provider_text_sha256 mismatch: {sample_id}")
            if source_text_sha256 != computed:
                raise ValueError(f"source index source_text_sha256 mismatch: {sample_id}")
        declared_input = str(row.get("input_sha256") or "").strip().lower()
        if declared_input and declared_input != provider_text_sha256:
            raise ValueError(f"source index input_sha256 mismatch: {sample_id}")
        result[sample_id] = {
            "sample_id": sample_id,
            "event_id": str(
                row.get("event_id") or row.get("source_event_id") or ""
            ).strip()
            or None,
            "content": content,
            "provider_text_sha256": provider_text_sha256,
            "source_text_sha256": source_text_sha256,
            "entity_group": entity_group,
            "event_chain_group": event_chain_group,
        }
    if not result:
        raise ValueError("source index contains no rows")
    return result, raw


def _normalized_v2_target(value: Any, *, line_number: int) -> dict[str, Any]:
    issues = validate_semantic_v2_payload(value)
    if issues:
        raise ValueError(
            f"input line {line_number} has invalid v2 final: " + ",".join(issues)
        )
    assert isinstance(value, dict)
    return {
        **{
            field: str(value[field]).strip().upper()
            for field in (
                "materiality",
                "polarity",
                "impact_strength",
                "event_realization",
                "subject_relation",
                "risk_status",
                "novelty",
            )
        },
        "reason_codes": sorted(str(code).strip().upper() for code in value["reason_codes"]),
        "brief_reason": " ".join(str(value["brief_reason"]).split()),
    }


def _validate_and_normalize(
    rows: list[dict[str, Any]],
    source_index: dict[str, dict[str, Any]] | None,
    provider_input: dict[str, dict[str, Any]] | None,
    agreement_policy: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    prepared: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    agreement_counts: Counter[str] = Counter()
    for number, row in enumerate(rows, 1):
        sample_id = str(row.get("sample_id") or "").strip()
        if not sample_id or sample_id in seen_ids:
            raise ValueError(f"input line {number} has duplicate or missing sample_id")
        seen_ids.add(sample_id)
        nested_final = row.get("final")
        source: dict[str, Any] | None = None
        provider: dict[str, Any] | None = None
        if source_index is not None:
            if sample_id not in source_index:
                raise ValueError(f"input line {number} is missing from source index")
            source = source_index[sample_id]
        if provider_input is not None:
            if sample_id not in provider_input:
                raise ValueError(f"input line {number} is missing from provider input")
            provider = provider_input[sample_id]
        agreement_counts["input_rows"] += 1
        if nested_final is not None:
            if source_index is None:
                raise ValueError("nested multiview final requires --source-index")
            assert source is not None
            content = provider["content"] if provider is not None else source.get("content")
            if not isinstance(content, dict):
                raise ValueError(
                    "content-free source index requires --provider-input for nested final"
                )
            target = _normalized_v2_target(nested_final, line_number=number)
            fact = _normalized_v2_target(
                row.get("fact_mechanism_review"), line_number=number
            )
            boundary = _normalized_v2_target(
                row.get("boundary_review"), line_number=number
            )
            core_agreed = all(
                fact[field] == boundary[field] for field in ("materiality", "polarity")
            )
            all_agreed = all(
                fact[field] == boundary[field] for field in AGREEMENT_FIELDS
            )
            reported_core = row.get("first_pass_pair_agreed")
            if not isinstance(reported_core, bool) or reported_core is not core_agreed:
                raise ValueError(f"first-pass agreement flag mismatch: {sample_id}")
            agreement_counts["applicable_rows"] += 1
            agreement_counts["core_agreed_rows"] += int(core_agreed)
            agreement_counts["all_axes_agreed_rows"] += int(all_agreed)
            agreement_passed = (
                agreement_policy == "none"
                or (agreement_policy == "core" and core_agreed)
                or (agreement_policy == "all" and all_agreed)
            )
        else:
            content = row.get("content")
            if not isinstance(content, dict):
                raise ValueError(f"input line {number} has invalid content")
            if source is not None:
                indexed_content = source.get("content")
                if isinstance(indexed_content, dict) and stable_json(content) != stable_json(
                    indexed_content
                ):
                    raise ValueError(f"input/source content mismatch: {sample_id}")
            if provider is not None and stable_json(content) != stable_json(provider["content"]):
                raise ValueError(f"input/provider content mismatch: {sample_id}")
            if source is None:
                source_hash = _sha256_bytes(stable_json(content).encode("utf-8"))
                declared_text = str(row.get("text_sha256") or "").strip().lower()
                if declared_text and declared_text != source_hash:
                    raise ValueError(f"input text_sha256 mismatch: {sample_id}")
                source = {
                    "event_id": str(row.get("event_id") or "").strip() or None,
                    "content": content,
                    "provider_text_sha256": source_hash,
                    "source_text_sha256": source_hash,
                    "entity_group": _normalized_group(row.get("entity_group")),
                    "event_chain_group": _normalized_group(row.get("event_chain_group")),
                }
            flat_target = {field: row.get(field) for field in V2_REQUIRED_FIELDS}
            target = _normalized_v2_target(flat_target, line_number=number)
            core_agreed = None
            all_agreed = None
            agreement_passed = True
            agreement_counts["unavailable_flat_rows"] += 1

        assert source is not None
        source_hash = _sha256_bytes(stable_json(content).encode("utf-8"))
        declared_input = str(row.get("input_sha256") or "").strip().lower()
        if nested_final is not None and not declared_input:
            raise ValueError(f"adjudication input_sha256 missing: {sample_id}")
        if declared_input and declared_input != source_hash:
            raise ValueError(f"adjudication input_sha256 mismatch: {sample_id}")
        if provider is not None and provider["provider_text_sha256"] != source_hash:
            raise ValueError(f"provider input content hash mismatch after join: {sample_id}")
        if source["provider_text_sha256"] != source_hash:
            raise ValueError(f"source provider_text_sha256 mismatch after join: {sample_id}")
        if source["source_text_sha256"] != source_hash:
            raise ValueError(f"source text_sha256 mismatch after join: {sample_id}")
        normalized_content = normalize_qwen_risk_content(content)
        if not any(
            (
                normalized_content.get("headline"),
                normalized_content.get("summary"),
                normalized_content.get("passages"),
            )
        ):
            raise ValueError(f"input line {number} has empty semantic content")
        entity_group = source["entity_group"]
        event_chain_group = source["event_chain_group"]
        if not entity_group or not event_chain_group:
            raise ValueError(f"input line {number} has missing group identity")
        if not agreement_passed:
            # Filtering happens only after all three join/hash bindings above
            # have been verified, so a rejected training row cannot conceal a
            # corrupt or mismatched input record.
            agreement_counts["filtered_rows"] += 1
            continue
        content_json = stable_json(normalized_content)
        prepared.append(
            {
                "sample_id": sample_id,
                "event_id": source["event_id"],
                "content": normalized_content,
                "content_sha256": _sha256_bytes(content_json.encode("utf-8")),
                "provider_text_sha256": source["provider_text_sha256"],
                "source_text_sha256": source["source_text_sha256"],
                "target": target,
                "pair": f"{target['materiality']}|{target['polarity']}",
                "reason_codes": target["reason_codes"],
                "entity_group": entity_group,
                "event_chain_group": event_chain_group,
                "adjudication_model": str(
                    row.get("adjudication_model") or row.get("model") or ""
                ).strip()
                or None,
                "first_pass_core_agreed": core_agreed,
                "first_pass_all_axes_agreed": all_agreed,
                "agreement_policy": agreement_policy,
                "agreement_filter_passed": agreement_passed,
            }
        )
        agreement_counts["kept_rows"] += 1
    if not prepared:
        raise ValueError(f"agreement policy {agreement_policy} filtered all rows")
    return prepared, {
        "policy": agreement_policy,
        "comparison_fields": list(
            AGREEMENT_FIELDS
            if agreement_policy == "all"
            else AGREEMENT_FIELDS[:2]
            if agreement_policy == "core"
            else ()
        ),
        **{
            field: agreement_counts[field]
            for field in (
                "input_rows",
                "applicable_rows",
                "core_agreed_rows",
                "all_axes_agreed_rows",
                "unavailable_flat_rows",
                "kept_rows",
                "filtered_rows",
            )
        },
    }


class _UnionFind:
    def __init__(self, count: int) -> None:
        self.parent = list(range(count))
        self.rank = [0] * count

    def find(self, item: int) -> int:
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, first: int, second: int) -> None:
        left, right = self.find(first), self.find(second)
        if left == right:
            return
        if self.rank[left] < self.rank[right]:
            left, right = right, left
        self.parent[right] = left
        if self.rank[left] == self.rank[right]:
            self.rank[left] += 1


@dataclass(frozen=True)
class _Component:
    component_id: str
    rows: tuple[dict[str, Any], ...]
    pair_counts: Counter[str]
    rank: str

    @property
    def size(self) -> int:
        return len(self.rows)


def _connected_components(rows: list[dict[str, Any]], salt: str) -> list[_Component]:
    union = _UnionFind(len(rows))
    links: dict[tuple[str, str], int] = {}
    for index, row in enumerate(rows):
        for link in (
            ("entity", row["entity_group"]),
            ("chain", row["event_chain_group"]),
            ("content", row["content_sha256"]),
        ):
            previous = links.setdefault(link, index)
            union.union(previous, index)
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, row in enumerate(rows):
        grouped[union.find(index)].append(row)
    components: list[_Component] = []
    for members in grouped.values():
        members.sort(key=lambda row: row["sample_id"])
        sample_ids = [row["sample_id"] for row in members]
        identity = _sha256_bytes(stable_json(sample_ids).encode("utf-8"))[:24]
        components.append(
            _Component(
                component_id=f"component-{identity}",
                rows=tuple(members),
                pair_counts=Counter(row["pair"] for row in members),
                rank=_sha256_bytes(f"{salt}:{identity}".encode("utf-8")),
            )
        )
    return components


def _validate_ratios(ratios: dict[str, float]) -> None:
    if set(ratios) != {"TRAIN", "DEV", "TEST"}:
        raise ValueError("split ratios must contain TRAIN, DEV, and TEST")
    if any(not math.isfinite(value) or value <= 0 for value in ratios.values()):
        raise ValueError("split ratios must be finite and positive")
    if abs(sum(ratios.values()) - 1.0) > 1e-9:
        raise ValueError("split ratios must sum to 1")


def _split_components(
    components: list[_Component], ratios: dict[str, float]
) -> dict[str, list[_Component]]:
    _validate_ratios(ratios)
    if len(components) < 3:
        raise ValueError("at least three disconnected group components are required")
    total_rows = sum(component.size for component in components)
    total_pairs: Counter[str] = Counter()
    for component in components:
        total_pairs.update(component.pair_counts)
    target_rows = {split: total_rows * ratio for split, ratio in ratios.items()}
    target_pairs = {
        split: {pair: count * ratios[split] for pair, count in total_pairs.items()}
        for split in ratios
    }
    assigned: dict[str, list[_Component]] = {split: [] for split in ratios}
    row_counts: Counter[str] = Counter()
    pair_counts = {split: Counter() for split in ratios}

    def rarity(component: _Component) -> float:
        return sum(count / max(total_pairs[pair], 1) for pair, count in component.pair_counts.items())

    ordered = sorted(
        components,
        key=lambda component: (-rarity(component), -component.size, component.rank),
    )

    def cost(candidate: str, component: _Component) -> tuple[float, float, str]:
        value = 0.0
        for split in ratios:
            rows_after = row_counts[split] + (component.size if split == candidate else 0)
            row_target = max(target_rows[split], 1.0)
            value += ((rows_after - row_target) / row_target) ** 2
            for pair, total in total_pairs.items():
                count_after = pair_counts[split][pair]
                if split == candidate:
                    count_after += component.pair_counts[pair]
                pair_target = max(target_pairs[split][pair], 1.0)
                value += 1.75 * ((count_after - pair_target) / pair_target) ** 2
            if rows_after > row_target * 1.20:
                value += 4.0 * ((rows_after - row_target) / row_target) ** 2
        # Stable tie-breaking is independent of input order.
        tie = _sha256_bytes(f"{component.rank}:{candidate}".encode("utf-8"))
        return value, row_counts[candidate] / max(target_rows[candidate], 1.0), tie

    for component in ordered:
        split = min(ratios, key=lambda candidate: cost(candidate, component))
        assigned[split].append(component)
        row_counts[split] += component.size
        pair_counts[split].update(component.pair_counts)

    # A very large connected component can make greedy stratification leave a
    # tiny corpus split empty.  Repair only the empty split by moving the
    # smallest independently ranked component from the fullest donor.
    for empty in [split for split in ratios if not assigned[split]]:
        donors = [split for split in ratios if len(assigned[split]) > 1]
        if not donors:
            raise ValueError("group topology cannot produce three non-empty splits")
        donor = max(donors, key=lambda split: row_counts[split] / target_rows[split])
        moved = min(assigned[donor], key=lambda component: (component.size, component.rank))
        assigned[donor].remove(moved)
        assigned[empty].append(moved)
        row_counts[donor] -= moved.size
        row_counts[empty] += moved.size
        pair_counts[donor].subtract(moved.pair_counts)
        pair_counts[empty].update(moved.pair_counts)
    return assigned


def _sft_row(
    row: dict[str, Any], split: str, component_id: str, target_contract: str
) -> dict[str, Any]:
    if target_contract == "core-v1":
        # The v2 validator has already checked coherence between independent
        # impact magnitude, polarity, and downside materiality.  The production
        # v1 target remains four fields; impact_strength is retained below for
        # audit and positive-strength display rather than added to v1 output.
        assistant_target = expected_semantic_payload(
            row["target"]["materiality"], row["target"]["polarity"]
        )
        system_prompt = QWEN_RISK_SYSTEM_PROMPT
    elif target_contract == "full-v2":
        assistant_target = row["target"]
        system_prompt = V4_SYSTEM_PROMPT
    else:
        raise ValueError(f"unsupported target contract: {target_contract}")
    return {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": stable_json(row["content"])},
            {"role": "assistant", "content": stable_json(assistant_target)},
        ],
        "metadata": {
            "sample_id": row["sample_id"],
            "event_id": row["event_id"],
            "entity_group": row["entity_group"],
            "event_chain_group": row["event_chain_group"],
            "group_component_id": component_id,
            "content_sha256": row["content_sha256"],
            "provider_text_sha256": row["provider_text_sha256"],
            "source_text_sha256": row["source_text_sha256"],
            "reason_codes": row["reason_codes"],
            "adjudication_model": row["adjudication_model"],
            "target_contract": target_contract,
            "agreement_policy": row["agreement_policy"],
            "first_pass_core_agreed": row["first_pass_core_agreed"],
            "first_pass_all_axes_agreed": row["first_pass_all_axes_agreed"],
            "agreement_filter_passed": row["agreement_filter_passed"],
            "adjudication_v2_audit": {
                field: row["target"][field]
                for field in (
                    "event_realization",
                    "impact_strength",
                    "subject_relation",
                    "risk_status",
                    "novelty",
                    "reason_codes",
                    "brief_reason",
                )
            },
            "split": split,
            "label_provenance": "INDEPENDENT_AI_ADJUDICATION_V4",
            "human_gold_claimed": False,
            "qwen_prediction_included": False,
            "evidence_state_used_as_model_target": False,
            "post_event_market_data_included": False,
        },
    }


def _effective_multiplier(target: dict[str, str], multipliers: dict[str, int]) -> int:
    candidates = [1]
    if target["materiality"] == "MATERIAL_ADVERSE":
        candidates.append(multipliers["material_adverse"])
    if target["polarity"] == "POSITIVE":
        candidates.append(multipliers["positive"])
    if target["polarity"] == "MIXED":
        candidates.append(multipliers["mixed"])
    return max(candidates)


def _balanced_training_rows(
    rows: list[dict[str, Any]], multipliers: dict[str, int]
) -> list[dict[str, Any]]:
    if set(multipliers) != set(DEFAULT_MULTIPLIERS) or any(
        not isinstance(value, int) or not 1 <= value <= 16 for value in multipliers.values()
    ):
        raise ValueError("TRAIN multipliers must be integers in [1, 16]")
    balanced: list[dict[str, Any]] = []
    for row in rows:
        target = json.loads(row["messages"][-1]["content"])
        multiplier = _effective_multiplier(target, multipliers)
        sample_id = row["metadata"]["sample_id"]
        for repeat_index in range(multiplier):
            copy = json.loads(json.dumps(row))
            copy["metadata"].update(
                {
                    "origin_sample_id": sample_id,
                    "training_instance_id": (
                        sample_id if repeat_index == 0 else f"{sample_id}::repeat-{repeat_index}"
                    ),
                    "oversample_repeat_index": repeat_index,
                    "oversampled": repeat_index > 0,
                }
            )
            balanced.append(copy)
    return balanced


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return b"".join((stable_json(row) + "\n").encode("utf-8") for row in rows)


def _distribution(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    materiality: Counter[str] = Counter()
    polarity: Counter[str] = Counter()
    pair: Counter[str] = Counter()
    priority: Counter[str] = Counter()
    for row in rows:
        target = json.loads(row["messages"][-1]["content"])
        materiality[target["materiality"]] += 1
        polarity[target["polarity"]] += 1
        pair[f"{target['materiality']}|{target['polarity']}"] += 1
        priority[semantic_priority_v2(target["materiality"], target["polarity"])] += 1
    return {
        "materiality": dict(sorted(materiality.items())),
        "polarity": dict(sorted(polarity.items())),
        "semantic_pair": dict(sorted(pair.items())),
        "semantic_priority": dict(sorted(priority.items())),
    }


def _leakage_audit(prepared: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    fields = ("entity_group", "event_chain_group", "group_component_id", "content_sha256")
    intersections: dict[str, dict[str, int]] = {}
    split_pairs = (("TRAIN", "DEV"), ("TRAIN", "TEST"), ("DEV", "TEST"))
    for first, second in split_pairs:
        key = f"{first}__{second}"
        intersections[key] = {}
        for field in fields:
            left = {row["metadata"][field] for row in prepared[first]}
            right = {row["metadata"][field] for row in prepared[second]}
            intersections[key][field] = len(left & right)
    passed = all(
        count == 0
        for by_field in intersections.values()
        for count in by_field.values()
    )
    return {
        "passed": passed,
        "pairwise_intersection_counts": intersections,
        "axes": list(fields),
        "transitive_connected_components_used": True,
    }


def _write_atomic(output_dir: Path, files: dict[str, bytes]) -> None:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = output_dir.parent / f".{output_dir.name}.building-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        for name, raw in files.items():
            (staging / name).write_bytes(raw)
        staging.rename(output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def prepare(
    *,
    adjudications: Path,
    provider_input: Path | None = None,
    source_index: Path | None = None,
    output_dir: Path,
    target_contract: str = "full-v2",
    agreement_policy: str = "all",
    fixed_split: str | None = None,
    split_salt: str = DEFAULT_SPLIT_SALT,
    ratios: dict[str, float] | None = None,
    multipliers: dict[str, int] | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    if fixed_split is not None and fixed_split != "TEST":
        raise ValueError("fixed split only supports TEST")
    if fixed_split is not None and ratios is not None:
        raise ValueError("fixed TEST split is mutually exclusive with split ratios")
    if fixed_split == "TEST" and agreement_policy != "none":
        raise ValueError("fixed TEST requires --agreement-policy none to prevent selection bias")
    if provider_input is not None and source_index is None:
        raise ValueError("--provider-input requires --source-index")
    ratios = dict(ratios or DEFAULT_SPLIT_RATIOS)
    multipliers = dict(multipliers or DEFAULT_MULTIPLIERS)
    if target_contract not in TARGET_CONTRACTS:
        raise ValueError("target contract must be core-v1 or full-v2")
    if agreement_policy not in AGREEMENT_POLICIES:
        raise ValueError("agreement policy must be core, all, or none")
    _validate_ratios(ratios)
    if not str(split_salt or "").strip():
        raise ValueError("split salt must be nonblank")
    input_rows, input_raw = _read_input(adjudications.resolve())
    source_rows: dict[str, dict[str, Any]] | None = None
    source_raw: bytes | None = None
    provider_rows: dict[str, dict[str, Any]] | None = None
    provider_raw: bytes | None = None
    if provider_input is not None:
        provider_rows, provider_raw = _read_provider_input(provider_input.resolve())
    if source_index is not None:
        source_rows, source_raw = _read_source_index(source_index.resolve())
    rows, agreement_audit = _validate_and_normalize(
        input_rows, source_rows, provider_rows, agreement_policy
    )
    components = _connected_components(rows, split_salt)
    assigned = (
        {"TRAIN": [], "DEV": [], "TEST": components}
        if fixed_split == "TEST"
        else _split_components(components, ratios)
    )
    prepared: dict[str, list[dict[str, Any]]] = {}
    for split, split_components in assigned.items():
        split_rows = [
            _sft_row(row, split, component.component_id, target_contract)
            for component in split_components
            for row in component.rows
        ]
        prepared[split] = sorted(split_rows, key=lambda row: row["metadata"]["sample_id"])
    audit = _leakage_audit(prepared)
    if not audit["passed"]:
        raise AssertionError("group leakage audit failed")
    balanced = (
        []
        if fixed_split == "TEST"
        else _balanced_training_rows(prepared["TRAIN"], multipliers)
    )
    audit_rows = [
        {
            "component_id": component.component_id,
            "split": split,
            "row_count": component.size,
            "sample_ids_sha256": _sha256_bytes(
                stable_json([row["sample_id"] for row in component.rows]).encode("utf-8")
            ),
            "semantic_pair_counts": dict(sorted(component.pair_counts.items())),
        }
        for split, split_components in assigned.items()
        for component in sorted(split_components, key=lambda item: item.component_id)
    ]
    files = (
        {
            OUTPUT_NAMES["test"]: _jsonl_bytes(prepared["TEST"]),
            OUTPUT_NAMES["split_audit"]: _jsonl_bytes(audit_rows),
        }
        if fixed_split == "TEST"
        else {
            OUTPUT_NAMES["train"]: _jsonl_bytes(prepared["TRAIN"]),
            OUTPUT_NAMES["train_balanced"]: _jsonl_bytes(balanced),
            OUTPUT_NAMES["dev"]: _jsonl_bytes(prepared["DEV"]),
            OUTPUT_NAMES["test"]: _jsonl_bytes(prepared["TEST"]),
            OUTPUT_NAMES["split_audit"]: _jsonl_bytes(audit_rows),
        }
    )
    output_hashes = {name: _sha256_bytes(raw) for name, raw in files.items()}
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "contract_version": CONTRACT_VERSION,
        "semantic_contract_version": (
            QWEN_RISK_CONTRACT_VERSION
            if target_contract == "core-v1"
            else QWEN_RISK_CONTRACT_V2_VERSION
        ),
        "adjudication_contract_version": QWEN_RISK_CONTRACT_V2_VERSION,
        "target_contract": target_contract,
        "dataset_role": (
            "EXTERNAL_FIXED_TEST_ONLY" if fixed_split == "TEST" else "MODEL_DEVELOPMENT"
        ),
        "base_model": "Qwen/Qwen2.5-1.5B-Instruct",
        "input": {
            "filename": adjudications.name,
            "sha256": _sha256_bytes(input_raw),
            "row_count": len(input_rows),
            "nested_multiview_final": all(isinstance(row.get("final"), dict) for row in input_rows),
        },
        "provider_input": (
            {
                "filename": provider_input.name,
                "sha256": _sha256_bytes(provider_raw),
                "row_count": len(provider_rows),
                "content_sha256_verified": True,
                "joined_adjudication_count": len(input_rows),
            }
            if provider_input is not None
            and provider_raw is not None
            and provider_rows is not None
            else None
        ),
        "source_index": (
            {
                "filename": source_index.name,
                "sha256": _sha256_bytes(source_raw),
                "row_count": len(source_rows),
                "joined_adjudication_count": len(input_rows),
                "content_hash_verified": True,
                "provider_text_sha256_verified": True,
                "source_text_sha256_verified": True,
                "text_sha256_verified": True,
                "input_sha256_verified": True,
                "content_stored_in_index": all(
                    isinstance(row.get("content"), dict) for row in source_rows.values()
                ),
            }
            if source_index is not None and source_raw is not None and source_rows is not None
            else None
        ),
        "agreement_filter": agreement_audit,
        "split": {
            "algorithm": (
                "EXTERNAL_FIXED_TEST_ONLY" if fixed_split == "TEST" else SPLIT_ALGORITHM
            ),
            "salt": split_salt,
            "ratios": None if fixed_split == "TEST" else ratios,
            "connected_component_count": len(components),
            "unique_rows": {split: len(prepared[split]) for split in ratios},
            "component_counts": {split: len(assigned[split]) for split in ratios},
            "leakage_audit": audit,
            "dev_resampled": False,
            "test_resampled": False,
            "test_used_for_model_selection": False,
            "fixed_split": fixed_split,
        },
        "train_resampling": (
            {
                "policy": "NONE_EXTERNAL_FIXED_TEST_ONLY",
                "unique_rows": 0,
                "effective_rows": 0,
                "oversampled_rows": 0,
            }
            if fixed_split == "TEST"
            else {
                "policy": RESAMPLING_POLICY,
                "multipliers": multipliers,
                "combination": "MAX_NOT_PRODUCT",
                "unique_rows": len(prepared["TRAIN"]),
                "effective_rows": len(balanced),
                "oversampled_rows": len(balanced) - len(prepared["TRAIN"]),
            }
        ),
        "label_distribution": {
            "TRAIN_UNIQUE": _distribution(prepared["TRAIN"]),
            "TRAIN_EFFECTIVE": _distribution(balanced),
            "DEV": _distribution(prepared["DEV"]),
            "TEST": _distribution(prepared["TEST"]),
        },
        "outputs": output_hashes,
        "ms_swift": {
            "validated_version": "4.5.2",
            "data_format": "messages-jsonl",
            "environment_bootstrap": "scripts/bootstrap_qwen_training_windows.ps1",
            "working_directory": "<DATASET_DIR>",
            "training_allowed": fixed_split != "TEST",
            "training_recipe": None if fixed_split == "TEST" else [
                "swift",
                "sft",
                "--model",
                "Qwen/Qwen2.5-1.5B-Instruct",
                "--dataset",
                OUTPUT_NAMES["train_balanced"],
                "--val_dataset",
                OUTPUT_NAMES["dev"],
                "--split_dataset_ratio",
                "0",
                "--train_type",
                "lora",
                "--quant_method",
                "bnb",
                "--quant_bits",
                "4",
                "--bnb_4bit_quant_type",
                "nf4",
                "--bnb_4bit_use_double_quant",
                "true",
                "--lora_rank",
                "8",
                "--lora_alpha",
                "32",
                "--target_modules",
                "all-linear",
                "--torch_dtype",
                "float16",
                "--num_train_epochs",
                "3",
                "--per_device_train_batch_size",
                "1",
                "--per_device_eval_batch_size",
                "1",
                "--gradient_accumulation_steps",
                "16",
                "--learning_rate",
                "0.0001",
                "--max_length",
                "2048",
                "--eval_steps",
                "20",
                "--save_steps",
                "20",
                "--save_total_limit",
                "2",
                "--logging_steps",
                "5",
                "--warmup_ratio",
                "0.05",
                "--dataloader_num_workers",
                "0",
                "--strict",
                "true",
                "--output_dir",
                "<MODEL_OUTPUT_DIR>",
            ],
            "reserved_test_dataset": OUTPUT_NAMES["test"],
        },
        "label_provenance": "INDEPENDENT_AI_ADJUDICATION_V4",
        "full_v2_adjudication_preserved_in_metadata": True,
        "mechanism_axes_exposed_to_model_target": target_contract == "full-v2",
        "human_gold_claimed": False,
        "qwen_predictions_read": False,
        "qwen_predictions_used_for_labels_or_split": False,
        "post_event_market_data_included": False,
        "production_model_changed": False,
        "production_ledger_changed": False,
        "no_trading": True,
    }
    manifest_raw = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    files[MANIFEST_NAME] = manifest_raw
    for name, digest in {**output_hashes, MANIFEST_NAME: _sha256_bytes(manifest_raw)}.items():
        files[f"{name}.sha256"] = f"{digest}  {name}\n".encode("ascii")
    _write_atomic(output_dir, files)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adjudications", type=Path, required=True)
    parser.add_argument("--provider-input", type=Path)
    parser.add_argument("--source-index", type=Path)
    parser.add_argument(
        "--target-contract", choices=sorted(TARGET_CONTRACTS), default="full-v2"
    )
    parser.add_argument("--fixed-split", choices=("TEST",))
    parser.add_argument(
        "--agreement-policy", choices=sorted(AGREEMENT_POLICIES), default="all"
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--split-salt", default=DEFAULT_SPLIT_SALT)
    parser.add_argument("--train-ratio", type=float, default=0.70)
    parser.add_argument("--dev-ratio", type=float, default=0.15)
    parser.add_argument("--test-ratio", type=float, default=0.15)
    parser.add_argument("--material-adverse-multiplier", type=int, default=3)
    parser.add_argument("--positive-multiplier", type=int, default=4)
    parser.add_argument("--mixed-multiplier", type=int, default=4)
    args = parser.parse_args()
    manifest = prepare(
        adjudications=args.adjudications,
        provider_input=args.provider_input,
        source_index=args.source_index,
        output_dir=args.output_dir,
        target_contract=args.target_contract,
        fixed_split=args.fixed_split,
        agreement_policy=args.agreement_policy,
        split_salt=args.split_salt,
        ratios=(
            None
            if args.fixed_split
            else {"TRAIN": args.train_ratio, "DEV": args.dev_ratio, "TEST": args.test_ratio}
        ),
        multipliers={
            "material_adverse": args.material_adverse_multiplier,
            "positive": args.positive_multiplier,
            "mixed": args.mixed_multiplier,
        },
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
