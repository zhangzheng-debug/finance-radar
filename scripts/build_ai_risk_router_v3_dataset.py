#!/usr/bin/env python3
"""Build evidence-first AI adjudications and freeze a disjoint blind-v2 set.

The generated labels are explicitly AI rubric adjudications, never human labels.
Event taxonomy and source identity may be audited, but they are never included
in the learned model text and never determine the target label by themselves.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_DB = ROOT / "data" / "operational_backups" / "finance_radar_20260722T072926Z.sqlite3"
DEFAULT_DEV = ROOT / "artifacts" / "risk_router_v3_ai_adjudications_dev.jsonl"
DEFAULT_BLIND = ROOT / "artifacts" / "risk_router_external_blind_v2.jsonl"
DEFAULT_FREEZE = ROOT / "artifacts" / "risk_router_external_blind_v2_freeze.json"
DEFAULT_REPORT = ROOT / "artifacts" / "risk_router_v3_label_audit.json"
DEFAULT_MARKDOWN = ROOT / "artifacts" / "risk_router_v3_label_audit.md"
DEFAULT_OVERRIDES = ROOT / "config" / "risk_router_v3_ai_overrides.json"

AI_ADJUDICATOR = "codex_gpt5_evidence_policy_v1"
AI_REVIEWER_TYPE = "AI_RUBRIC_ADJUDICATOR_NOT_HUMAN"
BLIND_TARGETS = {"RISK_REVIEW": 30, "NON_TARGET": 30, "ABSTAIN": 20}
BLIND_MIN_EVENT_DATE = "2025-01-01"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def pattern(expression: str) -> re.Pattern[str]:
    return re.compile(expression, re.I | re.S)


ADVERSE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bankruptcy_or_receivership", pattern(r"\b(?:chapter\s*(?:7|11)|bankrupt(?:cy)?|insolven(?:cy|t)|receivership|winding[- ]up|unable to pay (?:its )?debts|joint voluntary liquidators?|appointed.{0,40}(?:receiver|administrator)|closed.{0,80}fdic)\b")),
    ("going_concern_or_default", pattern(r"\b(?:substantial doubt.{0,100}(?:ability to continue|going concern)|insufficient cash.{0,100}(?:obligations|continue)|unable to continue as a going concern|debt default|defaulted on|missed (?:debt|interest) payment|covenant breach)\b")),
    ("equity_cancellation_or_severe_dilution", pattern(r"\b(?:old common|existing common|legacy (?:common )?(?:equity|holders?)).{0,120}(?:cancelled|canceled|zero recovery|no recovery)|\b(?:one[- ]for[- ](?:50|75|80|100|150)|1[- ]for[- ](?:50|75|80|100|150))\b|\b(?:highly dilutive|dilutive offering)\b")),
    ("listing_loss_or_suspension", pattern(r"\b(?:delisting determination|trading (?:is )?suspended|trading suspension|ordered suspended|listing noncompliance|notice of noncompliance|penny[- ]stock bar)\b")),
    ("enforcement_finality", pattern(r"\b(?:filed (?:a )?(?:civil )?complaint|complaint (?:alleges|seeks)|charged? .{0,80}(?:fraud|violat|insider trading)|civil monetary penalt|civil penalt|disgorgement|final (?:consent )?judgment|permanent(?:ly)? (?:enjoin|ban)|cease and desist|officer and director bar|trading and registration ban)\b")),
    ("bank_regulatory_action", pattern(r"\b(?:written agreement.{0,300}(?:unsafe or unsound|capital plan|liquidity|contingency funding)|prohibit(?:s|ed)? .{0,140}(?:banking|bank|industry) participation|fined? .{0,100}\$[0-9]|formal enforcement action)\b")),
    ("fraud_or_misappropriation", pattern(r"\b(?:fraudulent scheme|fraudulently solicited|misappropriat(?:ed|ion)|pump[- ]and[- ]dump|false profitability claims|bilked millions|(?:market|order|trading) spoofing|spoofing (?:orders|markets?))\b")),
    ("product_safety_material", pattern(r"\b(?:class i|most serious recall type|recall|removes?|correction).{0,180}(?:serious injur|death|contamination|stroke|perforation|therapy|fail[- ]safe)|\b(?:three|four|five|six|seven|eight|nine|\d+) deaths? associated\b")),
    ("cyber_or_asset_loss", pattern(r"\b(?:security breach|data breach|ransomware|cyberattack|wallet (?:hack|drain)|funds? (?:stolen|drained)|protocol exploit|bridge exploit)\b")),
    ("material_operating_harm", pattern(r"\b(?:mass layoffs?|workforce reduction|plant shutdown|operations? suspended|catastrophic trading losses|profit warning|guidance (?:cut|lowered|withdrawn))\b")),
    ("sanctions_or_attack", pattern(r"\b(?:sanctions? (?:imposed|announced)|export controls?|blockade|commercial vessels?.{0,120}(?:attack|strike|threat)|(?:attack|strike|threat).{0,120}commercial vessels?|missile attack|airstrike|armed uavs?|armed unmanned surface vessels?)\b")),
)


NON_TARGET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("explicit_not_event_truth", pattern(r"\b(?:not event truth|not a new event|without contemporaneous primary|metadata (?:error|mismatch)|duplicate (?:detector|observation|price)|price[- ]only|market outcome|ticker .{0,50}backfilled|mislabeled as|not bankruptcy)\b")),
    ("positive_results_or_guidance", pattern(r"\b(?:record revenue|record profit|positive preliminary|beat(?:s|en)? (?:estimates|expectations)|raises? (?:full[- ]year )?guidance|guidance raised|revenue growth|profit growth)\b")),
    ("capital_return", pattern(r"\b(?:share repurchase|stock buyback|dividend increase|return of capital)\b")),
    ("ordinary_product_or_partnership", pattern(r"\b(?:launch(?:es|ed)?|introduces?|collaborat(?:es|ion)|partnership|contract award|regulatory approval|opens? new|expands? availability)\b")),
    ("routine_governance", pattern(r"\b(?:appoints? [a-z .'-]{2,80} as |director general secretariat|committee appointment|annual meeting voting|meeting minutes|research task force|publishes? indicative .{0,40}calendar)\b")),
    ("routine_statistics_or_guidance", pattern(r"\b(?:statistics|statistical release|consumer price index|producer price index|employment situation|payments study|issues? (?:joint )?(?:statement|guidance)|operational calendars?|reserve maintenance periods?)\b")),
    ("routine_policy_or_calendar", pattern(r"\b(?:schedules? .{0,80}(?:results?|release)|summary of economic projections|fomc statement|minutes of (?:the )?.{0,80}(?:meeting|committee)|final rule establishes|proposes? .{0,100}(?:requirements?|amendments?)|research task forces?|distressed or underserved .{0,50}list|stress[- ]test results?)\b")),
    ("routine_corporate_update", pattern(r"\b(?:annual meeting results?|shareholders? approv(?:e|ed)|board re[- ]election|appoints? .{0,100}(?:director|chair|cfo|auditor)|reports? .{0,80}(?:drill results?|nav and leverage)|closes? .{0,60}(?:spac )?ipo|routine form nt|planned ceo retirement|internal succession)\b")),
    ("whistleblower_award_administration", pattern(r"\b(?:grants?|announc(?:es|ed)) .{0,80}whistleblower awards?\b|\bwhistleblower awards? totaling\b")),
    ("paid_transaction_or_continuity", pattern(r"\b(?:cash consideration|per share in cash|cash (?:trust )?redemption|pro rata trust|assume all deposits|purchase substantially all assets|otcqx continuity|continued listing on|approved for listing|underlying .{0,80}(?:shares?|h shares?).{0,80}(?:continued|remained) (?:trading|listed)|ordinary shares? (?:continued|remained) (?:trading|listed))\b")),
)


CONFLICT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("allegation_denied_or_unresolved", pattern(r"\b(?:denies?|denied|disputes?|without admitting or denying|subject to court approval|proposed judgment)\b")),
    ("no_harm_yet", pattern(r"\b(?:no serious injuries|no deaths|has not resulted in|no customer funds? lost)\b")),
    ("conditional_or_expected", pattern(r"\b(?:expected to|plans? to|may result|could lead|potential(?:ly)?|preliminary)\b")),
    ("management_departure_or_change", pattern(r"\b(?:departure of directors?|departure of .{0,40}officer|resign(?:s|ed|ation)|changes? in control)\b")),
)


GOING_CONCERN_NEGATION_OR_TEMPLATE = pattern(
    r"(?:conclude whether.{0,180}raise substantial doubt|"
    r"sufficient (?:financial )?resources.{0,180}continue as a going concern|"
    r"previously disclosed.{0,180}continue as a going concern.{0,180}(?:alleviated|resolved))"
)


BROAD_ADVERSE_CONTEXT = pattern(
    r"\b(?:delist(?:ed|ing)?|suspend(?:ed|sion)?|chapter\s*(?:7|11)|bankrupt(?:cy)?|insolven(?:cy|t)?|receiver|"
    r"reverse split|share consolidation|winding[- ]up|wind[- ]down|liquidat(?:e|ed|ion|ing)?|administration|"
    r"working[- ]capital deficit|cash exhaustion|going concern|dilut|warrants?|offering|"
    r"no contemporaneous issuer|identity (?:and|or) (?:action|timing)|later otc identity)\b"
)


RAW_OR_INSUFFICIENT = pattern(
    r"^(?:\s*(?:8-k|6-k|10-k|10-q|20-f)\s+)?(?:[a-z0-9:_-]+\s+){25,}$|"
    r"\b(?:xbrl|commission file number|employer identification number)\b.{0,300}$"
)


POLICY = {
    "schema_version": 1,
    "task": "evidence-stage material adverse risk routing",
    "axes": {
        "materiality": ["MATERIAL_ADVERSE", "NOT_MATERIAL_ADVERSE", "UNCLEAR"],
        "polarity": ["ADVERSE", "POSITIVE", "NEUTRAL", "MIXED", "UNCLEAR"],
        "evidence_state": ["PRIMARY_SUPPORTED", "DISCOVERY_ONLY", "CONFLICTED", "INSUFFICIENT"],
    },
    "rules": {
        "risk": [code for code, _ in ADVERSE_PATTERNS],
        "non_target": [code for code, _ in NON_TARGET_PATTERNS],
        "conflict": [code for code, _ in CONFLICT_PATTERNS],
    },
    "mapping": {
        "RISK_REVIEW": "material adverse + adverse + primary supported",
        "NON_TARGET": "not material adverse + positive/neutral + primary supported",
        "ABSTAIN": "discovery-only, conflicted, insufficient, mixed or unclear",
    },
    "prohibited_learned_features": [
        "event_family", "event_type", "source_id", "authority_tier", "event_status",
        "manual_grade", "post_event_market_data",
    ],
}


def normalize(value: str | None) -> str:
    return " ".join(str(value or "").split())


def clean_title(value: str, source_id: str) -> str:
    title = normalize(value)
    if source_id == "sharadar_active_research" and ("_" in title or title.casefold().endswith(" candidate")):
        return ""
    return title


def find_codes(text: str, rules: tuple[tuple[str, re.Pattern[str]], ...]) -> list[str]:
    return [code for code, expression in rules if expression.search(text)]


def load_ai_overrides(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("reviewer_type") != AI_REVIEWER_TYPE:
        raise ValueError("AI override file must state AI_RUBRIC_ADJUDICATOR_NOT_HUMAN")
    overrides: dict[str, dict[str, Any]] = {}
    for item in payload.get("overrides", []):
        event_id = str(item.get("event_id") or "")
        if not event_id or event_id in overrides:
            raise ValueError(f"invalid or duplicate override event_id: {event_id!r}")
        if item.get("label") not in {"RISK_REVIEW", "NON_TARGET", "ABSTAIN"}:
            raise ValueError(f"invalid override label for {event_id}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(item.get("evidence_sha256") or "")):
            raise ValueError(f"invalid evidence hash for {event_id}")
        overrides[event_id] = item
    return overrides


def load_rows(db_path: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in connection.execute(
            """
            WITH evidence_ranked AS (
                SELECT ev.*,r.source_id AS evidence_source_id,s.authority_tier,
                       ROW_NUMBER() OVER (
                           PARTITION BY ev.event_id
                           ORDER BY CASE ev.evidence_status
                               WHEN 'confirmed_primary' THEN 0
                               WHEN 'accepted_manual_primary_evidence' THEN 1
                               WHEN 'machine_extracted_unreviewed' THEN 2
                               WHEN 'candidate_passage' THEN 3
                               WHEN 'no_keyword_passage' THEN 4
                               ELSE 5 END,
                               COALESCE(ev.passage_score,0) DESC,ev.updated_at DESC
                       ) AS evidence_rank
                FROM event_evidence ev
                JOIN raw_observations r ON r.observation_id=ev.observation_id
                JOIN sources s ON s.source_id=r.source_id
            )
            SELECT e.event_id,e.status AS event_status,e.event_date,e.stable_id,e.company_name,
                   e.ticker_at_event,e.event_family,e.event_type,e.discovery_source,
                   COALESCE((SELECT chain_id FROM event_chain_members cm
                             WHERE cm.event_id=e.event_id LIMIT 1),'') AS chain_id,
                   COALESCE((SELECT r.title FROM event_observations eo
                             JOIN raw_observations r ON r.observation_id=eo.observation_id
                             WHERE eo.event_id=e.event_id AND eo.relation_type NOT LIKE '%filtered%'
                             ORDER BY r.local_received_at DESC LIMIT 1),'') AS title,
                   COALESCE((SELECT r.summary FROM event_observations eo
                             JOIN raw_observations r ON r.observation_id=eo.observation_id
                             WHERE eo.event_id=e.event_id AND eo.relation_type NOT LIKE '%filtered%'
                             ORDER BY r.local_received_at DESC LIMIT 1),'') AS summary,
                   er.evidence_id,er.evidence_url,er.evidence_passage,er.evidence_status,
                   er.evidence_source_id,er.authority_tier,er.passage_score,
                   COALESCE((SELECT status FROM pipeline_jobs j WHERE j.event_id=e.event_id
                             ORDER BY updated_at DESC LIMIT 1),'') AS job_status
            FROM canonical_events e
            LEFT JOIN evidence_ranked er ON er.event_id=e.event_id AND er.evidence_rank=1
            ORDER BY e.event_date,e.event_id
            """
        )
    ]
    connection.close()
    return rows


def evidence_state(row: dict[str, Any], text: str) -> str:
    status = str(row.get("evidence_status") or "")
    authority = str(row.get("authority_tier") or "")
    source_id = str(row.get("evidence_source_id") or row.get("discovery_source") or "")
    if not normalize(row.get("evidence_passage")) or len(text) < 60:
        return "INSUFFICIENT"
    if source_id == "opennews_free" or authority.startswith("P2"):
        return "DISCOVERY_ONLY"
    if status in {"confirmed_primary", "accepted_manual_primary_evidence"}:
        return "PRIMARY_SUPPORTED"
    if authority.startswith(("P0", "P1")) and status == "machine_extracted_unreviewed":
        return "PRIMARY_SUPPORTED"
    return "INSUFFICIENT"


def adjudicate(
    row: dict[str, Any],
    overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source_id = str(row.get("discovery_source") or "")
    title = clean_title(str(row.get("title") or ""), source_id)
    summary = normalize(row.get("summary"))
    passage = normalize(row.get("evidence_passage"))
    # Accepted manual evidence rows often retain a rejected-candidate title or
    # identity/timing correction in the observation summary. Those fields are
    # audit context, not semantic evidence for the recovered canonical event.
    # Use the reviewed passage itself so the model cannot learn the old ticker
    # or the phrase "accepted official evidence" as a class shortcut.
    model_title = "" if row.get("evidence_status") == "accepted_manual_primary_evidence" else title
    model_summary = "" if row.get("evidence_status") == "accepted_manual_primary_evidence" else summary
    text = normalize(
        " ".join(part for part in (row.get("company_name"), model_title, model_summary, passage) if part)
    )[:30000]
    lowered = text.casefold()
    risk_codes = find_codes(lowered, ADVERSE_PATTERNS)
    if "going_concern_or_default" in risk_codes and GOING_CONCERN_NEGATION_OR_TEMPLATE.search(lowered):
        risk_codes.remove("going_concern_or_default")
    non_target_codes = find_codes(lowered, NON_TARGET_PATTERNS)
    conflict_codes = find_codes(lowered, CONFLICT_PATTERNS)
    state = evidence_state(row, text)
    manual_rejection = (
        row.get("event_status") == "rejected"
        and row.get("evidence_status") == "accepted_manual_primary_evidence"
    )

    manual_adverse_context = bool(risk_codes or BROAD_ADVERSE_CONTEXT.search(lowered))
    manual_paid_or_continuing_exit = "paid_transaction_or_continuity" in non_target_codes
    if manual_rejection and manual_adverse_context and not manual_paid_or_continuing_exit:
        label = "ABSTAIN"
        materiality = "UNCLEAR"
        polarity = "MIXED"
        state = "CONFLICTED"
        reason_codes = [*risk_codes, *non_target_codes, "reviewed_identity_or_time_mismatch"]
        confidence = 0.99
        rationale = (
            "The passage describes adverse content, but the reviewed candidate has an identity, "
            "timing, duplication, or event-truth mismatch; it must not teach the semantic model "
            "that the adverse language itself is non-risk."
        )
    elif manual_rejection:
        label = "NON_TARGET"
        materiality = "NOT_MATERIAL_ADVERSE"
        polarity = "NEUTRAL"
        state = "PRIMARY_SUPPORTED"
        reason_codes = ["reviewed_candidate_invalid_or_wrong_time"]
        confidence = 0.99
        rationale = "Reviewed primary evidence rejects this candidate as a distinct material adverse event."
    elif state in {"DISCOVERY_ONLY", "INSUFFICIENT"} or RAW_OR_INSUFFICIENT.search(passage.casefold()):
        label = "ABSTAIN"
        materiality = "UNCLEAR"
        polarity = "UNCLEAR"
        reason_codes = [f"evidence_{state.casefold()}"]
        confidence = 0.97 if state == "DISCOVERY_ONLY" else 0.92
        rationale = "Evidence is discovery-only or lacks a decision-grade exact passage."
    elif risk_codes and non_target_codes and not any(
        code in risk_codes
        for code in {
            "bankruptcy_or_receivership", "going_concern_or_default", "enforcement_finality",
            "bank_regulatory_action", "product_safety_material",
        }
    ):
        label = "ABSTAIN"
        materiality = "UNCLEAR"
        polarity = "MIXED"
        state = "CONFLICTED"
        reason_codes = [*risk_codes, *non_target_codes, *conflict_codes]
        confidence = 0.86
        rationale = "The same evidence contains material adverse and non-adverse cues without a decisive outcome."
    elif risk_codes:
        label = "RISK_REVIEW"
        materiality = "MATERIAL_ADVERSE"
        polarity = "ADVERSE"
        reason_codes = [*risk_codes, *conflict_codes]
        confidence = min(0.99, 0.90 + 0.02 * len(set(risk_codes)))
        rationale = "Primary evidence states an explicit material adverse event or binding adverse action."
    elif non_target_codes:
        label = "NON_TARGET"
        materiality = "NOT_MATERIAL_ADVERSE"
        polarity = "POSITIVE" if any(
            code in non_target_codes
            for code in {"positive_results_or_guidance", "capital_return", "ordinary_product_or_partnership"}
        ) else "NEUTRAL"
        reason_codes = [*non_target_codes, *conflict_codes]
        confidence = min(0.98, 0.90 + 0.02 * len(set(non_target_codes)))
        rationale = "Primary evidence supports an ordinary positive or neutral event without material adverse content."
    else:
        label = "ABSTAIN"
        materiality = "UNCLEAR"
        polarity = "UNCLEAR"
        reason_codes = [*conflict_codes] or ["no_decisive_adverse_or_non_target_clause"]
        confidence = 0.82
        rationale = "Available primary evidence is not decisive enough for either substantive class."

    evidence_sha256 = hashlib.sha256(passage.encode("utf-8")).hexdigest()
    strict_override = (overrides or {}).get(str(row["event_id"]))
    if strict_override:
        if strict_override["evidence_sha256"] != evidence_sha256:
            raise ValueError(
                f"stale AI override evidence hash for {row['event_id']}: "
                f"expected {strict_override['evidence_sha256']}, got {evidence_sha256}"
            )
        label = strict_override["label"]
        materiality = strict_override["axes"]["materiality"]
        polarity = strict_override["axes"]["polarity"]
        state = strict_override["axes"]["evidence_state"]
        reason_codes = list(strict_override["reason_codes"])
        rationale = str(strict_override["rationale"])
        confidence = float(strict_override["adjudication_confidence"])
    text_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    entity_group = normalize(
        row.get("stable_id") or row.get("ticker_at_event") or row.get("company_name") or row["event_id"]
    ).casefold()
    return {
        "sample_id": "V3-" + hashlib.sha256(f"{row['event_id']}|{text_sha256}".encode()).hexdigest()[:20],
        "event_id": row["event_id"],
        "event_date": str(row.get("event_date") or "0000-00-00")[:10],
        "entity_group": entity_group,
        "event_chain_group": str(row.get("chain_id") or "").casefold(),
        "source_group": str(row.get("evidence_source_id") or source_id),
        "authority_tier": str(row.get("authority_tier") or "unknown"),
        "title": title,
        "canonical_url": row.get("evidence_url"),
        "text": text,
        "text_sha256": text_sha256,
        "evidence_sha256": evidence_sha256,
        "label": label,
        "axes": {
            "materiality": materiality,
            "polarity": polarity,
            "evidence_state": state,
        },
        "reason_codes": sorted(set(reason_codes)),
        "rationale": rationale,
        "adjudication_confidence": round(confidence, 3),
        "adjudicator": AI_ADJUDICATOR,
        "reviewer_type": AI_REVIEWER_TYPE,
        "human_reviewed": False,
        "model_input_contract": "company_plus_exact_evidence_with_unreviewed_observation_context_only",
        "prohibited_features_excluded": True,
        "audit_context": {
            "event_status": row.get("event_status"),
            "event_family": row.get("event_family"),
            "event_type": row.get("event_type"),
            "discovery_source": source_id,
            "evidence_status": row.get("evidence_status"),
            "job_status": row.get("job_status"),
            "ai_strict_override": bool(strict_override),
            "override_reviewed_at": strict_override.get("reviewed_at") if strict_override else None,
        },
    }


def near_duplicate_key(row: dict[str, Any]) -> str:
    words = re.findall(r"[a-z0-9]+", row["text"].casefold())
    return hashlib.sha256(" ".join(words[:80]).encode()).hexdigest()


STRICT_OFFICIAL_PAGE_SOURCES = {
    "bls_key_indicators",
    "cftc_enforcement",
    "ecb_press",
    "ecb_statistical_press",
    "fda_medwatch",
    "fdic_press_releases",
    "federal_reserve_press",
    "ftc_press",
    "sec_litigation_releases",
    "sec_trading_suspensions",
}


def strict_evidence_row(row: dict[str, Any]) -> bool:
    evidence_status = str(row["audit_context"].get("evidence_status") or "")
    passage = normalize(row["text"])
    if row["audit_context"].get("ai_strict_override"):
        return True
    if evidence_status in {"confirmed_primary", "accepted_manual_primary_evidence"}:
        return True
    return (
        row["source_group"] in STRICT_OFFICIAL_PAGE_SOURCES
        and evidence_status == "machine_extracted_unreviewed"
        and 60 <= len(passage) <= 6000
    )


def training_eligible(row: dict[str, Any]) -> bool:
    if row["adjudication_confidence"] < 0.86:
        return False
    if row["label"] in {"RISK_REVIEW", "NON_TARGET"}:
        return strict_evidence_row(row)
    return (
        row["axes"]["evidence_state"] in {"DISCOVERY_ONLY", "INSUFFICIENT", "CONFLICTED"}
        and len(row["text"]) >= 20
    )


def source_balanced_order(rows: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    """Deterministically interleave sources so a large SEC bucket cannot occupy the holdout."""
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        buckets[row["source_group"]].append(row)
    for source, items in buckets.items():
        items.sort(
            key=lambda row: hashlib.sha256(
                f"blind-v2|{label}|{source}|{row['sample_id']}".encode()
            ).hexdigest()
        )
    sources = sorted(
        buckets,
        key=lambda source: hashlib.sha256(f"blind-v2-source|{label}|{source}".encode()).hexdigest(),
    )
    ordered: list[dict[str, Any]] = []
    offset = 0
    while True:
        added = False
        for source in sources:
            if offset < len(buckets[source]):
                ordered.append(buckets[source][offset])
                added = True
        if not added:
            return ordered
        offset += 1


def select_blind(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible = [
        row for row in rows
        if row["event_date"] >= BLIND_MIN_EVENT_DATE
        and row["source_group"] != "sharadar_active_research"
        and row["adjudication_confidence"] >= 0.86
        and (
            strict_evidence_row(row)
            or (
                row["label"] == "ABSTAIN"
                and row["axes"]["evidence_state"] in {"DISCOVERY_ONLY", "INSUFFICIENT", "CONFLICTED"}
                and len(row["text"]) >= 20
            )
        )
    ]
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in eligible:
        by_label[row["label"]].append(row)
    blind: list[dict[str, Any]] = []
    used_entities: set[str] = set()
    used_near: set[str] = set()
    for label, target in BLIND_TARGETS.items():
        candidates = source_balanced_order(by_label[label], label)
        selected: list[dict[str, Any]] = []
        for row in candidates:
            duplicate = near_duplicate_key(row)
            if row["entity_group"] in used_entities or duplicate in used_near:
                continue
            selected.append(row)
            used_entities.add(row["entity_group"])
            used_near.add(duplicate)
            if len(selected) == target:
                break
        if len(selected) < target:
            raise RuntimeError(f"insufficient blind-v2 rows for {label}: {len(selected)}/{target}")
        blind.extend(selected)
    blind_ids = {row["sample_id"] for row in blind}
    blind_entities = {row["entity_group"] for row in blind if row["entity_group"]}
    blind_chains = {row["event_chain_group"] for row in blind if row["event_chain_group"]}
    blind_near = {near_duplicate_key(row) for row in blind}
    development = [
        row for row in rows
        if row["sample_id"] not in blind_ids
        and (not row["entity_group"] or row["entity_group"] not in blind_entities)
        and (not row["event_chain_group"] or row["event_chain_group"] not in blind_chains)
        and near_duplicate_key(row) not in blind_near
    ]
    return development, sorted(blind, key=lambda row: row["sample_id"])


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(stable_json(row) for row in rows) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dev", type=Path, default=DEFAULT_DEV)
    parser.add_argument("--blind", type=Path, default=DEFAULT_BLIND)
    parser.add_argument("--freeze", type=Path, default=DEFAULT_FREEZE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    args = parser.parse_args()

    overrides = load_ai_overrides(args.overrides)
    raw_rows = load_rows(args.db)
    adjudications = [adjudicate(row, overrides) for row in raw_rows]
    development_all, blind = select_blind(adjudications)
    development = [row for row in development_all if training_eligible(row)]
    abstain_rows = sorted(
        (row for row in development if row["label"] == "ABSTAIN"),
        key=lambda row: hashlib.sha256(f"v3-dev-abstain|{row['sample_id']}".encode()).hexdigest(),
    )
    retained_abstain_ids = {row["sample_id"] for row in abstain_rows[:400]}
    development = [
        row for row in development
        if row["label"] != "ABSTAIN" or row["sample_id"] in retained_abstain_ids
    ]
    write_jsonl(args.dev, development)
    blind_rows = [
        {
            **row,
            "expected_label": row.pop("label"),
            "prediction": None,
        }
        for row in (dict(item) for item in blind)
    ]
    write_jsonl(args.blind, blind_rows)
    dataset_sha256 = hashlib.sha256(args.blind.read_bytes()).hexdigest()
    policy_sha256 = hashlib.sha256(stable_json(POLICY).encode()).hexdigest()
    freeze = {
        "schema_version": 2,
        "freeze_id": f"external-blind-v2-{dataset_sha256[:12]}",
        "frozen_at": utc_now(),
        "dataset_sha256": dataset_sha256,
        "rows": len(blind_rows),
        "label_counts": dict(Counter(row["expected_label"] for row in blind_rows)),
        "source_counts": dict(Counter(row["source_group"] for row in blind_rows)),
        "blind_min_event_date": BLIND_MIN_EVENT_DATE,
        "adjudication_policy_sha256": policy_sha256,
        "adjudicator": AI_ADJUDICATOR,
        "reviewer_type": AI_REVIEWER_TYPE,
        "human_labels_claimed": False,
        "label_policy_locked_before_inference": True,
        "predictions_present": False,
        "training_dataset_path": args.dev.name,
        "overlap_rules": {
            "event_and_sample_id_disjoint": True,
            "entity_group_disjoint_with_blind": True,
            "blind_internal_entity_duplicate_count": len(blind_rows)
            - len({row["entity_group"] for row in blind_rows}),
            "blind_internal_near_duplicate_count": len(blind_rows)
            - len({near_duplicate_key(row) for row in blind_rows}),
            "near_duplicate_prefix_hash_disjoint": True,
        },
        "minimums": BLIND_TARGETS,
        "no_trading": True,
    }
    args.freeze.write_text(json.dumps(freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {
        "schema_version": 1,
        "created_at": utc_now(),
        "source_database": args.db.name,
        "source_database_sha256": hashlib.sha256(args.db.read_bytes()).hexdigest(),
        "policy": POLICY,
        "policy_sha256": policy_sha256,
        "adjudicator": AI_ADJUDICATOR,
        "reviewer_type": AI_REVIEWER_TYPE,
        "human_labels_claimed": False,
        "total_rows": len(adjudications),
        "development_rows": len(development),
        "blind_rows": len(blind_rows),
        "development_label_counts": dict(Counter(row["label"] for row in development)),
        "blind_label_counts": freeze["label_counts"],
        "blind_source_counts": freeze["source_counts"],
        "axis_counts": {
            axis: dict(Counter(row["axes"][axis] for row in adjudications))
            for axis in ("materiality", "polarity", "evidence_state")
        },
        "reason_counts": dict(Counter(code for row in adjudications for code in row["reason_codes"])),
        "quality_checks": {
            "all_rows_have_text_hash": all(row["text_sha256"] for row in adjudications),
            "all_rows_have_rationale": all(len(row["rationale"]) >= 20 for row in adjudications),
            "all_rows_explicitly_not_human": all(not row["human_reviewed"] for row in adjudications),
            "blind_minimums_met": all(freeze["label_counts"].get(k, 0) >= v for k, v in BLIND_TARGETS.items()),
            "blind_source_groups_gte_4": len(freeze["source_counts"]) >= 4,
            "blind_predictions_absent": all(row["prediction"] is None for row in blind_rows),
        },
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# Risk Router v3 AI label audit",
        "",
        f"- AI adjudicator: `{AI_ADJUDICATOR}`",
        f"- Provenance: `{AI_REVIEWER_TYPE}`; these are not represented as human labels.",
        f"- Total / development / frozen blind rows: `{len(adjudications)}` / `{len(development)}` / `{len(blind_rows)}`",
        f"- Development labels: `{stable_json(report['development_label_counts'])}`",
        f"- Blind labels: `{stable_json(freeze['label_counts'])}`",
        f"- Blind source groups: `{len(freeze['source_counts'])}`",
        f"- Blind dataset SHA-256: `{dataset_sha256}`",
        f"- Policy SHA-256: `{policy_sha256}`",
        "",
        "Each row is adjudicated on materiality, polarity and evidence state. Source identity and event taxonomy are retained only in audit context and are excluded from learned text.",
    ]
    args.markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({
        "development_rows": len(development),
        "development_label_counts": report["development_label_counts"],
        "blind_rows": len(blind_rows),
        "blind_label_counts": freeze["label_counts"],
        "blind_source_groups": len(freeze["source_counts"]),
        "quality_checks": report["quality_checks"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
