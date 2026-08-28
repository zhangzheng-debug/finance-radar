"""Pure evidence predicates and immutable dual-human review receipts.

This module deliberately depends only on the Python standard library so the
write path, product reader and shadow model cannot drift into three different
interpretations of the same evidence state.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Mapping

from app.source_url_policy import is_public_source_url


CONFLICTING_EVIDENCE_STATUSES = frozenset(
    {
        "contradicted",
        "contradicted_by_primary",
        "conflict",
        "conflicted",
        "denied",
        "denied_by_primary",
        "disputed",
        "disputed_by_primary",
        "rejected",
        "rejected_primary",
        "retracted",
        "retracted_by_primary",
        "withdrawn",
        "withdrawn_by_primary",
    }
)

STANDARD_READER_EVIDENCE_STATUSES = frozenset(
    {
        "machine_extracted_unreviewed",
        "candidate_passage",
        "confirmed_primary",
        "accepted_manual_primary_evidence",
        "accepted_light_primary_evidence",
    }
)
DUAL_HUMAN_EVIDENCE_STATUS = "accepted_dual_human_primary_evidence"
DUAL_HUMAN_SELECTED_EVIDENCE_RECEIPT_V1 = "dual-human-selected-evidence-receipt-v1"
DUAL_HUMAN_SELECTED_EVIDENCE_RECEIPT_VERSION = (
    "dual-human-selected-evidence-receipt-v2"
)
HUMAN_FACT_CLAIM_CONTRACT_VERSION = "human-fact-claim-v1"
HUMAN_FACT_SUBJECT_BASES = frozenset({"EXACT_IN_PASSAGE"})
HUMAN_FACT_STAGES = frozenset(
    {"PROPOSED", "FILED", "DISCLOSED", "EFFECTIVE", "ONGOING", "COMPLETED"}
)
HUMAN_FACT_MODALITIES = frozenset(
    {"REALIZED", "PROPOSED_OR_CONDITIONAL", "UNCLEAR"}
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PRIMARY_AUTHORITY_TIER_RE = re.compile(r"P[01](?:_[A-Z0-9]+)*", re.I)
_SUBJECT_ACTION_AUXILIARIES = frozenset(
    {"has", "had", "have", "is", "was", "were", "are", "did", "does", "do"}
)
_SUBJECT_ACTION_ADVERBS = frozenset(
    {"formally", "officially", "successfully", "voluntarily", "immediately", "now", "also"}
)
_SUBJECT_ACTION_ALLOWED_GAPS = frozenset(
    {""}
    | _SUBJECT_ACTION_AUXILIARIES
    | _SUBJECT_ACTION_ADVERBS
    | {
        f"{auxiliary} {adverb}"
        for auxiliary in _SUBJECT_ACTION_AUXILIARIES
        for adverb in _SUBJECT_ACTION_ADVERBS
    }
)
_SUBJECT_ACTION_GAP_EDGE_CHARS = " \t\r\n,;:()[]-–—"
SUBJECT_ACTION_BINDING_CONTRACT_VERSION = "minimal-subject-action-clause-v1"
REALIZED_LANGUAGE_GATE_CONTRACT_VERSION = "realized-language-fail-closed-v1"
REALIZED_ACTION_HEAD_CONTRACT_VERSION = "realized-action-head-allowlist-v1"
HUMAN_FACT_PREDICATE_CONTRACT_VERSION = "human-fact-predicate-map-v1"
_HUMAN_FACT_PREDICATE_RULES: dict[str, dict[str, Any]] = {
    "BANKRUPTCY_PETITION_FILED": {
        "event_types": frozenset(
            {"bankruptcy_liquidation", "bankruptcy_or_distress", "chapter_11"}
        ),
        "event_families": frozenset({"bankruptcy_or_distress"}),
        "action_heads": frozenset({"filed"}),
        "object_pattern": r"\b(?:chapter\s+(?:7|11)|bankruptcy|petition(?:\s+for\s+relief)?)\b",
    },
    "LIQUIDATION_COMMENCED": {
        "event_types": frozenset({"bankruptcy_liquidation", "bankruptcy_or_distress"}),
        "event_families": frozenset({"bankruptcy_or_distress"}),
        "action_heads": frozenset({"commenced", "liquidated", "dissolved"}),
        "object_pattern": r"\b(?:liquidation|dissolution|winding[ -]up|chapter\s+7)\b",
    },
    "OFFICER_DEPARTED": {
        "event_types": frozenset({"management_change", "management_departure"}),
        "event_families": frozenset({"management_change"}),
        "action_heads": frozenset(
            {"resigned", "retired", "departed", "removed", "dismissed", "terminated"}
        ),
        "object_pattern": r"\b(?:officer|director|president|chair(?:man|woman|person)?|ceo|cfo)\b",
    },
    "OFFICER_APPOINTED": {
        "event_types": frozenset(
            {"management_change", "chief_financial_officer_appointment"}
        ),
        "event_families": frozenset({"management_change"}),
        "action_heads": frozenset({"appointed", "named", "elected", "designated"}),
        "object_pattern": r"\b(?:officer|director|president|chair(?:man|woman|person)?|ceo|cfo)\b",
    },
    "DELISTED_OR_SUSPENDED": {
        "event_types": frozenset(
            {"delisted", "delisting", "listing_status", "trading_paused", "voluntarydelisting"}
        ),
        "event_families": frozenset({"delisting_or_suspension", "listing_status"}),
        "action_heads": frozenset({"delisted", "suspended", "ceased"}),
        "object_pattern": r"\b(?:listing|trading|common\s+(?:stock|shares)|ordinary\s+shares|securit(?:y|ies))\b",
    },
    "LISTING_NOTICE_RECEIVED": {
        "event_types": frozenset(
            {"listing_status", "minimum_bid_price_deficiency_notice"}
        ),
        "event_families": frozenset({"delisting_or_suspension", "listing_status"}),
        "action_heads": frozenset({"received"}),
        "object_pattern": r"\b(?:notice|notification|deficien(?:cy|t)|noncompliance)\b",
    },
    "SECURITIES_ISSUED_OR_SOLD": {
        "event_types": frozenset(
            {
                "financing", "offering_completed", "offering_or_dilution",
                "convertible_debt_financing", "senior_unsecured_debt_financing",
            }
        ),
        "event_families": frozenset({"financing_or_dilution", "offering_or_dilution"}),
        "action_heads": frozenset({"issued", "sold"}),
        "object_pattern": r"\b(?:stock|shares?|warrants?|notes?|securit(?:y|ies)|offering|placement)\b",
    },
    "OFFERING_PRICED": {
        "event_types": frozenset(
            {"financing", "offering_completed", "offering_or_dilution"}
        ),
        "event_families": frozenset({"financing_or_dilution", "offering_or_dilution"}),
        "action_heads": frozenset({"priced"}),
        "object_pattern": r"\b(?:offering|placement|stock|shares?|notes?|securit(?:y|ies))\b",
    },
    "OFFERING_COMPLETED_OR_CLOSED": {
        "event_types": frozenset(
            {"financing", "offering_completed", "offering_or_dilution"}
        ),
        "event_families": frozenset({"financing_or_dilution", "offering_or_dilution"}),
        "action_heads": frozenset({"completed", "closed", "consummated"}),
        "object_pattern": r"\b(?:offering|placement|financing|sale\s+of\s+securit(?:y|ies))\b",
    },
    "DEBT_REFINANCED_OR_FACILITY_AMENDED": {
        "event_types": frozenset(
            {"debt_refinancing", "credit_facility_amendment", "financing"}
        ),
        "event_families": frozenset({"financing_or_dilution", "debt_or_liquidity"}),
        "action_heads": frozenset({"refinanced", "repaid", "amended"}),
        "object_pattern": r"\b(?:debt|notes?|loan|credit\s+facility|facility|agreement)\b",
    },
    "MERGER_OR_ACQUISITION_COMPLETED": {
        "event_types": frozenset(
            {"merger_completed", "merger_or_acquisition", "material_corporate_transaction"}
        ),
        "event_families": frozenset({"merger_or_acquisition"}),
        "action_heads": frozenset({"completed", "closed", "consummated"}),
        "object_pattern": r"\b(?:merger|acquisition|transaction|business\s+combination)\b",
    },
    "TRANSACTION_AGREEMENT_ENTERED": {
        "event_types": frozenset(
            {"merger_or_acquisition", "material_corporate_transaction"}
        ),
        "event_families": frozenset({"merger_or_acquisition"}),
        "action_heads": frozenset({"entered", "executed", "signed"}),
        "object_pattern": r"\b(?:agreement|merger|acquisition|transaction)\b",
    },
    "TRANSACTION_TERMINATED": {
        "event_types": frozenset(
            {"merger_or_acquisition", "material_corporate_transaction"}
        ),
        "event_families": frozenset({"merger_or_acquisition"}),
        "action_heads": frozenset({"terminated"}),
        "object_pattern": r"\b(?:agreement|merger|acquisition|transaction)\b",
    },
    "REVERSE_SPLIT_EFFECTIVE": {
        "event_types": frozenset({"reverse_split"}),
        "event_families": frozenset({"capital_structure"}),
        "action_heads": frozenset({"completed", "effected", "implemented"}),
        "object_pattern": r"\b(?:reverse\s+(?:stock\s+)?split|share\s+consolidation)\b",
    },
    "DEBT_DEFAULT_OR_COVENANT_BREACH": {
        "event_types": frozenset({"debt_default", "cash_short_debt_stress"}),
        "event_families": frozenset({"debt_or_liquidity"}),
        "action_heads": frozenset({"defaulted", "breached"}),
        "object_pattern": r"\b(?:debt|notes?|loan|covenant|credit\s+facility|agreement)\b",
    },
    "ENFORCEMENT_ACTION_FILED": {
        "event_types": frozenset({"sec_litigation_release"}),
        "event_families": frozenset({"regulatory_or_legal"}),
        "action_heads": frozenset({"filed"}),
        "object_pattern": r"\b(?:complaint|charges?|enforcement\s+action|lawsuit|proceeding)\b",
    },
}
_REALIZED_ACTION_HEADS = frozenset(
    head
    for rule in _HUMAN_FACT_PREDICATE_RULES.values()
    for head in rule["action_heads"]
)
_KNOWN_HUMAN_FACT_EVENT_TYPES = frozenset(
    event_type
    for rule in _HUMAN_FACT_PREDICATE_RULES.values()
    for event_type in rule["event_types"]
)
_NON_REALIZED_LANGUAGE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"not|never|without|did(?:n't|n’t)|fail|failed|fails|failing|"
    r"decline|declined|declines|declining|cancel|canceled|cancelled|cancels|canceling|cancelling|"
    r"abandon|abandoned|abandons|abandoning|rescind|rescinded|rescinds|rescinding|"
    r"consider|considered|considers|considering|explore|explored|explores|exploring|"
    r"seek|seeks|seeking|sought|"
    r"schedule|scheduled|schedules|scheduling|set\s+to|attempt|attempted|attempts|attempting|"
    r"deny|denied|denies|denying|refuse|refused|refuses|refusing|"
    r"withdraw|withdrew|withdrawn|withdraws|withdrawing|allege|alleged|alleges|alleging|"
    r"expect|expected|expects|expecting|plan|planned|plans|planning|intend|intended|"
    r"intends|intending|propose|proposed|proposes|proposing|purport|purported|purports|"
    r"purporting|suggest|suggested|suggests|suggesting|claim|claimed|claims|claiming|"
    r"may|might|could|would|will|shall|if|unless|subject\s+to|contingent\s+on|"
    r"conditional\s+on|rumou?r|hearsay|hypothetical|counterfactual|example|"
    r"illustrative|headline|title|phrase|quote|quoted|read|reads|scenario|pro\s+forma"
    r")(?![A-Za-z0-9])",
    re.I,
)


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _exact_text(value: Any) -> str:
    return str(value or "")


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _sqlite_json_sha256(value: Any) -> str | None:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return _sha256_json(parsed)


def _sqlite_text_sha256(value: Any) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _action_head(action_quote: Any) -> str:
    match = re.match(
        r"[A-Za-z]+(?:['’][A-Za-z]+)?",
        _exact_text(action_quote).lstrip(" \t\r\n,;:()[]-–—"),
    )
    return match.group(0).casefold() if match is not None else ""


def allowed_human_fact_predicates(
    event_type: Any, event_family: Any = ""
) -> tuple[str, ...]:
    normalized_type = _text(event_type).casefold()
    # Formal conclusions are exact-type claims.  A broad family is useful for
    # queue routing, but it cannot prove a narrower event type (for example,
    # a Chapter 11 petition does not prove cancellation of old common stock).
    # Unknown types therefore fail closed until they are explicitly mapped.
    _ = event_family
    resolved: list[str] = []
    for fact_predicate, rule in _HUMAN_FACT_PREDICATE_RULES.items():
        if normalized_type in rule["event_types"]:
            resolved.append(fact_predicate)
    return tuple(sorted(resolved))


def _human_fact_predicate_compatible(
    fact_predicate: Any,
    event_type: Any,
    event_family: Any,
    action_quote: Any,
    object_quote: Any,
) -> bool:
    predicate = _text(fact_predicate).upper()
    rule = _HUMAN_FACT_PREDICATE_RULES.get(predicate)
    if rule is None or predicate not in allowed_human_fact_predicates(
        event_type, event_family
    ):
        return False
    if _action_head(action_quote) not in rule["action_heads"]:
        return False
    object_text = _exact_text(object_quote).strip()
    return bool(object_text) and re.search(
        rule["object_pattern"], object_text, re.I
    ) is not None


def _sqlite_human_fact_predicate_compatible(
    fact_predicate: Any,
    event_type: Any,
    event_family: Any,
    action_quote: Any,
    object_quote: Any,
) -> int:
    return int(
        _human_fact_predicate_compatible(
            fact_predicate,
            event_type,
            event_family,
            action_quote,
            object_quote,
        )
    )


def _fact_quote_context(
    passage: Any,
    fact_sentence_quote: Any,
) -> dict[str, Any] | None:
    source = _exact_text(passage)
    quote = _exact_text(fact_sentence_quote)
    if not quote or source.count(quote) != 1:
        return None
    start = source.find(quote)
    end = start + len(quote)
    left = start - 1
    while left >= 0 and source[left] in " \t\r":
        left -= 1
    if left >= 0 and source[left] not in ".?!\n":
        return None
    quote_tail = quote.rstrip(" \t\r\n")
    right = end
    while right < len(source) and source[right] in " \t\r":
        right += 1
    if not quote_tail or (
        quote_tail[-1] not in ".?!\n"
        and right < len(source)
        and source[right] not in ".?!\n"
    ):
        return None
    return {
        "fact_sentence_start": start,
        "fact_sentence_end": end,
        "evidence_passage_sha256": hashlib.sha256(
            source.encode("utf-8")
        ).hexdigest(),
    }


def _sqlite_fact_quote_context_valid(
    passage: Any,
    fact_sentence_quote: Any,
    claimed_start: Any,
    claimed_end: Any,
) -> int:
    context = _fact_quote_context(passage, fact_sentence_quote)
    if context is None:
        return 0
    try:
        return int(
            int(claimed_start) == context["fact_sentence_start"]
            and int(claimed_end) == context["fact_sentence_end"]
        )
    except (TypeError, ValueError):
        return 0


def _realized_claim_language_safe(
    action_quote: Any,
    fact_sentence_quote: Any,
    subject_surface_quote: Any,
    evidence_passage: Any,
) -> bool:
    action = _exact_text(action_quote)
    sentence = _exact_text(fact_sentence_quote).lstrip(" \t\r\n")
    subject_surface = _exact_text(subject_surface_quote)
    if not action or not subject_surface or not sentence.startswith(subject_surface):
        return False
    if _action_head(action) not in _REALIZED_ACTION_HEADS:
        return False
    # Ignore only the already-bound subject surface.  A company named "May" or
    # "Will" is not future language; the same token anywhere after the subject
    # is fail-closed.
    asserted_clause = sentence[len(subject_surface) :]
    if _NON_REALIZED_LANGUAGE_RE.search(asserted_clause) is not None:
        return False
    full_context = _exact_text(evidence_passage)
    # Do not treat a canonical company/ticker literally named May or Will as a
    # modal, but scan every other word in the frozen selected passage.
    context_without_subject = full_context.replace(subject_surface, " ")
    return _NON_REALIZED_LANGUAGE_RE.search(context_without_subject) is None


def _sqlite_realized_claim_language_safe(
    action_quote: Any,
    fact_sentence_quote: Any,
    subject_surface_quote: Any,
    evidence_passage: Any,
) -> int:
    return int(
        _realized_claim_language_safe(
            action_quote,
            fact_sentence_quote,
            subject_surface_quote,
            evidence_passage,
        )
    )


def register_sqlite_integrity_functions(connection: sqlite3.Connection) -> None:
    """Register deterministic hashes required by the fail-closed reader SQL."""

    connection.create_function(
        "json_sha256", 1, _sqlite_json_sha256, deterministic=True
    )
    connection.create_function(
        "text_sha256", 1, _sqlite_text_sha256, deterministic=True
    )
    connection.create_function(
        "realized_claim_language_safe",
        4,
        _sqlite_realized_claim_language_safe,
        deterministic=True,
    )
    connection.create_function(
        "human_fact_predicate_compatible",
        5,
        _sqlite_human_fact_predicate_compatible,
        deterministic=True,
    )
    connection.create_function(
        "fact_quote_context_valid",
        4,
        _sqlite_fact_quote_context_valid,
        deterministic=True,
    )
    connection.create_function(
        "public_source_url_ok",
        1,
        lambda value: int(is_public_source_url(value)),
        deterministic=True,
    )


def _exact_contiguous_quote(value: Any, passage: str, *, field: str) -> str:
    quote = _exact_text(value).strip()
    if not quote:
        raise ValueError(f"{field} is required")
    if len(quote) > 1200 or any(ord(character) < 32 and character not in "\n\r\t" for character in quote):
        raise ValueError(f"{field} is unsafe")
    if quote not in passage:
        raise ValueError(f"{field} must be an exact contiguous substring of selected passage")
    return quote


def _minimal_subject_action_binding(
    *, subject: str, action_quote: str, fact_sentence_quote: str
) -> dict[str, str] | None:
    """Bind a canonical subject to the action of one minimal quoted clause.

    Mere co-occurrence is unsafe: the subject may be background, an object, or
    a counterparty while a different company performs the action.  V2 therefore
    accepts only a reviewer-selected clause beginning with the exact subject
    (or ``$TICKER``), followed by punctuation/whitespace, at most one controlled
    auxiliary and one controlled adverb, then the exact action quote.
    """

    sentence = fact_sentence_quote.lstrip(" \t\r\n")
    surfaces = (f"${subject}", subject) if not subject.startswith("$") else (subject,)
    subject_surface = next(
        (
            surface
            for surface in surfaces
            if sentence.startswith(surface)
            and (
                len(sentence) == len(surface)
                or not sentence[len(surface)].isascii()
                or not sentence[len(surface)].isalnum()
            )
        ),
        "",
    )
    if not subject_surface:
        return None
    tail = sentence[len(subject_surface) :]
    action_position = tail.find(action_quote)
    if action_position < 0:
        return None
    gap_quote = tail[:action_position]
    if not gap_quote or gap_quote[0].isascii() and gap_quote[0].isalnum():
        return None
    normalized_gap = gap_quote.strip(_SUBJECT_ACTION_GAP_EDGE_CHARS).casefold()
    if normalized_gap not in _SUBJECT_ACTION_ALLOWED_GAPS:
        return None
    prefix_quote = subject_surface + gap_quote + action_quote
    return {
        "subject_action_binding_contract": SUBJECT_ACTION_BINDING_CONTRACT_VERSION,
        "subject_surface_quote": subject_surface,
        "subject_action_gap_quote": gap_quote,
        "subject_action_gap_normalized": normalized_gap,
        "subject_action_prefix_quote": prefix_quote,
    }


def canonicalize_human_fact_claim(
    raw_claim: Any,
    *,
    event: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and canonicalize one reviewer-authored concrete fact claim.

    The public sentence is deterministic and contains no free-form summary.
    Reviewers may only identify exact text already frozen in the selected
    passage, plus the canonical issuer when the selected document itself is
    issuer-bound.
    """

    if not isinstance(raw_claim, Mapping):
        raise ValueError("human_fact_claim is required")
    allowed = {
        "contract_version",
        "subject",
        "subject_basis",
        "predicate",
        "fact_predicate",
        "action_quote",
        "object_quote",
        "stage",
        "modality",
        "fact_sentence_quote",
        "fact_sentence_start",
        "fact_sentence_end",
        "evidence_passage_sha256",
        "event_date_or_effective_date",
    }
    extra = sorted(set(raw_claim) - allowed)
    missing = sorted((allowed - {"event_date_or_effective_date"}) - set(raw_claim))
    if extra:
        raise ValueError("human_fact_claim has unsupported fields: " + ",".join(extra))
    if missing:
        raise ValueError("human_fact_claim is missing fields: " + ",".join(missing))
    if _text(raw_claim.get("contract_version")) != HUMAN_FACT_CLAIM_CONTRACT_VERSION:
        raise ValueError(
            f"human_fact_claim.contract_version must be {HUMAN_FACT_CLAIM_CONTRACT_VERSION}"
        )
    passage = _exact_text(evidence.get("evidence_passage"))
    subject = _text(raw_claim.get("subject"))
    canonical_subjects = {
        _text(event.get("company_name")),
        _text(event.get("ticker_at_event")),
    } - {""}
    if not subject or subject not in canonical_subjects:
        raise ValueError("human_fact_claim.subject must equal the canonical company or ticker")
    subject_basis = _text(raw_claim.get("subject_basis")).upper()
    if subject_basis == "DOCUMENT_ISSUER":
        raise ValueError(
            "DOCUMENT_ISSUER is not publishable in event-fact-review-v2; use NEEDS_EVIDENCE"
        )
    if subject_basis not in HUMAN_FACT_SUBJECT_BASES:
        raise ValueError("human_fact_claim.subject_basis is invalid")
    predicate = _text(raw_claim.get("predicate"))
    if predicate != _text(event.get("event_type")):
        raise ValueError("human_fact_claim.predicate must equal the current event_type")
    action_quote = _exact_contiguous_quote(
        raw_claim.get("action_quote"), passage, field="human_fact_claim.action_quote"
    )
    fact_sentence_quote = _exact_contiguous_quote(
        raw_claim.get("fact_sentence_quote"),
        passage,
        field="human_fact_claim.fact_sentence_quote",
    )
    if len(fact_sentence_quote) < 12:
        raise ValueError("human_fact_claim.fact_sentence_quote is too short")
    if action_quote not in fact_sentence_quote:
        raise ValueError("human_fact_claim.action_quote must occur in fact_sentence_quote")
    if fact_sentence_quote.count(action_quote) != 1:
        raise ValueError(
            "human_fact_claim.action_quote must occur exactly once in fact_sentence_quote"
        )
    fact_quote_context = _fact_quote_context(passage, fact_sentence_quote)
    if fact_quote_context is None:
        raise ValueError(
            "human_fact_claim.fact_sentence_quote must occur once at explicit clause boundaries"
        )
    try:
        claimed_start = int(raw_claim.get("fact_sentence_start"))
        claimed_end = int(raw_claim.get("fact_sentence_end"))
    except (TypeError, ValueError) as exc:
        raise ValueError("human_fact_claim fact sentence offsets are required") from exc
    if (
        claimed_start != fact_quote_context["fact_sentence_start"]
        or claimed_end != fact_quote_context["fact_sentence_end"]
    ):
        raise ValueError("human_fact_claim fact sentence offsets do not match selected passage")
    if _text(raw_claim.get("evidence_passage_sha256")).casefold() != fact_quote_context[
        "evidence_passage_sha256"
    ]:
        raise ValueError("human_fact_claim evidence passage SHA-256 does not match")
    subject_action_binding = _minimal_subject_action_binding(
        subject=subject,
        action_quote=action_quote,
        fact_sentence_quote=fact_sentence_quote,
    )
    if subject_action_binding is None:
        raise ValueError(
            "human_fact_claim must quote a minimal clause in the form Subject [auxiliary/adverb] Action"
        )
    if not _realized_claim_language_safe(
        action_quote,
        fact_sentence_quote,
        subject_action_binding["subject_surface_quote"],
        passage,
    ):
        raise ValueError(
            "human_fact_claim contains future, conditional, negative, or epistemic language"
        )
    object_quote = _exact_text(raw_claim.get("object_quote")).strip()
    if object_quote:
        if len(object_quote) > 600 or object_quote not in passage:
            raise ValueError("human_fact_claim.object_quote must be an exact passage substring")
        if object_quote not in fact_sentence_quote:
            raise ValueError("human_fact_claim.object_quote must occur in fact_sentence_quote")
    fact_predicate = _text(raw_claim.get("fact_predicate")).upper()
    if not _human_fact_predicate_compatible(
        fact_predicate,
        event.get("event_type"),
        event.get("event_family"),
        action_quote,
        object_quote,
    ):
        raise ValueError(
            "human_fact_claim fact_predicate/action/object is not compatible with event_type"
        )
    stage = _text(raw_claim.get("stage")).upper()
    if stage not in HUMAN_FACT_STAGES:
        raise ValueError("human_fact_claim.stage is invalid")
    modality = _text(raw_claim.get("modality")).upper()
    if modality not in HUMAN_FACT_MODALITIES:
        raise ValueError("human_fact_claim.modality is invalid")
    if modality != "REALIZED":
        raise ValueError("confirmed human_fact_claim.modality must be REALIZED")
    if stage == "PROPOSED":
        raise ValueError("confirmed human_fact_claim.stage cannot be PROPOSED")
    event_date = _exact_text(raw_claim.get("event_date_or_effective_date")).strip()
    if event_date and event_date not in passage:
        raise ValueError(
            "human_fact_claim.event_date_or_effective_date must be an exact passage substring"
        )
    canonical = {
        "contract_version": HUMAN_FACT_CLAIM_CONTRACT_VERSION,
        "subject": subject,
        "subject_basis": subject_basis,
        "predicate": predicate,
        "fact_predicate": fact_predicate,
        "fact_predicate_contract": HUMAN_FACT_PREDICATE_CONTRACT_VERSION,
        "action_quote": action_quote,
        "object_quote": object_quote,
        "stage": stage,
        "modality": modality,
        "fact_sentence_quote": fact_sentence_quote,
        **fact_quote_context,
        "event_date_or_effective_date": event_date,
        "subject_identity_type": "",
        "subject_identity_value": "",
        **subject_action_binding,
        "realized_language_gate_contract": REALIZED_LANGUAGE_GATE_CONTRACT_VERSION,
        "realized_action_head_contract": REALIZED_ACTION_HEAD_CONTRACT_VERSION,
    }
    claim_sha256 = _sha256_json(canonical)
    public_summary = f"{subject}：{fact_sentence_quote}"
    return {
        **canonical,
        "canonical_claim_sha256": claim_sha256,
        "public_fact_summary": public_summary,
        "public_fact_summary_sha256": hashlib.sha256(
            public_summary.encode("utf-8")
        ).hexdigest(),
    }


def normalize_evidence_status(value: Any) -> str:
    """Normalize a persisted lifecycle state without inventing a new state."""

    return "_".join(str(value or "").replace("-", "_").casefold().split())


def is_conflicting_evidence_status(value: Any) -> bool:
    """Whether a persisted evidence lifecycle state requires human review."""

    return normalize_evidence_status(value) in CONFLICTING_EVIDENCE_STATUSES


def is_http_evidence_url(value: Any) -> bool:
    text = _text(value)
    return bool(text and len(text) <= 2048 and is_public_source_url(text))


def is_primary_authority_tier(value: Any) -> bool:
    """Accept canonical P0/P1 tiers and underscore-qualified subtiers only.

    The full-match boundary accepts production values such as ``P0_official``
    and ``P1_issuer_official`` while rejecting lookalike prefixes such as
    ``P00``, ``P01`` and ``P10``.
    """

    return _PRIMARY_AUTHORITY_TIER_RE.fullmatch(_text(value).upper()) is not None


def strict_selected_evidence_issues(evidence: Mapping[str, Any]) -> list[str]:
    """Return structural reasons a dual-human selection cannot be published."""

    issues: list[str] = []
    if not is_primary_authority_tier(evidence.get("authority_tier")):
        issues.append("SOURCE_NOT_EXACT_P0_P1")
    if not is_http_evidence_url(evidence.get("evidence_url")):
        issues.append("MISSING_HTTP_EVIDENCE_URL")
    if len(_exact_text(evidence.get("evidence_passage")).strip()) < 40:
        issues.append("MISSING_EXACT_PASSAGE")
    content_sha256 = _text(evidence.get("content_sha256")).casefold()
    if _SHA256_RE.fullmatch(content_sha256) is None:
        issues.append("MISSING_SOURCE_CONTENT_SHA256")
    if not _text(evidence.get("source_id")):
        issues.append("MISSING_SOURCE_ID")
    if _text(evidence.get("observation_status")).casefold() == "deleted":
        issues.append("SOURCE_REVISION_DELETED")
    if _text(evidence.get("latest_revision_kind")).casefold() == "edit" and not _flag(
        evidence.get("passage_currently_proven")
    ):
        issues.append("SOURCE_REVISION_PASSAGE_NOT_PROVEN")
    return issues


def build_dual_human_selected_evidence_receipt(
    evidence: Mapping[str, Any],
    *,
    event_id: str,
    event_version: int,
    evidence_fingerprint_before: str,
    canonical_claim_sha256: str,
    public_fact_summary_sha256: str,
) -> dict[str, Any]:
    """Freeze the exact selected evidence state used for a formal decision."""

    issues = strict_selected_evidence_issues(evidence)
    if issues:
        raise ValueError("SELECTED_EVIDENCE_NOT_CITABLE: " + ",".join(issues))
    passage = _exact_text(evidence.get("evidence_passage"))
    payload: dict[str, Any] = {
        "contract_version": DUAL_HUMAN_SELECTED_EVIDENCE_RECEIPT_VERSION,
        "event_id": _text(event_id),
        "event_version": int(event_version),
        "evidence_id": _text(evidence.get("evidence_id")),
        "evidence_status_before": _text(evidence.get("evidence_status")),
        "evidence_status_after": DUAL_HUMAN_EVIDENCE_STATUS,
        "evidence_url": _text(evidence.get("evidence_url")),
        "evidence_passage": passage,
        "evidence_passage_sha256": hashlib.sha256(passage.encode("utf-8")).hexdigest(),
        "source_id": _text(evidence.get("source_id")),
        "source_content_sha256": _text(evidence.get("content_sha256")).casefold(),
        "source_authority_tier": _text(evidence.get("authority_tier")).upper(),
        "source_observation_status": _text(evidence.get("observation_status")) or "captured",
        "source_revision_no": int(evidence.get("latest_revision_no") or 0),
        "source_revision_kind": _text(evidence.get("latest_revision_kind")) or "new",
        "source_passage_currently_proven": int(
            evidence.get("passage_currently_proven") or 0
        ),
        "evidence_fingerprint_before": _text(evidence_fingerprint_before),
        "canonical_claim_sha256": _text(canonical_claim_sha256).casefold(),
        "public_fact_summary_sha256": _text(public_fact_summary_sha256).casefold(),
    }
    if _SHA256_RE.fullmatch(payload["canonical_claim_sha256"]) is None:
        raise ValueError("canonical_claim_sha256 is invalid")
    if _SHA256_RE.fullmatch(payload["public_fact_summary_sha256"]) is None:
        raise ValueError("public_fact_summary_sha256 is invalid")
    payload["receipt_sha256"] = _sha256_json(payload)
    return payload


def dual_human_selected_evidence_receipt_matches(
    receipt: Any,
    evidence: Mapping[str, Any],
    *,
    event_id: str,
    event_version: int,
    evidence_fingerprint_before: str,
    canonical_claim_sha256: str,
    public_fact_summary_sha256: str,
) -> bool:
    """Verify an immutable post-apply receipt against the current source revision."""

    if not isinstance(receipt, dict):
        return False
    payload = dict(receipt)
    claimed_sha256 = _text(payload.pop("receipt_sha256", "")).casefold()
    if _SHA256_RE.fullmatch(claimed_sha256) is None or claimed_sha256 != _sha256_json(payload):
        return False
    passage = _exact_text(evidence.get("evidence_passage"))
    expected = {
        "contract_version": DUAL_HUMAN_SELECTED_EVIDENCE_RECEIPT_VERSION,
        "event_id": _text(event_id),
        "event_version": int(event_version),
        "evidence_id": _text(evidence.get("evidence_id")),
        "evidence_status_after": DUAL_HUMAN_EVIDENCE_STATUS,
        "evidence_url": _text(evidence.get("evidence_url")),
        "evidence_passage": passage,
        "evidence_passage_sha256": hashlib.sha256(passage.encode("utf-8")).hexdigest(),
        "source_id": _text(evidence.get("source_id")),
        "source_content_sha256": _text(evidence.get("content_sha256")).casefold(),
        "source_authority_tier": _text(evidence.get("authority_tier")).upper(),
        "source_observation_status": _text(evidence.get("observation_status")) or "captured",
        "source_revision_no": int(evidence.get("latest_revision_no") or 0),
        "source_revision_kind": _text(evidence.get("latest_revision_kind")) or "new",
        "source_passage_currently_proven": int(
            evidence.get("passage_currently_proven") or 0
        ),
        "evidence_fingerprint_before": _text(evidence_fingerprint_before),
        "canonical_claim_sha256": _text(canonical_claim_sha256).casefold(),
        "public_fact_summary_sha256": _text(public_fact_summary_sha256).casefold(),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        return False
    if _text(evidence.get("evidence_status")) != DUAL_HUMAN_EVIDENCE_STATUS:
        return False
    return not strict_selected_evidence_issues(
        {**dict(evidence), "evidence_status": payload.get("evidence_status_before")}
    )


def _flag(value: Any) -> bool:
    try:
        return int(value) == 1
    except (TypeError, ValueError):
        return False


def is_strict_dual_human_evidence(evidence: Mapping[str, Any]) -> bool:
    """Dual-human support requires the relation flags and live receipt match."""

    return (
        _text(evidence.get("evidence_status")) == DUAL_HUMAN_EVIDENCE_STATUS
        and _text(evidence.get("relation_status")) == "HUMAN_CONFIRMED"
        and _flag(evidence.get("subject_match"))
        and _flag(evidence.get("event_claim_supported"))
        and _flag(evidence.get("date_coherent"))
        and _flag(evidence.get("dual_human_receipt_consistent"))
    )


def is_reader_supporting_evidence(evidence: Mapping[str, Any]) -> bool:
    """Apply the common public-reader evidence predicate in Python callers."""

    if "reader_eligible" not in evidence or not _flag(evidence.get("reader_eligible")):
        # Machine and human receipt validation lives in the ledger query.  A
        # presentation helper must not recreate a weaker parallel gate from a
        # handful of status flags.
        return False
    if _text(evidence.get("observation_status")).casefold() == "deleted":
        return False
    if _text(evidence.get("latest_revision_kind")).casefold() == "edit" and not _flag(
        evidence.get("passage_currently_proven", 1)
    ):
        return False
    if not is_http_evidence_url(evidence.get("evidence_url")):
        return False
    if len(_exact_text(evidence.get("evidence_passage")).strip()) < 40:
        return False
    if not is_primary_authority_tier(evidence.get("authority_tier")):
        return False
    if _text(evidence.get("evidence_status")) == DUAL_HUMAN_EVIDENCE_STATUS:
        return is_strict_dual_human_evidence(evidence)
    return (
        _text(evidence.get("evidence_status")) in STANDARD_READER_EVIDENCE_STATUSES
        and _text(evidence.get("relation_status")) in {"SCOPED_MATCH", "HUMAN_CONFIRMED"}
        and _flag(evidence.get("subject_match"))
        and _flag(evidence.get("event_claim_supported"))
        and _flag(evidence.get("date_coherent"))
    )
