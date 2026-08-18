"""Bounded, evidence-first light verification for candidate events.

``SUPPORTED`` is deliberately hard to obtain.  This module may move a candidate
to ``verified`` only when one addressable primary-source passage passes all of
the identity, event-fact, date, and modality gates.  A failed gate is not a
negative conclusion: it leaves the canonical event alone and creates a durable
follow-up task containing the missing evidence dimensions.

The shadow model is observational only.  It never decides a formal state and a
same-input comparison is explicitly marked ``UNCHANGED / NOT_APPLICABLE``.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

from app.models import RiskRouter, derive_evidence_context
from scripts.event_ledger import stable_json, utc_now


LIGHT_VERIFICATION_VERSION = "light-evidence-gate-v3-subject-bound"
LEGACY_LIGHT_VERIFICATION_VERSION = "light-evidence-gate-v1"
LIGHT_VERIFIED_EVIDENCE_STATUS = "accepted_light_primary_evidence"
LIGHT_FOLLOWUP_JOB_TYPE = "light_verification_followup"
MAX_EVIDENCE_OBJECTS = 2
MAX_EXCERPT_CHARS = 6000
MIN_EXCERPT_CHARS = 80
ALLOWED_EVENT_STATUSES = {"candidate"}
ALLOWED_EVIDENCE_STATUSES = {
    "candidate_passage",
    "machine_extracted_unreviewed",
    "confirmed_primary",
    "accepted_manual_primary_evidence",
    LIGHT_VERIFIED_EVIDENCE_STATUS,
}
ALLOWED_PRIMARY_TIERS = {"P0", "P1"}
BLOCKING_ROUGH_OUTCOMES = {"ROUGH_CONFLICT", "ROUGH_UNRESOLVED"}
# A bounded passage check can safely confirm only a small set of discrete,
# source-record events.  Threshold, price, ratio and accounting events need a
# claim-specific quantitative parser plus a comparison against the event fact;
# a lexical match is never enough to turn them into a formal conclusion.
AUTO_FORMAL_EVENT_TYPES = frozenset(
    {
        "delisted",
        "voluntarydelisting",
        "reverse_split",
        "bankruptcy_liquidation",
    }
)
STATUS_RANK = {
    "accepted_manual_primary_evidence": 0,
    "confirmed_primary": 1,
    "machine_extracted_unreviewed": 2,
    "candidate_passage": 3,
    LIGHT_VERIFIED_EVIDENCE_STATUS: 0,
}


EVENT_SIGNAL_PATTERNS = {
    "delisted": r"delist|delisting|listing rule|form 25|suspend",
    "voluntarydelisting": r"delist|delisting|form 25",
    "reverse_split": r"reverse split|reverse stock split|share consolidation",
    "bankruptcy_liquidation": r"bankruptcy|chapter 11|liquidation|insolvency",
    # Keep this ASCII-only.  The previous escaped ``\\s`` form matched a
    # literal backslash and made this taxonomy unreliable on real passages.
    "negative_equity": r"negative equity|accumulated deficit|shareholders.{0,3}deficit|stockholders.{0,3}deficit|total equity\s*\(",
    "revenue_collapse_yoy": r"(?:revenue|sales).{0,100}(?:decreas|declin|collapse|down|loss|year over year)",
    "cash_short_debt_stress": r"cash and cash equivalents.{0,120}(?:debt|shortfall|negative)|liquidity (?:risk|position|constraint|crisis)|debt obligations|substantial doubt|covenant|default",
    "interest_coverage_below_1": r"interest coverage|coverage ratio|covenant|default|operating loss",
    "gross_margin_collapse": r"gross margin.{0,100}(?:decreas|declin|collapse|negative)|gross profit.{0,100}(?:decreas|declin|loss)",
    "free_cash_flow_turn_negative": r"free cash flow|cash flow.{0,100}capital expenditures",
    "one_day_crash": r"(?:stock|share) price.{0,60}(?:decreas|declin|drop|loss)|price decline",
    "five_day_crash": r"(?:stock|share) price.{0,60}(?:decreas|declin|drop|loss)|price decline",
    "twenty_one_day_crash": r"(?:stock|share) price.{0,60}(?:decreas|declin|drop|loss)|price decline",
    "volume_crash": r"trading volume.{0,60}(?:decreas|declin|drop)|volume (?:decreas|declin|drop)",
}

_COMPANY_STOPWORDS = {"inc", "corp", "co", "ltd", "llc", "the"}
_NEGATION_OR_COUNTERCLAIM = re.compile(
    r"\b(?:not|never|no longer|withdrawn|withdrawal|rescinded|cancelled|canceled|"
    r"terminated|denied|denies|disputed|disputes|refuted|remains listed|continues to be listed)\b",
    re.IGNORECASE,
)
_SPECULATIVE_MODALITY = re.compile(
    r"\b(?:may|might|could|would|expects?|intends?|plans?|potential(?:ly)?|risk of|"
    r"possible|forward-looking)\b",
    re.IGNORECASE,
)
_ASSERTED_MODALITY = re.compile(
    r"\b(?:was|were|is|are|has|have|had|filed|announced|reported|determined|"
    r"approved|completed|will be|will suspend|trading will be)\b",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY = re.compile(
    r"[.!?;。！？；]|,\s*(?:while|whereas|but|although|however)\b|"
    r"\b(?:while|whereas|but|although|however)\b",
    re.IGNORECASE,
)
_THIRD_PARTY_RELATION = re.compile(
    r"\b(?:customer|client|vendor|supplier|subsidiary|affiliate|partner|"
    r"portfolio company|borrower|tenant|distributor)\b",
    re.IGNORECASE,
)
_STRUCTURAL_SELF_REFERENCE = re.compile(
    r"\b(?:the company|the issuer|the registrant|our common stock|its common stock)\b",
    re.IGNORECASE,
)


def _compact(value: Any, limit: int = MAX_EXCERPT_CHARS) -> str:
    return " ".join(str(value or "").split())[:limit]


def _parse_keywords(value: Any) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        text = str(value or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            parsed = None
        raw = parsed if isinstance(parsed, list) else re.split(r"[;,|]", text)
    return [" ".join(str(item).casefold().split()) for item in raw if str(item).strip()]


def _valid_url(value: Any) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _source_date(row: dict[str, Any]) -> date | None:
    for key in ("filing_date", "source_published_at", "updated_at"):
        parsed = _parse_date(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _event_date(event: dict[str, Any]) -> date | None:
    return _parse_date(event.get("event_date"))


def _normalized_cik(row: dict[str, Any]) -> str | None:
    for key in ("cik", "issuer_cik", "company_cik", "entity_cik"):
        digits = re.sub(r"\D", "", str(row.get(key) or ""))
        if digits:
            return digits.lstrip("0") or "0"
    return None


def _identity_check(event: dict[str, Any], evidence: dict[str, Any], haystack: str) -> tuple[bool, dict[str, Any]]:
    """Require a stable issuer identity using CIK when available, otherwise a
    ticker plus company name or a complete multi-word company name.

    This intentionally rejects the former loose "two company words anywhere"
    heuristic.  It remains workable for current evidence rows, which generally
    contain issuer name but not CIK.
    """

    normalized = haystack.casefold()
    event_cik = _normalized_cik(event)
    evidence_cik = _normalized_cik(evidence)
    ticker = str(event.get("ticker_at_event") or "").strip().casefold()
    ticker_match = bool(
        ticker
        and len(ticker) >= 2
        and re.search(rf"(?<![a-z0-9]){re.escape(ticker)}(?![a-z0-9])", normalized)
    )
    company = str(event.get("company_name") or "").strip().casefold()
    words = [
        item
        for item in re.findall(r"[a-z0-9]{2,}", company)
        if item not in _COMPANY_STOPWORDS
    ]
    company_phrase = " ".join(words)
    company_full_match = bool(
        company_phrase
        and (
            company_phrase in normalized
            or (len(words) >= 2 and all(re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", normalized) for word in words))
        )
    )
    if event_cik:
        stable = bool(evidence_cik and evidence_cik == event_cik)
        basis = "cik" if stable else "cik_mismatch_or_missing"
    elif ticker:
        stable = bool(ticker_match and (company_full_match or len(words) <= 1))
        basis = "ticker_and_company" if stable and company_full_match else ("ticker_only" if stable else "ticker_or_company_not_stable")
    else:
        stable = bool(company_full_match and len(words) >= 2)
        basis = "complete_company_name" if stable else "company_name_not_stable"
    return stable, {
        "stable_identity": stable,
        "basis": basis,
        "event_cik": event_cik,
        "evidence_cik": evidence_cik,
        "ticker_match": ticker_match,
        "company_full_match": company_full_match,
    }


def _event_signal(event: dict[str, Any], evidence: dict[str, Any], claim_text: str) -> tuple[bool, list[str], list[tuple[int, int]]]:
    keywords = _parse_keywords(evidence.get("matched_keywords"))
    event_type = str(event.get("event_type") or "").casefold()
    family = str(event.get("event_family") or "").casefold()
    pattern = EVENT_SIGNAL_PATTERNS.get(event_type) or EVENT_SIGNAL_PATTERNS.get(family)
    if not pattern:
        return False, [], []
    normalized = claim_text.casefold()
    matches = list(re.finditer(pattern, normalized))
    if not matches:
        return False, [], []
    matched = [keyword for keyword in keywords if keyword and keyword in normalized]
    matched.extend(match.group(0) for match in matches)
    return True, list(dict.fromkeys(matched))[:8], [(match.start(), match.end()) for match in matches]


def _clause_for_span(text: str, span: tuple[int, int]) -> tuple[int, int, str]:
    start, end = span
    clause_start = 0
    clause_end = len(text)
    for boundary in _CLAUSE_BOUNDARY.finditer(text):
        if boundary.end() <= start:
            clause_start = boundary.end()
            continue
        if boundary.start() >= end:
            clause_end = boundary.start()
            break
    return clause_start, clause_end, text[clause_start:clause_end].strip()


def _subject_event_binding_check(
    event: dict[str, Any],
    evidence: dict[str, Any],
    claim_text: str,
    signal_spans: list[tuple[int, int]],
) -> tuple[bool, list[tuple[int, int]], dict[str, Any]]:
    """Require the target issuer and event predicate in one local clause."""

    bound_spans: list[tuple[int, int]] = []
    clause_checks: list[dict[str, Any]] = []
    event_cik = _normalized_cik(event)
    evidence_cik = _normalized_cik(evidence)
    for span in signal_spans:
        clause_start, clause_end, clause = _clause_for_span(claim_text, span)
        _, local_identity = _identity_check(event, evidence, clause)
        explicit_identity = bool(
            local_identity.get("ticker_match") or local_identity.get("company_full_match")
        )
        third_party_relation = bool(_THIRD_PARTY_RELATION.search(clause))
        structured_self_reference = bool(
            event_cik
            and evidence_cik == event_cik
            and _STRUCTURAL_SELF_REFERENCE.search(clause)
            and not third_party_relation
        )
        bound = bool(
            not third_party_relation
            and (explicit_identity or structured_self_reference)
        )
        if bound:
            bound_spans.append(span)
        clause_checks.append(
            {
                "clause_start": clause_start,
                "clause_end": clause_end,
                "clause": clause,
                "explicit_identity": explicit_identity,
                "structured_self_reference": structured_self_reference,
                "third_party_relation": third_party_relation,
                "bound": bound,
            }
        )
    return bool(bound_spans), bound_spans, {
        "bound": bool(bound_spans),
        "bound_signal_count": len(bound_spans),
        "signal_count": len(signal_spans),
        "clauses": clause_checks,
        "reason": (
            "issuer_and_event_predicate_bound_in_local_clause"
            if bound_spans
            else "issuer_not_bound_to_event_predicate"
        ),
    }


def _automatic_formal_eligibility(event: dict[str, Any]) -> tuple[bool, str]:
    """Return whether this taxonomy may ever use the bounded auto-formal path.

    The allow-list is intentionally much narrower than the discovery taxonomy.
    An otherwise well-sourced threshold/market/fundamental candidate remains a
    useful nonterminal evidence task, but it cannot be called formally verified
    until a dedicated quantitative verifier exists.
    """

    event_type = str(event.get("event_type") or "").casefold()
    if event_type in AUTO_FORMAL_EVENT_TYPES:
        return True, "discrete_source_record_event"
    return False, "event_type_requires_quantitative_or_human_verification"


def _modality_check(claim_text: str, signal_spans: list[tuple[int, int]]) -> tuple[bool, str]:
    """Reject a signal when its local context says it did not happen.

    We deliberately bias false rather than promote a denial, withdrawal, or a
    merely hypothetical forward-looking risk statement as an event fact.
    """

    normalized = claim_text.casefold()
    for start, end in signal_spans:
        window = normalized[max(0, start - 100) : min(len(normalized), end + 140)]
        if _NEGATION_OR_COUNTERCLAIM.search(window):
            return False, "negated_withdrawn_or_counterclaimed"
        speculative = bool(_SPECULATIVE_MODALITY.search(window))
        asserted = bool(_ASSERTED_MODALITY.search(window))
        if speculative and not asserted:
            return False, "speculative_or_forward_looking"
        if not asserted:
            return False, "event_modality_not_asserted"
    return True, "asserted_or_completed"


def _rank_evidence(row: dict[str, Any]) -> tuple[int, int, int, int]:
    tier = str(row.get("authority_tier") or "P9")
    tier_rank = int(tier[1:]) if len(tier) > 1 and tier[1:].isdigit() else 9
    status_rank = STATUS_RANK.get(str(row.get("evidence_status") or ""), 9)
    return tier_rank, status_rank, -int(row.get("passage_score") or 0), -len(str(row.get("evidence_passage") or ""))


def select_primary_evidence(evidence: list[dict[str, Any]], limit: int = MAX_EVIDENCE_OBJECTS) -> list[dict[str, Any]]:
    """Select at most two distinct addressable primary passages; never fetch."""

    candidates = [
        row
        for row in evidence
        if str(row.get("authority_tier") or "") in ALLOWED_PRIMARY_TIERS
        and str(row.get("evidence_status") or "") in ALLOWED_EVIDENCE_STATUSES
        and _valid_url(row.get("evidence_url"))
        and MIN_EXCERPT_CHARS <= len(_compact(row.get("evidence_passage"))) <= MAX_EXCERPT_CHARS
    ]
    selected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for row in sorted(candidates, key=_rank_evidence):
        url = str(row.get("evidence_url") or "")
        if url in seen_urls:
            continue
        selected.append(row)
        seen_urls.add(url)
        if len(selected) >= max(1, min(int(limit), MAX_EVIDENCE_OBJECTS)):
            break
    return selected


def evidence_fingerprint(event: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    """Stable fingerprint used to avoid re-consuming unchanged candidates.

    It covers every retained evidence row, not only the currently selected row,
    so a new primary passage re-opens an earlier insufficient/skip outcome.
    """

    event_part = {
        key: str(event.get(key) or "")
        for key in ("event_id", "current_version", "event_family", "event_type", "event_date", "company_name", "ticker_at_event")
    }
    evidence_part = [
        {
            key: str(row.get(key) or "")
            for key in (
                "evidence_id",
                "evidence_status",
                "authority_tier",
                "evidence_url",
                "filing_date",
                "source_published_at",
                "updated_at",
                "evidence_passage",
                "observation_title",
                "observation_summary",
                "matched_keywords",
            )
        }
        for row in sorted(evidence, key=lambda item: str(item.get("evidence_id") or ""))
    ]
    return hashlib.sha256(stable_json({"event": event_part, "evidence": evidence_part}).encode("utf-8")).hexdigest()


def evidence_receipt_rows(connection: sqlite3.Connection, event_id: str) -> list[dict[str, Any]]:
    """Return the provenance-enriched evidence receipt used by every scope gate.

    A formal authorization must bind the same fields that were evaluated:
    evidence row state plus its source/observation provenance.  Keeping this
    query here lets the write transaction re-read exactly that receipt instead
    of accidentally comparing a bare ``event_evidence`` row with an enriched
    read-model record.
    """

    return [
        dict(item)
        for item in connection.execute(
            """SELECT ev.*, o.title AS observation_title, o.summary AS observation_summary,
                      o.source_published_at, o.local_received_at, s.source_id, s.name AS source_name,
                      s.authority_tier, s.source_type
               FROM event_evidence ev
               JOIN raw_observations o ON o.observation_id=ev.observation_id
               JOIN sources s ON s.source_id=o.source_id
               WHERE ev.event_id=?
               ORDER BY ev.evidence_id""",
            (event_id,),
        )
    ]


def _model_text(event: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    facts = event.get("facts") if isinstance(event.get("facts"), dict) else {}
    sections = [
        str(event.get("company_name") or ""),
        str(event.get("ticker_at_event") or ""),
        str(event.get("event_family") or ""),
        str(event.get("event_type") or ""),
        str(event.get("event_date") or ""),
        str(facts.get("evidence_summary") or ""),
    ]
    for item in evidence[:MAX_EVIDENCE_OBJECTS]:
        sections.extend(
            [
                str(item.get("observation_title") or ""),
                str(item.get("observation_summary") or ""),
                str(item.get("evidence_passage") or ""),
            ]
        )
    return " ".join(_compact(item, 5000) for item in sections if _compact(item))[:20000]


def model_snapshot(router: RiskRouter, event: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Run the CPU-only shadow router once for a bounded comparison."""

    result = router.predict(_model_text(event, evidence), evidence_context=derive_evidence_context(evidence))
    snapshot = {
        key: result.get(key)
        for key in (
            "label",
            "confidence",
            "probabilities",
            "model_version",
            "runtime",
            "decision_source",
            "confidence_applicable",
            "semantic_model_invoked",
            "evidence_gate",
            "input_sha256",
            "no_trading",
        )
    }
    snapshot["event_version"] = int(event.get("current_version") or 0)
    snapshot["shadow"] = True
    snapshot["latency_ms"] = float(result.get("latency_ms") or 0)
    return snapshot


def model_delta(before_model: dict[str, Any], after_model: dict[str, Any]) -> dict[str, Any]:
    """Explain whether a shadow comparison had a meaningful changed input."""

    same_input = bool(before_model.get("input_sha256")) and before_model.get("input_sha256") == after_model.get("input_sha256")
    if same_input:
        return {
            "status": "UNCHANGED",
            "confidence": "NOT_APPLICABLE",
            "input_changed": False,
            "model_view_changed": False,
            "reason": "shadow input fingerprint is unchanged; no model reassessment is applicable",
        }
    comparable = (
        before_model.get("label"),
        before_model.get("confidence"),
        before_model.get("input_sha256"),
    ) != (
        after_model.get("label"),
        after_model.get("confidence"),
        after_model.get("input_sha256"),
    )
    return {
        "status": "CHANGED" if comparable else "UNCHANGED",
        "confidence": "NOT_APPLICABLE" if not comparable else "OBSERVATIONAL_ONLY",
        "input_changed": True,
        "model_view_changed": comparable,
        "reason": "shadow comparison only; it does not determine the formal evidence state",
    }


def _gap_reasons(best: dict[str, Any], *, automatic_formal_eligible: bool = True) -> list[str]:
    reasons: list[str] = []
    if not best.get("identity_match"):
        reasons.append("stable issuer identity is missing or inconsistent")
    if not best.get("event_signal"):
        reasons.append("event taxonomy signal is absent from the primary passage")
    if best.get("event_signal") and not best.get("subject_event_bound"):
        reasons.append("event predicate is not bound to the target issuer in the same clause")
    if not best.get("modality_safe"):
        reasons.append(f"event claim is not decision-grade: {best.get('modality_reason') or 'unknown modality'}")
    if not best.get("date_coherent"):
        reasons.append("source date is not within the permitted 366-day event window")
    if int(best.get("excerpt_chars") or 0) < MIN_EXCERPT_CHARS:
        reasons.append("primary passage is too short to support a claim")
    if not automatic_formal_eligible:
        reasons.append(
            "event type requires a quantitative fact gate or human verification before a formal conclusion"
        )
    return reasons or ["primary evidence requires human review before a formal conclusion"]


def evaluate_event(event: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a deterministic, non-mutating light-verification decision."""

    before_version = int(event.get("current_version") or 0)
    fingerprint = evidence_fingerprint(event, evidence)
    if str(event.get("status") or "").lower() not in ALLOWED_EVENT_STATUSES:
        return {
            "version": LIGHT_VERIFICATION_VERSION,
            "event_id": event.get("event_id"),
            "decision": "SKIPPED",
            "before_version": before_version,
            "evidence_ids": [],
            "evidence_fingerprint": fingerprint,
            "score": 0,
            "rationale": "only candidate events are eligible for light verification",
            "checks": {},
            "gap_reasons": ["event is no longer a candidate and requires its existing workflow"],
        }

    selected = select_primary_evidence(evidence)
    if not selected:
        return {
            "version": LIGHT_VERIFICATION_VERSION,
            "event_id": event.get("event_id"),
            "decision": "SKIPPED",
            "before_version": before_version,
            "evidence_ids": [],
            "evidence_fingerprint": fingerprint,
            "score": 0,
            "rationale": "no bounded primary passage is available; wait for evidence collection",
            "checks": {"primary_passage": False},
            "gap_reasons": ["no eligible P0/P1 primary passage is available", "collect an addressable primary-source passage"],
        }

    contradiction_statuses = {
        str(item.get("evidence_status") or "").casefold()
        for item in evidence
        if any(marker in str(item.get("evidence_status") or "").casefold() for marker in ("contradict", "conflict", "rejected_primary"))
    }
    if contradiction_statuses:
        return {
            "version": LIGHT_VERIFICATION_VERSION,
            "event_id": event.get("event_id"),
            "decision": "CONFLICT",
            "before_version": before_version,
            "evidence_ids": [str(item["evidence_id"]) for item in selected],
            "evidence_fingerprint": fingerprint,
            "score": 0,
            "rationale": "contradictory or rejected primary evidence requires human review",
            "checks": {"conflict": True, "statuses": sorted(contradiction_statuses)},
            "gap_reasons": ["contradictory or rejected primary evidence requires human adjudication"],
        }

    automatic_formal_eligible, automatic_formal_reason = _automatic_formal_eligibility(event)
    checks: list[dict[str, Any]] = []
    for item in selected:
        excerpt = _compact(item.get("evidence_passage"))
        identity_haystack = " ".join(
            [
                excerpt,
                _compact(item.get("observation_title")),
                _compact(item.get("observation_summary")),
                str(item.get("form") or ""),
                str(item.get("items") or ""),
            ]
        ).casefold()
        identity, identity_detail = _identity_check(event, item, identity_haystack)
        signal, matched_keywords, signal_spans = _event_signal(event, item, excerpt)
        subject_event_bound, bound_signal_spans, subject_binding = (
            _subject_event_binding_check(event, item, excerpt, signal_spans)
            if signal
            else (False, [], {"bound": False, "reason": "event_signal_absent", "clauses": []})
        )
        modality_safe, modality_reason = (
            _modality_check(excerpt, bound_signal_spans)
            if subject_event_bound
            else (
                False,
                "issuer_not_bound_to_event_predicate" if signal else "event_signal_absent",
            )
        )
        source_date = _source_date(item)
        event_date = _event_date(event)
        date_gap_days = abs((source_date - event_date).days) if source_date and event_date else None
        date_coherent = date_gap_days is not None and date_gap_days <= 366
        score = 0
        score += 30 if str(item.get("authority_tier") or "") == "P0" else 25
        score += 20 if len(excerpt) >= MIN_EXCERPT_CHARS else 0
        score += 20 if identity else 0
        score += 20 if signal and subject_event_bound and modality_safe else 0
        score += 10 if date_coherent else 0
        checks.append(
            {
                "evidence_id": str(item["evidence_id"]),
                "authority_tier": item.get("authority_tier"),
                "evidence_status": item.get("evidence_status"),
                "url": item.get("evidence_url"),
                "score": score,
                "identity_match": identity,
                "identity_detail": identity_detail,
                "event_signal": signal,
                "matched_keywords": matched_keywords,
                "subject_event_bound": subject_event_bound,
                "subject_binding": subject_binding,
                "modality_safe": modality_safe,
                "modality_reason": modality_reason,
                "date_coherent": date_coherent,
                "source_date": source_date.isoformat() if source_date else None,
                "event_date": event_date.isoformat() if event_date else None,
                "date_gap_days": date_gap_days,
                "excerpt_chars": len(excerpt),
                "automatic_formal_eligible": automatic_formal_eligible,
                "automatic_formal_reason": automatic_formal_reason,
            }
        )

    best = max(checks, key=lambda item: int(item["score"]))
    supported = bool(
        automatic_formal_eligible
        and best["identity_match"]
        and best["event_signal"]
        and best["subject_event_bound"]
        and best["modality_safe"]
        and best["date_coherent"]
        and best["excerpt_chars"] >= MIN_EXCERPT_CHARS
    )
    decision = "SUPPORTED" if supported else "INSUFFICIENT"
    rationale = (
        "primary passage passes stable-identity, subject-event, event-fact, date and modality gates"
        if supported
        else (
            "primary passage is retained for review, but this event type is not eligible for automatic formal verification"
            if not automatic_formal_eligible
            else "primary passage exists but failed one or more mandatory light-verification gates"
        )
    )
    return {
        "version": LIGHT_VERIFICATION_VERSION,
        "event_id": event.get("event_id"),
        "decision": decision,
        "before_version": before_version,
        "evidence_ids": [str(item["evidence_id"]) for item in selected],
        "evidence_fingerprint": fingerprint,
        "score": int(best["score"]),
        "rationale": rationale,
        "checks": checks,
        "gap_reasons": []
        if supported
        else _gap_reasons(best, automatic_formal_eligible=automatic_formal_eligible),
        "claim_summary": {
            "subject": event.get("company_name") or event.get("ticker_at_event") or event.get("event_id"),
            "predicate": event.get("event_type"),
            "event_date": event.get("event_date"),
            "source_date": best.get("source_date"),
            "modality": best.get("modality_reason"),
            "supporting_evidence": [str(best["evidence_id"])],
        },
        "budget": {
            "primary_documents_read": len(selected),
            "max_primary_documents": MAX_EVIDENCE_OBJECTS,
            "model_calls": 0,
            "max_model_calls": 2,
            "network_fetches": 0,
            "automatic_retries": 0,
        },
    }


def _load_json(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _latest_rough_outcome(connection: sqlite3.Connection, event_id: str) -> str | None:
    row = connection.execute(
        """SELECT payload_json FROM pipeline_jobs
           WHERE event_id=? AND status='COMPLETED_AUTHORIZED_ROUGH_REVIEW'
           ORDER BY updated_at DESC,job_id DESC LIMIT 1""",
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    rough = _load_json(row["payload_json"]).get("rough_review", {})
    if not isinstance(rough, dict):
        return None
    return str(rough.get("outcome") or "").upper() or None


def _followup_job_id(event_id: str) -> str:
    return f"light-followup-{hashlib.sha256(event_id.encode('utf-8')).hexdigest()[:24]}"


def _persist_followup(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    before_version: int,
    result: dict[str, Any],
    batch_id: str,
    force_human_review: bool = False,
    legacy: bool = False,
    authorization_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create/update a durable nonterminal task without changing event truth."""

    now = utc_now()
    previous = connection.execute(
        "SELECT priority,attempts,payload_json FROM pipeline_jobs WHERE event_id=? AND job_type=?",
        (event_id, LIGHT_FOLLOWUP_JOB_TYPE),
    ).fetchone()
    existing_payload = _load_json(previous["payload_json"]) if previous is not None else {}
    existing_attempts = int(previous["attempts"] or 0) if previous is not None else 0
    priority_row = connection.execute("SELECT MAX(priority) AS priority FROM pipeline_jobs WHERE event_id=?", (event_id,)).fetchone()
    priority = max(50, int(priority_row["priority"] or 0) if priority_row is not None else 0)
    decision = str(result.get("decision") or "SKIPPED")
    reasons = [str(item) for item in result.get("gap_reasons", []) if str(item).strip()]
    if not reasons:
        reasons = [str(result.get("rationale") or "light verification requires follow-up")]
    human_review = force_human_review or decision == "CONFLICT"
    followup = {
        "version": LIGHT_VERIFICATION_VERSION,
        "batch_id": batch_id,
        "last_attempted_at": now,
        "attempt_count": existing_attempts + 1,
        "decision": decision,
        "evidence_fingerprint": result.get("evidence_fingerprint"),
        "evidence_ids": result.get("evidence_ids", []),
        "gap_reasons": reasons,
        "expected_next_action": "human_review" if human_review else "collect_or_review_primary_evidence",
        "original_event_version": before_version,
        "formal_verification": False,
        "no_trading": True,
        "legacy_reconciliation": bool(legacy),
        "authorization": dict(authorization_context or {}),
    }
    payload = dict(existing_payload)
    payload["light_verification_followup"] = followup
    status = "PENDING_HUMAN_REVIEW" if human_review else "PENDING_EVIDENCE_REVIEW"
    connection.execute(
        """INSERT INTO pipeline_jobs(
               job_id,event_id,job_type,status,priority,attempts,available_at,last_error,
               payload_json,created_at,updated_at
           ) VALUES (?,?,?,?,?,1,?,?,?, ?,?)
           ON CONFLICT(event_id,job_type) DO UPDATE SET
               status=excluded.status,
               priority=MAX(pipeline_jobs.priority,excluded.priority),
               attempts=pipeline_jobs.attempts+1,
               available_at=excluded.available_at,
               last_error=excluded.last_error,
               payload_json=excluded.payload_json,
               updated_at=excluded.updated_at""",
        (
            _followup_job_id(event_id),
            event_id,
            LIGHT_FOLLOWUP_JOB_TYPE,
            status,
            priority,
            now,
            "; ".join(reasons)[:500],
            stable_json(payload),
            now,
            now,
        ),
    )
    return {"job_type": LIGHT_FOLLOWUP_JOB_TYPE, "status": status, "payload": followup}


def _formal_budget_block_reason(
    connection: sqlite3.Connection,
    *,
    batch_id: str,
    daily_budget: int | None,
    max_batch_applies: int | None,
) -> str | None:
    """Check new v2 formal mutations inside the ledger write transaction."""

    if daily_budget is not None:
        today = utc_now()[:10]
        count = int(
            connection.execute(
                """SELECT COUNT(*) FROM event_versions
                   WHERE change_reason='light_evidence_verification_v2' AND changed_at LIKE ?""",
                (f"{today}%",),
            ).fetchone()[0]
        )
        if count >= max(0, int(daily_budget)):
            return "daily formal-application budget is exhausted"
    if max_batch_applies is not None:
        count = int(
            connection.execute(
                """SELECT COUNT(*) FROM event_versions
                   WHERE change_reason='light_evidence_verification_v2'
                     AND json_valid(facts_json)
                     AND json_extract(facts_json,'$.light_verification.batch_id')=?""",
                (batch_id,),
            ).fetchone()[0]
        )
        if count >= max(0, int(max_batch_applies)):
            return "scoped batch formal-application budget is exhausted"
    return None


def _validate_formal_scope(
    connection: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    result: dict[str, Any],
    authorization_context: dict[str, Any] | None,
    batch_id: str,
    require_current_support: bool = True,
) -> None:
    """Re-read and bind the exact evidence receipt inside the write transaction."""

    if not authorization_context:
        raise ValueError("formal light verification requires a scoped authorization context")
    for key in ("authorization_id", "actor", "purpose", "expires_at", "batch_id"):
        if not str(authorization_context.get(key) or "").strip():
            raise ValueError(f"formal light verification authorization is missing {key}")
    if str(authorization_context["batch_id"]) != batch_id:
        raise ValueError("formal light verification authorization batch does not match the write target")
    expiry_text = str(authorization_context["expires_at"]).replace("Z", "+00:00")
    try:
        expires_at = datetime.fromisoformat(expiry_text)
    except ValueError as exc:
        raise ValueError("formal light verification authorization expires_at is invalid") from exc
    if expires_at.tzinfo is None:
        raise ValueError("formal light verification authorization expires_at must include a timezone")
    if expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise ValueError("formal light verification authorization is expired")
    scope_entry = authorization_context.get("scope_entry")
    if not isinstance(scope_entry, dict):
        raise ValueError("formal light verification authorization is missing an exact scope entry")
    event_id = str(row["event_id"])
    before_version = int(row["current_version"])
    if str(scope_entry.get("event_id") or "") != event_id:
        raise ValueError("formal authorization event id does not match the write target")
    if int(scope_entry.get("current_version") or -1) != before_version:
        raise ValueError("formal authorization event version does not match the write target")
    evidence = evidence_receipt_rows(connection, event_id)
    current_fingerprint = evidence_fingerprint(dict(row), evidence)
    evaluated_fingerprint = str(result.get("evidence_fingerprint") or "")
    authorized_fingerprint = str(scope_entry.get("evidence_fingerprint") or "")
    if not evaluated_fingerprint or current_fingerprint != evaluated_fingerprint:
        raise ValueError("evidence changed since light verification evaluation")
    if authorized_fingerprint != current_fingerprint:
        raise ValueError("formal authorization evidence fingerprint does not match the write target")
    if require_current_support:
        fresh_result = evaluate_event(dict(row), evidence)
        if fresh_result.get("decision") != "SUPPORTED":
            raise ValueError("event no longer passes the strict formal light-verification gates")
        if list(fresh_result.get("evidence_ids") or []) != list(result.get("evidence_ids") or []):
            raise ValueError("formal light verification evidence selection no longer matches the write target")


def apply_event(
    connection: sqlite3.Connection,
    result: dict[str, Any],
    *,
    batch_id: str,
    before_model: dict[str, Any],
    after_model: dict[str, Any],
    authorization_context: dict[str, Any] | None = None,
    daily_budget: int | None = None,
    max_batch_applies: int | None = None,
) -> dict[str, Any]:
    """Apply a formal support only; all other outcomes persist a follow-up.

    Callers must hold ``BEGIN IMMEDIATE``.  This makes the event version CAS and
    formal daily/batch budget checks race-safe within the ledger database.
    """

    event_id = str(result["event_id"])
    row = connection.execute("SELECT * FROM canonical_events WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown event: {event_id}")
    before_version = int(row["current_version"])
    if before_version != int(result["before_version"]):
        raise ValueError(f"event changed since evaluation: {event_id}")
    decision = str(result["decision"])
    if decision not in {"SUPPORTED", "INSUFFICIENT", "CONFLICT", "SKIPPED"}:
        raise ValueError(f"unknown light verification decision: {decision}")

    delta = result.get("model_delta") or model_delta(before_model, after_model)
    base = {
        **result,
        "batch_id": batch_id,
        "before_model": before_model,
        "after_model": after_model,
        "model_delta": delta,
        "authorization_context": authorization_context or {},
    }
    rough_outcome = _latest_rough_outcome(connection, event_id)
    blocked_by_rough = rough_outcome in BLOCKING_ROUGH_OUTCOMES
    if decision != "SUPPORTED" or blocked_by_rough:
        if blocked_by_rough:
            base["gap_reasons"] = list(base.get("gap_reasons") or []) + [
                f"rough-review outcome {rough_outcome} requires human adjudication before formal application"
            ]
        followup = _persist_followup(
            connection,
            event_id=event_id,
            before_version=before_version,
            result=base,
            batch_id=batch_id,
            force_human_review=blocked_by_rough,
            authorization_context=authorization_context,
        )
        return {
            **base,
            "applied": False,
            "formal_applied": False,
            "attempt_persisted": True,
            "after_version": before_version,
            "followup": followup,
            "application_blocked_reason": (
                f"rough-review outcome {rough_outcome}" if blocked_by_rough else None
            ),
            "created_at": utc_now(),
        }

    if str(row["status"] or "").lower() != "candidate":
        raise ValueError(f"event is no longer a candidate: {event_id}")
    if str(row["event_type"] or "").casefold() not in AUTO_FORMAL_EVENT_TYPES:
        raise ValueError("event type is not eligible for automatic formal light verification")
    _validate_formal_scope(
        connection,
        row=row,
        result=result,
        authorization_context=authorization_context,
        batch_id=batch_id,
    )
    budget_block = _formal_budget_block_reason(
        connection,
        batch_id=batch_id,
        daily_budget=daily_budget,
        max_batch_applies=max_batch_applies,
    )
    if budget_block:
        return {
            **base,
            "applied": False,
            "formal_applied": False,
            "attempt_persisted": False,
            "after_version": before_version,
            "application_blocked_reason": budget_block,
            "created_at": utc_now(),
        }

    now = utc_now()
    facts_row = connection.execute(
        "SELECT facts_json FROM event_versions WHERE event_id=? AND version=?",
        (event_id, before_version),
    ).fetchone()
    facts = _load_json(facts_row["facts_json"] if facts_row is not None else "{}")
    facts["light_verification"] = {
        "version": LIGHT_VERIFICATION_VERSION,
        "method": "bounded_primary_passage_gate",
        "formal_conclusion": "verified",
        "reviewed_at": now,
        "batch_id": batch_id,
        "authorization": authorization_context or {},
        "evidence_ids": result.get("evidence_ids", []),
        "evidence_fingerprint": result.get("evidence_fingerprint"),
        "score": result.get("score", 0),
        "checks": result.get("checks", []),
        "claim_summary": result.get("claim_summary", {}),
        "rationale": result.get("rationale", ""),
        "budget": result.get("budget", {}),
        "model_reassessment": {"before": before_model, "after": after_model, "delta": delta},
        "auto_verification_allowed": False,
        "no_trading": True,
    }
    new_version = before_version + 1
    update_cursor = connection.execute(
        """UPDATE canonical_events SET current_version=?,status='verified',label_status='verified',
           last_updated_at=?,no_trading=1 WHERE event_id=? AND current_version=? AND status='candidate'""",
        (new_version, now, event_id, before_version),
    )
    if update_cursor.rowcount != 1:
        raise ValueError(f"event was not updated atomically: {event_id}")
    connection.execute(
        """INSERT INTO event_versions(
           event_id,version,changed_at,status,label_status,event_family,event_type,
           manual_grade,facts_json,change_reason
           ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            new_version,
            now,
            "verified",
            "verified",
            row["event_family"],
            row["event_type"],
            row["manual_grade"],
            stable_json(facts),
            "light_evidence_verification_v2",
        ),
    )
    placeholders = ",".join("?" for _ in result.get("evidence_ids", []))
    if placeholders:
        connection.execute(
            f"UPDATE event_evidence SET evidence_status=?,auto_verification_allowed=0,updated_at=? WHERE event_id=? AND evidence_id IN ({placeholders})",
            (LIGHT_VERIFIED_EVIDENCE_STATUS, now, event_id, *result["evidence_ids"]),
        )
    connection.execute(
        """UPDATE pipeline_jobs SET status='COMPLETED_LIGHT_VERIFICATION',attempts=attempts+1,
           last_error=NULL,payload_json=?,updated_at=?
           WHERE event_id=? AND status IN ('PENDING_EVIDENCE_REVIEW','PENDING_PRIMARY_EVIDENCE','PENDING_HUMAN_REVIEW')""",
        (
            stable_json(
                {
                    "batch_id": batch_id,
                    "light_verification": {
                        "version": LIGHT_VERIFICATION_VERSION,
                        "decision": "SUPPORTED",
                        "evidence_ids": result.get("evidence_ids", []),
                        "formal_verification": True,
                        "no_trading": True,
                    },
                }
            ),
            now,
            event_id,
        ),
    )
    return {
        **base,
        "applied": True,
        "formal_applied": True,
        "attempt_persisted": True,
        "after_version": new_version,
        "created_at": now,
    }


def reconcile_legacy_event(
    connection: sqlite3.Connection,
    *,
    event_id: str,
    batch_id: str,
    authorization_context: dict[str, Any],
) -> dict[str, Any]:
    """Re-open a v1 light-verification record without reverting its history."""

    row = connection.execute("SELECT * FROM canonical_events WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown event: {event_id}")
    version = connection.execute(
        "SELECT facts_json,change_reason FROM event_versions WHERE event_id=? AND version=?",
        (event_id, row["current_version"]),
    ).fetchone()
    facts = _load_json(version["facts_json"] if version is not None else "{}")
    light = facts.get("light_verification") if isinstance(facts.get("light_verification"), dict) else {}
    legacy_version = str(light.get("version") or "")
    if legacy_version != LEGACY_LIGHT_VERIFICATION_VERSION and str(version["change_reason"] if version is not None else "") != "light_evidence_verification_v1":
        return {
            "event_id": event_id,
            "reopened": False,
            "reason": "event is not currently a v1 light-verification record",
        }
    evidence = evidence_receipt_rows(connection, event_id)
    _validate_formal_scope(
        connection,
        row=row,
        result={
            "event_id": event_id,
            "evidence_fingerprint": evidence_fingerprint(dict(row), evidence),
        },
        authorization_context=authorization_context,
        batch_id=batch_id,
        require_current_support=False,
    )
    event = dict(row)
    result = {
        "version": LIGHT_VERIFICATION_VERSION,
        "event_id": event_id,
        "decision": "CONFLICT" if str(row["status"]).lower() == "verified" else "INSUFFICIENT",
        "before_version": int(row["current_version"]),
        "evidence_ids": list(light.get("evidence_ids") or []),
        "evidence_fingerprint": evidence_fingerprint(event, evidence),
        "gap_reasons": [
            "legacy v1 light verification did not require the current identity, event-fact, date and modality gates",
            "preserve the original version; obtain human review or new primary evidence before any new formal conclusion",
        ],
        "rationale": "legacy light-verification reconciliation",
    }
    followup = _persist_followup(
        connection,
        event_id=event_id,
        before_version=int(row["current_version"]),
        result=result,
        batch_id=batch_id,
        force_human_review=str(row["status"]).lower() == "verified",
        legacy=True,
        authorization_context=authorization_context,
    )
    return {
        "event_id": event_id,
        "reopened": True,
        "original_status": row["status"],
        "original_version": int(row["current_version"]),
        "followup": followup,
        "gap_reasons": result["gap_reasons"],
    }
