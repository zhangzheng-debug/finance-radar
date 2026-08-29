#!/usr/bin/env python3
"""Mine independent high-precision semantic hard cases from a frozen review kit.

The resulting rows are weak supervision, not human gold.  Every event contained
in the owner manifest is excluded before matching, which keeps the existing
train/validation/owner-holdout boundary intact.  Rules are intentionally narrow:
they teach realized downside mechanisms and their most common false-positive
contrasts without using evidence posture, price outcomes, or prior model output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.qwen_risk_contract import (  # noqa: E402
    QWEN_RISK_CONTRACT_VERSION,
    expected_semantic_payload,
    normalize_qwen_risk_content,
)
from scripts.prepare_qwen_semantic_consensus_sft import (  # noqa: E402
    EXPERIMENT_SYSTEM_PROMPT,
)


CONTRACT_VERSION = "qwen-semantic-independent-hardcase-weak-supervision-v1"
PROVENANCE = "FROZEN_SOURCE_HIGH_PRECISION_WEAK_SUPERVISION"
TARGETS = {
    "PRIORITY": ("MATERIAL_ADVERSE", "ADVERSE"),
    "NEUTRAL": ("NOT_MATERIAL_ADVERSE", "NEUTRAL"),
    "POSITIVE": ("NOT_MATERIAL_ADVERSE", "POSITIVE"),
    "LOW_ADVERSE": ("NOT_MATERIAL_ADVERSE", "ADVERSE"),
}


def _pattern(value: str) -> re.Pattern[str]:
    return re.compile(value, re.I | re.S)


# These contrasts must run before adverse rules.  They are recurring causes of
# keyword-only false alarms in filings and public-news text.
CONTRAST_RULES: tuple[tuple[str, tuple[str, str], re.Pattern[str]], ...] = (
    (
        "paid_or_completed_listing_exit",
        TARGETS["NEUTRAL"],
        _pattern(
            r"\b(?:form\s*25|25-nse|delist).{0,1800}(?:per share in cash|merger consideration|"
            r"acquisition (?:was |has been )?completed|cash merger|received .{0,80}cash consideration)\b"
        ),
    ),
    (
        "resolved_financial_or_listing_risk",
        TARGETS["POSITIVE"],
        _pattern(
            r"\b(?:substantial doubt.{0,220}(?:is|was|has been) alleviated|"
            r"(?:has|had|successfully) regained compliance|compliance (?:has been|was) restored|"
            r"no longer (?:subject to|at risk of) delist|cured (?:the )?(?:default|breach))\b"
        ),
    ),
    (
        "hypothetical_liquidation_or_default",
        TARGETS["NEUTRAL"],
        _pattern(
            r"\b(?:if|should|could|may) .{0,220}(?:unable to consummate|fail to complete|"
            r"be required to liquidate|constitute an event of default).{0,260}"
            r"(?:business combination|trust account|liquidat|agreement|indenture)\b"
        ),
    ),
    (
        "spac_going_concern_is_lifecycle_risk",
        TARGETS["NEUTRAL"],
        _pattern(
            r"\b(?:blank check|acquisition corp|initial business combination|trust account)\b"
            r".{0,1800}\bsubstantial doubt.{0,180}(?:going concern|ability to continue)\b|"
            r"\bsubstantial doubt.{0,180}(?:going concern|ability to continue)\b"
            r".{0,1800}\b(?:blank check|acquisition corp|initial business combination|trust account)\b"
        ),
    ),
    (
        "contract_definition_not_realized",
        TARGETS["NEUTRAL"],
        _pattern(
            r"\b(?:the term [\"“]?default[\"”]? means|events? of default include|"
            r"for purposes? of this .{0,100}(?:event of default|covenant breach))\b"
        ),
    ),
)


PRIORITY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "bankruptcy_restructuring_or_equity_cancellation",
        _pattern(
            r"\b(?:filed|commenced|petitioned|entered|confirmed|effective).{0,180}"
            r"chapter\s*(?:7|11)|chapter\s*(?:7|11).{0,260}"
            r"(?:bankruptcy court|plan|petition|cancelled|canceled|discharged)|"
            r"(?:common stock|ordinary shares?|equity interests?).{0,180}"
            r"(?:cancelled|canceled|discharged).{0,100}(?:no recovery|no force and effect)\b"
        ),
    ),
    (
        "capital_exhaustion_or_operating_curtailment",
        _pattern(
            r"\b(?:unable to raise additional capital|if we are unable to raise additional capital|"
            r"failure to obtain (?:additional )?(?:capital|financing)|cash.{0,90}(?:is|will be) insufficient)"
            r".{0,260}(?:reduce|curtail|suspend|cease|continue as a going concern|operations?|obligations?)\b"
        ),
    ),
    (
        "going_concern_or_realized_default",
        _pattern(
            r"\b(?:substantial doubt.{0,160}(?:going concern|ability to continue)|"
            r"missed (?:a )?(?:debt|interest|principal) payment|maturity[- ]default|"
            r"(?:is|was|are|were) in default.{0,120}(?:loan|note|debt|credit)|"
            r"breached .{0,100}(?:financial )?covenant)\b"
        ),
    ),
    (
        "binding_listing_removal_or_suspension",
        _pattern(
            r"(?:^|\n|\")(?:(?:headline|document_type)\"?:\"?)?\s*(?:form\s*)?25(?:\s|-|\"|$)|"
            r"\b(?:notification of removal from listing|delisting determination|determined to delist|"
            r"ordered .{0,70}trading suspended|trading (?:was |is )?suspended|"
            r"scheduled .{0,70}suspension)\b"
        ),
    ),
    (
        "binding_enforcement_fraud_or_restatement",
        _pattern(
            r"\b(?:filed (?:a )?(?:civil )?complaint.{0,160}(?:alleg|fraud|violation)|"
            r"final (?:consent )?judgment|civil (?:monetary )?penalt|disgorgement|"
            r"fraudulent scheme|misappropriat(?:ed|ion)|criminally charged|"
            r"financial statements? should no longer be relied upon|will restate|"
            r"adverse opinion.{0,180}(?:internal control|financial reporting))\b"
        ),
    ),
    (
        "pivotal_clinical_or_serious_safety_failure",
        _pattern(
            r"\b(?:(?:phase\s*3|pivotal).{0,200}(?:did not meet|failed).{0,120}"
            r"(?:primary|key secondary) endpoint|clinical hold|complete response letter|"
            r"may cause serious injury or death|most serious recall type)\b"
        ),
    ),
)


ROUTINE_RULES: tuple[tuple[str, tuple[str, str], re.Pattern[str]], ...] = (
    (
        "clearly_positive_operating_result",
        TARGETS["POSITIVE"],
        _pattern(
            r"\b(?:record revenue|record profit|record throughput|record gold sold|"
            r"beat(?:s|en)? (?:estimates|expectations)|guidance raised|raises? (?:full[- ]year )?guidance|"
            r"successful(?:ly)? met .{0,100}(?:primary|key) endpoint)\b"
        ),
    ),
    (
        "routine_governance_or_administration",
        TARGETS["NEUTRAL"],
        _pattern(
            r"\b(?:annual meeting results?|board re[- ]election|committee appointment|"
            r"appoints? .{0,100}(?:director|chair|cfo|auditor)|planned ceo retirement|"
            r"internal succession|routine form nt|closes? .{0,60}(?:spac )?ipo)\b"
        ),
    ),
    (
        "ordinary_adverse_result",
        TARGETS["LOW_ADVERSE"],
        _pattern(
            r"\b(?:revenue (?:declined|decreased|fell)|net loss (?:increased|widened)|"
            r"lowered (?:full[- ]year )?guidance|missed (?:estimates|expectations))\b"
        ),
    ),
)


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _semantic_text(content: dict[str, Any]) -> str:
    normalized = normalize_qwen_risk_content(content)
    pieces = [str(normalized.get("headline") or ""), str(normalized.get("summary") or "")]
    pieces.extend(str(item.get("passage") or "") for item in normalized.get("passages") or [])
    return " ".join(" ".join(piece.split()) for piece in pieces if piece).strip()


def classify_hardcase(text: str) -> tuple[tuple[str, str], str] | None:
    normalized = " ".join(str(text or "").split())[:30000]
    for name, target, expression in CONTRAST_RULES:
        if expression.search(normalized):
            return target, name
    for name, expression in PRIORITY_RULES:
        if expression.search(normalized):
            return TARGETS["PRIORITY"], name
    for name, target, expression in ROUTINE_RULES:
        if expression.search(normalized):
            return target, name
    return None


def _expression_for_rule(rule: str) -> re.Pattern[str] | None:
    for name, _target, expression in CONTRAST_RULES:
        if name == rule:
            return expression
    for name, expression in PRIORITY_RULES:
        if name == rule:
            return expression
    for name, _target, expression in ROUTINE_RULES:
        if name == rule:
            return expression
    return None


def _bounded_content(content: dict[str, Any], focus_rule: str | None = None) -> dict[str, Any]:
    """Keep the relevant source window within a predictable local-GPU budget."""

    normalized = normalize_qwen_risk_content(content)
    passages = normalized.get("passages") or []
    expression = _expression_for_rule(focus_rule) if focus_rule else None
    selected: list[dict[str, Any]] = []
    if expression:
        for item in passages:
            passage = str(item.get("passage") or "")
            match = expression.search(passage)
            if not match:
                continue
            start = max(0, match.start() - 700)
            end = min(len(passage), match.end() + 2400)
            copy = dict(item)
            copy["passage"] = passage[start:end]
            selected = [copy]
            break
    if not selected:
        for item in passages[:2]:
            passage = str(item.get("passage") or "")
            if len(passage) > 3600:
                passage = passage[:2400] + " … " + passage[-1000:]
            copy = dict(item)
            copy["passage"] = passage
            selected.append(copy)
    normalized["passages"] = selected
    return normalize_qwen_risk_content(normalized)


def packet_to_content(packet: dict[str, Any]) -> dict[str, Any]:
    claim = packet.get("claim") or {}
    passages: list[dict[str, Any]] = []
    for evidence in packet.get("evidence") or []:
        if not isinstance(evidence, dict):
            continue
        passage = str(evidence.get("evidence_passage") or "").strip()
        if not passage:
            continue
        items = evidence.get("items") or []
        passages.append(
            {
                "document_type": evidence.get("form") or evidence.get("source_type") or "",
                "item_section": ", ".join(str(item) for item in items) if isinstance(items, list) else str(items),
                "published_at": evidence.get("filing_date") or claim.get("source_published_at"),
                "passage": passage,
            }
        )
    return normalize_qwen_risk_content(
        {
            "as_of": claim.get("local_received_at"),
            "event_date": packet.get("event_date"),
            "headline": claim.get("title"),
            "summary": claim.get("summary"),
            "passages": passages,
        }
    )


def _owner_exclusions(owner: dict[str, Any]) -> tuple[set[str], set[str]]:
    event_ids: set[str] = set()
    content_hashes: set[str] = set()
    for sample in owner.get("samples") or []:
        event_id = str(sample.get("event_id") or "").strip()
        if event_id:
            event_ids.add(event_id)
        content = sample.get("content") or {}
        text = _semantic_text(content)
        if text:
            content_hashes.add(sha256_text(text))
    return event_ids, content_hashes


def _read_jsonl(lines: Iterable[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in lines if line.strip()]


def _prepared_weak_row(
    packet: dict[str, Any], content: dict[str, Any], materiality: str, polarity: str, rule: str
) -> dict[str, Any]:
    content = _bounded_content(content, rule)
    target = expected_semantic_payload(materiality, polarity)
    event_id = str(packet.get("event_id") or "")
    content_sha256 = sha256_text(stable_json(content))
    return {
        "messages": [
            {"role": "system", "content": EXPERIMENT_SYSTEM_PROMPT},
            {"role": "user", "content": stable_json(content)},
            {"role": "assistant", "content": stable_json(target)},
        ],
        "metadata": {
            "sample_id": f"weak-{sha256_text(event_id + ':' + rule)[:24]}",
            "event_id": event_id,
            "entity_group": str(packet.get("company_name") or packet.get("ticker_at_event") or event_id),
            "event_chain_group": str(packet.get("event_chain") or packet.get("event_fingerprint") or event_id),
            "content_sha256": content_sha256,
            "split": "TRAIN",
            "label_provenance": PROVENANCE,
            "weak_rule": rule,
            "human_gold_claimed": False,
            "evidence_state_used_as_model_target": False,
            "post_event_market_data_included": False,
            "model_output_included_in_review": False,
        },
    }


def _load_package_candidates(owner_package: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with zipfile.ZipFile(owner_package) as archive:
        owner_name = next(name for name in archive.namelist() if name.endswith("owner_manifest.json"))
        owner = json.loads(archive.read(owner_name).decode("utf-8-sig"))
        # Input shards contain the same source packet contract for both lanes;
        # they do not contain either teammate's answers.  Reading both lanes
        # reaches the complete frozen snapshot and event-id deduplication below
        # removes any deliberate overlap.
        shard_names = sorted(name for name in archive.namelist() if name.endswith(".input.jsonl"))
        if not shard_names:
            raise ValueError("frozen package contains no input shards")
        packets: list[dict[str, Any]] = []
        for name in shard_names:
            rows = _read_jsonl(archive.read(name).decode("utf-8-sig").splitlines())
            packets.extend(row for row in rows if row.get("record_type") != "manifest")
    return owner, packets


def prepare(
    *,
    owner_package: Path,
    base_train: Path,
    output_dir: Path,
    per_rule_cap: int = 48,
    base_repeat: int = 2,
) -> dict[str, Any]:
    owner, packets = _load_package_candidates(owner_package)
    excluded_event_ids, excluded_text_hashes = _owner_exclusions(owner)
    candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    seen_event_ids: set[str] = set()
    seen_content_hashes: set[str] = set()
    exclusion_counts: Counter[str] = Counter()

    for packet in packets:
        event_id = str(packet.get("event_id") or "").strip()
        if not event_id or event_id in seen_event_ids:
            exclusion_counts["missing_or_duplicate_event_id"] += 1
            continue
        seen_event_ids.add(event_id)
        if event_id in excluded_event_ids:
            exclusion_counts["owner_manifest_event"] += 1
            continue
        content = packet_to_content(packet)
        semantic_text = _semantic_text(content)
        if not semantic_text:
            exclusion_counts["empty_semantic_text"] += 1
            continue
        semantic_hash = sha256_text(semantic_text)
        if semantic_hash in excluded_text_hashes:
            exclusion_counts["owner_manifest_text"] += 1
            continue
        content_hash = sha256_text(stable_json(content))
        if content_hash in seen_content_hashes:
            exclusion_counts["duplicate_content"] += 1
            continue
        seen_content_hashes.add(content_hash)
        decision = classify_hardcase(semantic_text)
        if not decision:
            exclusion_counts["no_high_precision_rule"] += 1
            continue
        (materiality, polarity), rule = decision
        row = _prepared_weak_row(packet, content, materiality, polarity, rule)
        candidates[rule].append(row)

    selected: list[dict[str, Any]] = []
    available_by_rule = {rule: len(rows) for rule, rows in sorted(candidates.items())}
    for rule, rows in sorted(candidates.items()):
        rows.sort(key=lambda row: sha256_text(f"{rule}:{row['metadata']['event_id']}"))
        selected.extend(rows[:per_rule_cap])

    base_rows = _read_jsonl(base_train.read_text(encoding="utf-8").splitlines())
    for row in base_rows:
        bounded = _bounded_content(json.loads(row["messages"][1]["content"]))
        row["messages"][1]["content"] = stable_json(bounded)
        row["metadata"]["content_sha256"] = sha256_text(stable_json(bounded))
        row["metadata"]["training_input_bounded"] = True
    base_hashes = {str(row.get("metadata", {}).get("content_sha256") or "") for row in base_rows}
    selected = [row for row in selected if row["metadata"]["content_sha256"] not in base_hashes]
    combined: list[dict[str, Any]] = []
    for row in base_rows:
        for repeat_index in range(base_repeat):
            copy = json.loads(json.dumps(row))
            if repeat_index:
                copy["metadata"]["training_repeat"] = repeat_index
                copy["metadata"]["origin_sample_id"] = row["metadata"]["sample_id"]
            combined.append(copy)
    combined.extend(selected)

    output_dir.mkdir(parents=True, exist_ok=True)
    weak_path = output_dir / "qwen_risk_sft_weak_hardcases.jsonl"
    combined_path = output_dir / "qwen_risk_sft_train_v4.jsonl"
    weak_path.write_text("".join(stable_json(row) + "\n" for row in selected), encoding="utf-8")
    combined_path.write_text("".join(stable_json(row) + "\n" for row in combined), encoding="utf-8")

    def pair_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
        return dict(
            Counter(
                f"{target['materiality']}|{target['polarity']}"
                for row in rows
                for target in [json.loads(row["messages"][-1]["content"])]
            )
        )

    selected_by_rule = dict(Counter(row["metadata"]["weak_rule"] for row in selected))
    manifest = {
        "schema_version": 1,
        "contract_version": CONTRACT_VERSION,
        "semantic_contract_version": QWEN_RISK_CONTRACT_VERSION,
        "owner_manifest_sha256": owner.get("manifest_sha256"),
        "owner_manifest_events_excluded": len(excluded_event_ids),
        "frozen_packet_rows_seen": len(packets),
        "per_rule_cap": per_rule_cap,
        "base_human_consensus_repeat": base_repeat,
        "available_by_rule": available_by_rule,
        "selected_by_rule": selected_by_rule,
        "exclusion_counts": dict(exclusion_counts),
        "base_human_consensus_rows": len(base_rows),
        "weak_supervision_rows": len(selected),
        "combined_training_rows": len(combined),
        "weak_pair_counts": pair_counts(selected),
        "combined_pair_counts": pair_counts(combined),
        "label_provenance": PROVENANCE,
        "human_gold_claimed": False,
        "owner_holdout_opened": False,
        "validation_or_holdout_rows_imported": False,
        "evidence_state_used_as_model_target": False,
        "post_event_market_data_included": False,
        "production_model_changed": False,
        "output_sha256": {
            "weak": hashlib.sha256(weak_path.read_bytes()).hexdigest(),
            "combined": hashlib.sha256(combined_path.read_bytes()).hexdigest(),
        },
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-package", type=Path, required=True)
    parser.add_argument("--base-train", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--per-rule-cap", type=int, default=48)
    parser.add_argument("--base-repeat", type=int, default=2)
    args = parser.parse_args()
    if args.per_rule_cap < 1:
        raise ValueError("per-rule-cap must be positive")
    if args.base_repeat < 1:
        raise ValueError("base-repeat must be positive")
    manifest = prepare(
        owner_package=args.owner_package.resolve(),
        base_train=args.base_train.resolve(),
        output_dir=args.output_dir.resolve(),
        per_rule_cap=args.per_rule_cap,
        base_repeat=args.base_repeat,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
