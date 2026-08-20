"""Build leakage-resistant ``human-blind-v3.1`` candidate samples.

The input is the frozen event-packet layer of an ``ai-census-v1`` owner
package.  AI census answers are intentionally not accepted.  The resulting
owner samples contain only claim text that was known at the claim cutoff and
exact P0/P1 passages whose filing/publication time is no later than that
cutoff.  They contain no old canonical status, AI/model output, market
outcome, peer answer, or preassigned target label.

This module only creates candidates.  It does not write an adjudication store,
freeze a blind dataset, train a model, or mutate the canonical event ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.services.adjudication import HUMAN_BLIND_CONTRACT_VERSION
from app.services.ai_event_census import CONTRACT_VERSION as AI_CENSUS_CONTRACT_VERSION
from app.services.ai_event_census import parse_assignment_records, read_jsonl


SCHEMA_VERSION = 1
SAMPLING_CONTRACT_VERSION = "human-blind-candidate-sampler-v1"
DEFAULT_TARGET_COUNT = 720
DEFAULT_SEED = "finance-radar-human-blind-v3.1-candidates-v1"
MIN_EXACT_PASSAGE_CHARS = 40


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def _clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _parse_timestamp(value: Any, *, field: str, date_only_allowed: bool) -> datetime:
    raw = _clean_text(value)
    if not raw:
        raise ValueError(f"{field} is required")
    if date_only_allowed and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return datetime.combine(datetime.fromisoformat(raw).date(), time.min, timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _claim_known_at(claim: dict[str, Any]) -> datetime:
    """Return max(published, received); both clocks are required."""

    published = _parse_timestamp(
        claim.get("source_published_at"),
        field="claim.source_published_at",
        date_only_allowed=False,
    )
    received = _parse_timestamp(
        claim.get("local_received_at"),
        field="claim.local_received_at",
        date_only_allowed=False,
    )
    return max(published, received)


def _evidence_time_values(row: dict[str, Any]) -> list[tuple[str, datetime]]:
    values: list[tuple[str, datetime]] = []
    if _clean_text(row.get("source_published_at")):
        values.append(
            (
                "source_published_at",
                _parse_timestamp(
                    row["source_published_at"],
                    field="evidence.source_published_at",
                    date_only_allowed=True,
                ),
            )
        )
    if _clean_text(row.get("filing_date")):
        values.append(
            (
                "filing_date",
                _parse_timestamp(
                    row["filing_date"],
                    field="evidence.filing_date",
                    date_only_allowed=True,
                ),
            )
        )
    return values


def _authority_class(value: Any) -> str:
    tier = _clean_text(value).upper()
    if tier.startswith("P0"):
        return "PRIMARY_OFFICIAL"
    if tier.startswith("P1"):
        return "ISSUER_OFFICIAL"
    raise ValueError("only P0/P1 authority tiers are eligible")


def _authority_sort_key(value: Any) -> tuple[int, str]:
    tier = _clean_text(value).upper()
    return (0 if tier.startswith("P0") or tier == "PRIMARY_OFFICIAL" else 1, tier)


def _masked_group(prefix: str, value: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


def _issuer_group(packet: dict[str, Any]) -> str:
    raw = _clean_text(packet.get("stable_id"))
    if not raw:
        ticker = _clean_text(packet.get("ticker_at_event"))
        company = _clean_text(packet.get("company_name"))
        raw = f"ticker:{ticker.casefold()}" if ticker else f"company:{company.casefold()}"
    if raw in {"", "company:"}:
        raise ValueError("issuer identity is unavailable")
    return _masked_group("issuer", raw.casefold())


def _chain_group(packet: dict[str, Any]) -> str:
    chain = packet.get("event_chain")
    chain_id = _clean_text(chain.get("chain_id")) if isinstance(chain, dict) else ""
    raw = chain_id or f"single-event:{_clean_text(packet.get('event_id'))}"
    return _masked_group("chain", raw.casefold())


def _eligible_passages(
    packet: dict[str, Any],
    *,
    as_of: datetime,
) -> tuple[list[dict[str, Any]], Counter[str]]:
    passages: list[dict[str, Any]] = []
    exclusions: Counter[str] = Counter()
    for row in packet.get("evidence") or []:
        if not isinstance(row, dict):
            exclusions["evidence_not_object"] += 1
            continue
        tier = _clean_text(row.get("authority_tier")).upper()
        if not tier.startswith(("P0", "P1")):
            exclusions["not_p0_p1"] += 1
            continue
        passage = _clean_text(row.get("evidence_passage"))
        if len(passage) < MIN_EXACT_PASSAGE_CHARS:
            exclusions["no_exact_passage"] += 1
            continue
        try:
            evidence_times = _evidence_time_values(row)
        except ValueError:
            exclusions["invalid_evidence_time"] += 1
            continue
        if not evidence_times:
            exclusions["missing_evidence_time"] += 1
            continue
        if any(value > as_of for _, value in evidence_times):
            exclusions["post_as_of_evidence"] += 1
            continue
        published_value = row.get("source_published_at") or row.get("filing_date")
        passages.append(
            {
                "evidence_id": _clean_text(row.get("evidence_id")),
                "authority_class": _authority_class(tier),
                "document_type": _clean_text(row.get("form") or row.get("source_type")),
                "item_section": _clean_text(row.get("items")),
                "published_at": published_value,
                "received_at": None,
                "passage": passage,
            }
        )
    passages.sort(
        key=lambda row: (
            _authority_sort_key(row["authority_class"]),
            _clean_text(row.get("published_at")),
            _clean_text(row.get("evidence_id")),
            hashlib.sha256(row["passage"].encode("utf-8")).hexdigest(),
        )
    )
    return passages[:8], exclusions


def packet_to_candidate(packet: dict[str, Any]) -> tuple[dict[str, Any], Counter[str]]:
    """Convert one frozen AI-census packet to a human-blind owner sample."""

    if packet.get("record_type") != "event_packet":
        raise ValueError("record_type must be event_packet")
    if _clean_text(packet.get("contract_version")) != AI_CENSUS_CONTRACT_VERSION:
        raise ValueError(f"input must use {AI_CENSUS_CONTRACT_VERSION}")
    event_id = _clean_text(packet.get("event_id"))
    if not event_id:
        raise ValueError("event_id is required")
    claim = packet.get("claim")
    if not isinstance(claim, dict):
        raise ValueError("claim must be an object")
    as_of = _claim_known_at(claim)
    passages, passage_exclusions = _eligible_passages(packet, as_of=as_of)
    if not passages:
        raise ValueError("no exact P0/P1 passage was available by claim known_at")

    content = {
        "contract_version": HUMAN_BLIND_CONTRACT_VERSION,
        "as_of": as_of.isoformat(),
        "cutoff_policy": (
            "claim_known_at=max(source_published_at,local_received_at);"
            "evidence_filing_or_published_at<=as_of"
        ),
        "headline": _clean_text(claim.get("title")),
        "summary": _clean_text(claim.get("summary")),
        "confirmed_facts": [],
        "passages": passages,
        "event_date": packet.get("event_date"),
        "source_identity_hidden": True,
        "target_label_hidden": True,
        "post_event_market_data_included": False,
        "model_output_included": False,
    }
    text_sha256 = sha256_json(content)
    sample_id = "hbv3-" + hashlib.sha256(
        f"{event_id}|{text_sha256}".encode("utf-8")
    ).hexdigest()[:24]
    family = _clean_text(packet.get("proposed_event_family")) or "unknown"
    primary = passages[0]
    source_rows = [
        row
        for row in packet.get("evidence") or []
        if isinstance(row, dict)
        and _clean_text(row.get("evidence_id")) == primary["evidence_id"]
    ]
    source_id = _clean_text(source_rows[0].get("source_id")) if source_rows else "unknown"
    authority_tier = (
        _clean_text(source_rows[0].get("authority_tier")) if source_rows else "P1"
    )
    candidate = {
        "sample_id": sample_id,
        "event_id": event_id,
        "text_sha256": text_sha256,
        "content": content,
        "source_id": source_id or "unknown",
        "authority_tier": authority_tier,
        "entity_group": _issuer_group(packet),
        "event_chain_group": _chain_group(packet),
        "event_family": family,
        "source_packet_sha256": _clean_text(packet.get("packet_sha256")),
        "split": "UNASSIGNED",
    }
    return candidate, passage_exclusions


def build_candidate_set(
    packets: Iterable[dict[str, Any]],
    *,
    target_count: int = DEFAULT_TARGET_COUNT,
    seed: str = DEFAULT_SEED,
) -> dict[str, Any]:
    """Select an exact deterministic, family-balanced, zero-group-overlap set."""

    if int(target_count) < 1:
        raise ValueError("target_count must be positive")
    if not _clean_text(seed):
        raise ValueError("seed is required")

    by_event: dict[str, dict[str, Any]] = {}
    duplicate_packet_count = 0
    for packet in packets:
        event_id = _clean_text(packet.get("event_id"))
        if not event_id:
            raise ValueError("every event packet must contain event_id")
        prior = by_event.get(event_id)
        if prior is not None:
            if stable_json(prior) != stable_json(packet):
                raise ValueError(f"conflicting frozen packets for event {event_id}")
            duplicate_packet_count += 1
            continue
        by_event[event_id] = dict(packet)

    rejection_counts: Counter[str] = Counter()
    passage_exclusion_counts: Counter[str] = Counter()
    candidates: list[dict[str, Any]] = []
    for event_id in sorted(by_event):
        try:
            candidate, passage_exclusions = packet_to_candidate(by_event[event_id])
        except ValueError as exc:
            rejection_counts[str(exc)] += 1
            continue
        passage_exclusion_counts.update(passage_exclusions)
        candidates.append(candidate)

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        buckets[str(candidate["event_family"])].append(candidate)
    for family, rows in buckets.items():
        rows.sort(
            key=lambda row: hashlib.sha256(
                f"{seed}|{family}|{row['event_id']}|{row['text_sha256']}".encode("utf-8")
            ).hexdigest()
        )

    family_order = sorted(
        buckets,
        key=lambda family: hashlib.sha256(f"{seed}|family|{family}".encode("utf-8")).hexdigest(),
    )
    selected: list[dict[str, Any]] = []
    used_entities: set[str] = set()
    used_chains: set[str] = set()
    used_text: set[str] = set()
    positions = {family: 0 for family in family_order}
    while len(selected) < int(target_count):
        progressed = False
        for family in family_order:
            rows = buckets[family]
            while positions[family] < len(rows):
                row = rows[positions[family]]
                positions[family] += 1
                if row["entity_group"] in used_entities:
                    rejection_counts["duplicate_issuer_group"] += 1
                    continue
                if row["event_chain_group"] in used_chains:
                    rejection_counts["duplicate_event_chain_group"] += 1
                    continue
                if row["text_sha256"] in used_text:
                    rejection_counts["duplicate_exact_content"] += 1
                    continue
                selected.append(row)
                used_entities.add(row["entity_group"])
                used_chains.add(row["event_chain_group"])
                used_text.add(row["text_sha256"])
                progressed = True
                break
            if len(selected) >= int(target_count):
                break
        if not progressed:
            break

    if len(selected) < int(target_count):
        raise ValueError(
            "insufficient leakage-safe zero-group-overlap candidates: "
            f"{len(selected)}/{int(target_count)}; eligible before grouping={len(candidates)}"
        )

    selected.sort(key=lambda row: str(row["sample_id"]))
    sample_set_sha256 = sha256_json(selected)
    return {
        "schema_version": SCHEMA_VERSION,
        "contract_version": HUMAN_BLIND_CONTRACT_VERSION,
        "sampling_contract_version": SAMPLING_CONTRACT_VERSION,
        "source_contract_version": AI_CENSUS_CONTRACT_VERSION,
        "seed": seed,
        "target_count": int(target_count),
        "row_count": len(selected),
        "sample_set_sha256": sample_set_sha256,
        "samples": selected,
        "input_unique_event_count": len(by_event),
        "input_overlap_packet_count": duplicate_packet_count,
        "eligible_before_group_dedup_count": len(candidates),
        "selected_family_counts": dict(
            sorted(Counter(row["event_family"] for row in selected).items())
        ),
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "passage_exclusion_counts": dict(sorted(passage_exclusion_counts.items())),
        "issuer_overlap_count": len(selected) - len(used_entities),
        "event_chain_overlap_count": len(selected) - len(used_chains),
        "exact_text_overlap_count": len(selected) - len(used_text),
        "confirmed_facts_included": False,
        "old_status_included": False,
        "ai_or_model_output_included": False,
        "post_event_market_data_included": False,
        "target_label_preassigned": False,
        "canonical_state_changed": False,
        "no_trading": True,
    }


def load_packets_from_census_package(package_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load and verify full frozen coverage from an AI census package root."""

    root = package_root.resolve()
    assignment_paths = sorted(root.glob("成员*/任务分片/*.input.jsonl"))
    if not assignment_paths:
        raise ValueError("no AI census assignment JSONL files found")
    packets: list[dict[str, Any]] = []
    batch_ids: set[str] = set()
    for path in assignment_paths:
        header, rows = parse_assignment_records(read_jsonl(path))
        batch_ids.add(str(header["batch_id"]))
        packets.extend(rows)
    if len(batch_ids) != 1:
        raise ValueError("assignment files do not belong to one census batch")

    owner_path = root / "负责人材料" / "owner_index.json"
    if not owner_path.is_file():
        raise ValueError("owner_index.json is required to prove full event coverage")
    owner = json.loads(owner_path.read_text(encoding="utf-8-sig"))
    if not isinstance(owner, dict):
        raise ValueError("owner_index.json must contain an object")
    expected = {str(row.get("event_id") or "") for row in owner.get("events") or []}
    actual = {_clean_text(row.get("event_id")) for row in packets}
    if "" in expected or not expected:
        raise ValueError("owner index has no valid event inventory")
    if actual != expected:
        missing = len(expected - actual)
        unexpected = len(actual - expected)
        raise ValueError(
            f"assignment coverage does not match owner index: missing={missing}, unexpected={unexpected}"
        )
    if int(owner.get("event_count") or -1) != len(expected):
        raise ValueError("owner event_count does not match owner event inventory")
    if _clean_text(owner.get("batch_id")) not in batch_ids:
        raise ValueError("owner index batch_id does not match assignments")
    return packets, {
        "batch_id": next(iter(batch_ids)),
        "owner_event_count": len(expected),
        "assignment_file_count": len(assignment_paths),
    }


def write_candidate_set(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
