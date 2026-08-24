from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from app.evidence_policy import is_primary_authority_tier


LEGACY_ADMISSION_CONTRACT_VERSION = "event-admission-v1"
PREVIOUS_ADMISSION_CONTRACT_VERSION = "event-admission-v2"
ADMISSION_CONTRACT_VERSION = "event-admission-v3"
FACT_SLOT_CONTRACT_VERSION = "deterministic-evidence-fact-slots-v2"
DISCOVERY_LEAD_STATES = {
    "PENDING_ENRICHMENT",
    "NEEDS_EVIDENCE",
    "LEAD_NO_SCOPED_EVENT",
    "READY_FOR_CANONICAL",
    "PROMOTED",
    "DUPLICATE",
    "EXCLUDED",
}
EVENT_FACT_STATES = {
    "NEEDS_EVIDENCE",
    "EVIDENCE_READY",
    "NEEDS_HUMAN",
    "DUPLICATE",
    "EXCLUDED",
}
SUPPORTED_RELATION_STATES = {"SCOPED_MATCH", "HUMAN_CONFIRMED"}
READER_ALLOWED_EVIDENCE_STATUSES = {
    "machine_extracted_unreviewed",
    "candidate_passage",
    "confirmed_primary",
    "accepted_manual_primary_evidence",
    "accepted_light_primary_evidence",
}
READER_BLOCKED_EVIDENCE_STATUSES = {
    "machine_extracted_non_decision",
    "attachment_incomplete",
    "link_only_no_relevant_passage",
    "no_keyword_passage",
}
EVENT_STAGES = {
    "PROPOSED",
    "FILED",
    "DISCLOSED",
    "EFFECTIVE",
    "ONGOING",
    "COMPLETED",
}


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    workflow_state: str
    reasons: tuple[str, ...]
    evidence_fingerprint: str
    fact_slot_receipt_sha256: str


@dataclass(frozen=True)
class EvidenceFactSlots:
    """One event fact whose displayed values are bounded to an exact passage.

    Normalized ``predicate`` and ``modality`` values are deterministic labels;
    every human-readable value ending in ``_text`` is an exact substring of
    ``evidence_sentence``.  The issuer supplied by feed metadata is never
    inserted into ``subject_text`` unless that issuer name occurs in the
    passage itself.
    """

    subject_text: str | None
    subject_binding: str
    issuer_name_explicit: bool
    actor_text: str | None
    predicate: str
    action_text: str
    object_text: str | None
    role_text: str | None
    person_text: str | None
    counterparty_text: str | None
    amount_text: str | None
    date_text: str | None
    effective_text: str | None
    modality: str
    modality_text: str | None
    evidence_sentence: str
    extraction_rule: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject_text,
            "subject_binding": self.subject_binding,
            "issuer_name_explicit_in_passage": self.issuer_name_explicit,
            "actor": self.actor_text,
            "predicate": self.predicate,
            "action_text": self.action_text,
            "object": self.object_text,
            "role": self.role_text,
            "person": self.person_text,
            "counterparty": self.counterparty_text,
            "amount": self.amount_text,
            "date": self.date_text,
            "effective": self.effective_text,
            "modality": self.modality,
            "modality_text": self.modality_text,
            "evidence_sentence": self.evidence_sentence,
            "extraction_rule": self.extraction_rule,
        }


@dataclass(frozen=True)
class EvidenceFactExtraction:
    contract_version: str
    event_type: str
    passage_sha256: str
    canonical_passage_sha256: str
    facts: tuple[EvidenceFactSlots, ...]
    missing_slots: tuple[str, ...]
    limitation: str | None

    @property
    def supported_facts(self) -> tuple[EvidenceFactSlots, ...]:
        return tuple(
            fact
            for fact in self.facts
            if fact.subject_binding
            in {
                "EXPLICIT_ISSUER",
                "EXPLICIT_ISSUER_CONTEXT",
            }
            and fact_supports_event_type(self.event_type, fact)
        )

    @property
    def supports_specific_fact(self) -> bool:
        return bool(self.supported_facts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "event_type": self.event_type,
            "passage_sha256": self.passage_sha256,
            "canonical_passage_sha256": self.canonical_passage_sha256,
            "facts": [
                {
                    **fact.as_dict(),
                    "event_type_compatible": fact_supports_event_type(
                        self.event_type, fact
                    ),
                }
                for fact in self.facts
            ],
            "compatible_fact_count": len(self.supported_facts),
            "missing_slots": list(self.missing_slots),
            "limitation": self.limitation,
        }


def _clean(value: Any) -> str:
    return " ".join(str(value or "").split())


_DATE_RE = re.compile(
    r"\b(?:"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}/\d{1,2}/\d{4}"
    r")\b",
    re.I,
)
_EFFECTIVE_RE = re.compile(
    r"\beffective(?:\s+(?:as\s+of|on))?\s+(?:immediately|"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})",
    re.I,
)
_AMOUNT_RE = re.compile(
    r"(?:US\$|\$)\s*\d[\d,.]*(?:\s*(?:million|billion|thousand))?"
    r"|\b\d+(?:\.\d+)?\s*(?:million|billion)\s+(?:dollars|shares|units)\b",
    re.I,
)
_ROLE_RE = re.compile(
    r"\b(?:interim\s+|acting\s+)?(?:"
    r"chief\s+(?:executive|financial|operating|accounting|legal)\s+officer"
    r"|principal\s+(?:executive|financial|accounting)\s+officer"
    r"|president|director|chair(?:man|woman|person)?|CEO|CFO)\b",
    re.I,
)
_SECURITY_RE = re.compile(
    r"\b(?:common\s+stock|common\s+shares|ordinary\s+shares|preferred\s+stock|"
    r"pre-funded\s+warrants?|warrants?|convertible\s+(?:senior\s+)?notes?|"
    r"senior\s+(?:unsecured\s+)?notes?|debt\s+securities|credit\s+facility|"
    r"registered\s+direct\s+offering|public\s+offering|at-the-market\s+offering|"
    r"private\s+placement|refinancing)\b",
    re.I,
)
DETERMINISTIC_FACT_SLOT_EVENT_TYPES = frozenset(
    {
        "management_change",
        "chief_financial_officer_appointment",
        "delisting",
        "minimum_bid_price_deficiency_notice",
        "offering_or_dilution",
        "debt_refinancing",
        "convertible_debt_financing",
        "senior_unsecured_debt_financing",
        "credit_facility_amendment",
        "merger_or_acquisition",
        "material_corporate_transaction",
    }
)

EVENT_TYPE_ALLOWED_PREDICATES: dict[str, frozenset[str]] = {
    "management_change": frozenset({"OFFICER_DEPARTURE", "OFFICER_APPOINTMENT"}),
    "chief_financial_officer_appointment": frozenset({"OFFICER_APPOINTMENT"}),
    "delisting": frozenset({"DELISTING_NOTICE", "DELISTING_ACTION"}),
    "minimum_bid_price_deficiency_notice": frozenset(
        {"MINIMUM_BID_DEFICIENCY_NOTICE"}
    ),
    "offering_or_dilution": frozenset(
        {
            "SECURITIES_ISSUANCE",
            "SECURITIES_SALE",
            "OFFERING_PRICED",
            "FINANCING_COMPLETED",
            "FINANCING_CLOSED",
            "OFFERING_COMMENCED",
            "FINANCING_ANNOUNCED",
            "FINANCING_AUTHORIZATION",
        }
    ),
    "debt_refinancing": frozenset({"DEBT_REFINANCING"}),
    "convertible_debt_financing": frozenset(
        {
            "SECURITIES_ISSUANCE",
            "SECURITIES_SALE",
            "OFFERING_PRICED",
            "FINANCING_COMPLETED",
            "FINANCING_CLOSED",
            "OFFERING_COMMENCED",
            "FINANCING_ANNOUNCED",
            "FINANCING_AUTHORIZATION",
        }
    ),
    "senior_unsecured_debt_financing": frozenset(
        {
            "SECURITIES_ISSUANCE",
            "SECURITIES_SALE",
            "OFFERING_PRICED",
            "FINANCING_COMPLETED",
            "FINANCING_CLOSED",
            "OFFERING_COMMENCED",
            "FINANCING_ANNOUNCED",
            "FINANCING_AUTHORIZATION",
        }
    ),
    "credit_facility_amendment": frozenset({"FINANCING_AMENDMENT"}),
    "merger_or_acquisition": frozenset(
        {
            "TRANSACTION_AGREEMENT_ENTERED",
            "TRANSACTION_AGREEMENT_SIGNED",
            "TRANSACTION_AGREEMENT_EXECUTED",
            "TRANSACTION_APPROVED",
            "TRANSACTION_COMPLETED",
            "TRANSACTION_CLOSED",
            "TRANSACTION_CONSUMMATED",
            "TRANSACTION_TERMINATED",
            "TRANSACTION_ABANDONED",
        }
    ),
    "material_corporate_transaction": frozenset(
        {
            "TRANSACTION_AGREEMENT_ENTERED",
            "TRANSACTION_AGREEMENT_SIGNED",
            "TRANSACTION_AGREEMENT_EXECUTED",
            "TRANSACTION_APPROVED",
            "TRANSACTION_COMPLETED",
            "TRANSACTION_CLOSED",
            "TRANSACTION_CONSUMMATED",
            "TRANSACTION_TERMINATED",
            "TRANSACTION_ABANDONED",
        }
    ),
}


def fact_supports_event_type(event_type: str, fact: EvidenceFactSlots) -> bool:
    """Return whether this exact fact proves the requested event type.

    Shared extractors cover related families, but a concrete neighbouring fact
    is not evidence for a narrower type.  For example, a stock offering cannot
    prove debt refinancing and a resignation cannot prove a CFO appointment.
    """

    normalized = _clean(event_type).casefold()
    if fact.predicate not in EVENT_TYPE_ALLOWED_PREDICATES.get(normalized, frozenset()):
        return False
    obj = _clean(fact.object_text).casefold()
    role = _clean(fact.role_text).casefold()
    sentence = _clean(fact.evidence_sentence).casefold()
    if normalized == "chief_financial_officer_appointment":
        return "chief financial officer" in role or role == "cfo"
    if normalized == "offering_or_dilution":
        return any(
            token in obj
            for token in (
                "common stock",
                "common shares",
                "ordinary shares",
                "preferred stock",
                "warrant",
                "offering",
                "private placement",
            )
        )
    if normalized == "convertible_debt_financing":
        return "convertible" in obj
    if normalized == "senior_unsecured_debt_financing":
        return "senior" in obj and "unsecured" in obj
    if normalized == "credit_facility_amendment":
        return "credit facility" in obj
    return True


def requires_specific_fact_extraction(event_type: str) -> bool:
    """Every classified SEC event must prove a passage-bound specific fact.

    This intentionally sacrifices classification coverage for public truth:
    an event type without a deterministic extractor remains a discovery lead
    instead of becoming a reader-visible category-shaped non-fact.
    """

    return bool(_clean(event_type))


def supports_deterministic_fact_extraction(event_type: str) -> bool:
    return _clean(event_type).casefold() in DETERMINISTIC_FACT_SLOT_EVENT_TYPES


def _sentence_windows(passage: str) -> tuple[str, ...]:
    compact = _clean(passage)
    if not compact:
        return ()
    # SEC visible text is sometimes missing punctuation between table cells.
    # Keep bounded clauses but never synthesize words absent from the passage.
    protected = re.sub(
        r"\b(Inc|Corp|Ltd|Co|Mr|Ms|Dr)\.",
        lambda match: match.group(1) + "<FACT_SLOT_DOT>",
        compact,
        flags=re.I,
    )
    sentences = tuple(
        part.replace("<FACT_SLOT_DOT>", ".").strip(" …")
        for part in re.split(r"(?<=[.!?;])\s+|\s{2,}", protected)
        if part.strip(" …")
    )
    return sentences or (compact,)


def _matched_text(match: re.Match[str] | None) -> str | None:
    return match.group(0) if match is not None else None


def _subject_from_sentence(
    sentence: str,
    expected_subject: str,
    *,
    action_start: int,
) -> tuple[str | None, str, bool]:
    expected = _clean(expected_subject)
    prefix = sentence[:action_start]
    allowed_tail = re.compile(
        r"^[\s,.]*(?:(?:has|had|will|shall|may|might|could|would|"
        r"expects?\s+to|intends?\s+to|plans?\s+to|successfully|today)\s+)*$",
        re.I,
    )
    if expected:
        matches = list(
            re.finditer(rf"(?<!\w){re.escape(expected)}(?!\w)", prefix, flags=re.I)
        )
        for match in reversed(matches):
            prior = prefix[max(0, match.start() - 32) : match.start()]
            # ``Shareholders of Issuer approved`` and ``Subsidiary of Issuer
            # issued`` mention the filing issuer but do not make it the actor.
            governed = re.search(
                r"\b(?:of|by|for|with|from|to|than|about|regarding)\s+$",
                prior,
                re.I,
            )
            if not governed and allowed_tail.fullmatch(prefix[match.end() :]):
                return match.group(0), "EXPLICIT_ISSUER", True
    pronouns = list(
        re.finditer(r"\b(?:the company|the registrant|the issuer|our company|we)\b", prefix, re.I)
    )
    if pronouns and allowed_tail.fullmatch(prefix[pronouns[-1].end() :]):
        pronoun = pronouns[-1]
        attribution = re.search(
            r"(?P<actor>(?:the\s+(?:exchange|regulator|court)|"
            r"[A-Z][A-Za-z0-9&.'’\-]*(?:\s+[A-Z][A-Za-z0-9&.'’\-]*){0,5}))"
            r"\s+(?:stated|reported|announced|said|confirmed|disclosed)\b"
            r"[^.;:]*?\bthat\s+$",
            prefix[: pronoun.start()],
            re.I,
        )
        if attribution is not None and _clean(attribution.group("actor")).casefold() != (
            expected.casefold()
        ):
            return pronoun.group(0), "AMBIGUOUS_COMPANY_PRONOUN", False
        # A generic company pronoun is document-issuer context only when no
        # other named company has already taken control of this sentence.
        # ``Target Corp stated that the company issued ...`` cannot be bound
        # to the filing issuer merely because the filing metadata names it.
        competing_named_entity = any(
            _clean(item.group(0)).casefold() != expected.casefold()
            for item in _NAMED_COMPANY_RE.finditer(prefix[: pronoun.start()])
        )
        if competing_named_entity:
            return pronoun.group(0), "AMBIGUOUS_COMPANY_PRONOUN", False
        return pronoun.group(0), "DOCUMENT_ISSUER_PRONOUN", False
    named_entities = list(
        re.finditer(
            r"\b[A-Z][A-Za-z0-9&.'’\-]*(?:\s+[A-Z][A-Za-z0-9&.'’\-]*){0,5}\s+"
            r"(?:Inc\.?|Corp\.?|Corporation|Company|Ltd\.?|LLC|L\.P\.)\b",
            prefix,
        )
    )
    if named_entities and allowed_tail.fullmatch(prefix[named_entities[-1].end() :]):
        return named_entities[-1].group(0), "OTHER_NAMED_ENTITY", False
    return None, "MISSING", False


_NAMED_COMPANY_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9&.'’\-]*(?:\s+[A-Z][A-Za-z0-9&.'’\-]*){0,5}\s+"
    r"(?:Inc\.?|Corp\.?|Corporation|Company|Ltd\.?|LLC|L\.P\.)\b"
)
_POSITIONAL_ORG_NAME = (
    r"[A-Z][A-Za-z0-9&.'’\-]*(?:\s+[A-Z][A-Za-z0-9&.'’\-]*){0,5}"
)


def _expected_subject_matches(sentence: str, expected_subject: str) -> list[re.Match[str]]:
    expected = _clean(expected_subject)
    if not expected:
        return []
    return list(
        re.finditer(rf"(?<!\w){re.escape(expected)}(?!\w)", sentence, flags=re.I)
    )


def _management_role_subject(
    sentence: str,
    expected_subject: str,
    *,
    role: re.Match[str],
    action: re.Match[str],
) -> tuple[str | None, str, bool]:
    """Bind a departure role to the issuer without same-sentence inference."""

    if role.end() > action.start() or not re.fullmatch(
        r"[\s,]*(?:has|had)?\s*", sentence[role.end() : action.start()], re.I
    ):
        return None, "MISSING", False
    expected_matches = _expected_subject_matches(sentence, expected_subject)
    for expected in reversed([item for item in expected_matches if item.start() < role.start()]):
        bridge = sentence[expected.end() : role.start()]
        competing = [
            item
            for item in _NAMED_COMPANY_RE.finditer(bridge)
            if _clean(item.group(0)).casefold() != _clean(expected_subject).casefold()
        ]
        if competing:
            continue
        if re.search(
            r"(?:['’]s\s+|\b(?:reported|announced|disclosed|stated|confirmed)\b"
            r"[^.;:]{0,100}\bthat\s+its\s+|\bits\s+)$",
            bridge,
            re.I,
        ):
            return expected.group(0), "EXPLICIT_ISSUER_CONTEXT", True
    # ``director of Example Corp resigned`` is also local and unambiguous.
    between = sentence[role.end() : action.start()]
    for expected in expected_matches:
        if expected.start() < role.end() or expected.end() > action.start():
            continue
        if re.fullmatch(
            rf"\s+of\s+{re.escape(expected.group(0))}[\s,]*(?:has|had)?\s*",
            between,
            re.I,
        ):
            return expected.group(0), "EXPLICIT_ISSUER_CONTEXT", True
    pronoun = re.search(
        r"\b(?P<subject>the\s+company['’]s|the\s+registrant['’]s|our\s+company['’]s)\s+$",
        sentence[max(0, role.start() - 40) : role.start()],
        re.I,
    )
    if pronoun:
        return pronoun.group("subject"), "DOCUMENT_ISSUER_PRONOUN", False
    return None, "MISSING", False


def _management_appointment_subject(
    sentence: str,
    expected_subject: str,
    *,
    action: re.Match[str],
) -> tuple[str | None, str, bool]:
    """Bind an issuer's explicitly named governing body to an appointment.

    SEC appointment prose normally makes the board, rather than the issuer
    itself, the grammatical actor (``the Board of Directors of Issuer
    appointed ...``).  Treat that as issuer context only when the same
    sentence contains the exact canonical issuer name and the governing-body
    phrase is locally attached to the action.  A filing-level issuer identity
    or a bare ``the Board`` is deliberately insufficient.
    """

    direct = _subject_from_sentence(
        sentence,
        expected_subject,
        action_start=action.start(),
    )
    expected = _clean(expected_subject)
    if direct[1] != "MISSING" and not (
        direct[1] == "OTHER_NAMED_ENTITY"
        and _clean(direct[0]).casefold() == expected.casefold()
    ):
        return direct
    if not expected:
        return direct
    prefix = sentence[: action.start()]
    expected_matches = _expected_subject_matches(sentence, expected)
    for match in reversed([item for item in expected_matches if item.end() <= action.start()]):
        local_prefix = prefix[max(0, match.start() - 100) :]
        after_subject = prefix[match.end() :]

        # ``the Board of Directors of Example Corp (...) appointed`` and
        # ``Example Corp's Board appointed`` are explicit local bindings.
        board_of_issuer = re.search(
            r"\b(?:board(?:\s+of\s+directors)?|directors?)\b[^.;:]{0,55}\bof\s+$",
            local_prefix[: match.start() - max(0, match.start() - 100)],
            re.I,
        )
        issuer_board = re.fullmatch(
            r"(?:['’]s)?\s+(?:board(?:\s+of\s+directors)?|directors?)"
            r"(?:\s*\([^)]{0,60}\))?[\s,]*(?:has|had)?\s*",
            after_subject,
            re.I,
        )
        if board_of_issuer or issuer_board:
            return match.group(0), "EXPLICIT_ISSUER_CONTEXT", True

        # A same-sentence alias is also bounded: the exact issuer name must
        # define ``the Company`` before ``the board ... of the Company``.
        alias_definition = re.match(
            r"[^.;:]{0,180}(?:\(“[^)”]{0,80}(?:the\s+)?“Company”[^)]*\)|"
            r"\([^)]{0,80}(?:the\s+)?[\"“]Company[\"”][^)]*\)|"
            r"\(\s*the\s+Company\s*\))",
            after_subject,
            re.I,
        )
        alias_tail = prefix[(match.end() + alias_definition.end()) :] if alias_definition else ""
        if alias_definition and re.fullmatch(
            r"[^.;:]{0,180}\b(?:board(?:\s+of\s+directors)?|directors?)\b"
            r"[^.;:]{0,80}\bof\s+the\s+Company\b"
            r"(?:\s*\([^)]{0,60}\))?[\s,]*(?:has|had)?\s*",
            alias_tail,
            re.I,
        ):
            return match.group(0), "EXPLICIT_ISSUER_CONTEXT", True
    return direct


def _listing_subject(
    sentence: str,
    expected_subject: str,
    *,
    action: re.Match[str],
    trigger: re.Match[str],
) -> tuple[str | None, str, bool]:
    # The company announcing a notice is not necessarily the security or
    # issuer to which that notice applies.  A named recipient after the
    # trigger controls the event subject and must not be reassigned to the
    # filing issuer merely because the issuer appears before ``announced``.
    trigger_tail = sentence[trigger.end() :]
    recipient = re.search(
        r"\b(?:(?:notice\s+)?(?:was\s+)?(?:issued|provided|sent)\s+)?"
        r"(?:to|for|about|regarding)\s+"
        rf"(?P<recipient>{_POSITIONAL_ORG_NAME})\b",
        trigger_tail,
    )
    if recipient is not None and _clean(recipient.group("recipient")).casefold() != (
        _clean(expected_subject).casefold()
    ):
        return recipient.group("recipient"), "OTHER_NAMED_ENTITY", False
    owner_before_trigger = re.search(
        rf"(?:\bthe\s+)?(?P<owner>{_POSITIONAL_ORG_NAME})(?:['’]s)?\s+$",
        sentence[: trigger.start()],
    )
    if owner_before_trigger is not None:
        nearest_name = _clean(owner_before_trigger.group("owner"))
        if nearest_name.casefold() != _clean(expected_subject).casefold():
            return owner_before_trigger.group("owner"), "OTHER_NAMED_ENTITY", False
    direct = _subject_from_sentence(sentence, expected_subject, action_start=action.start())
    if direct[1] in {"EXPLICIT_ISSUER", "DOCUMENT_ISSUER_PRONOUN"}:
        return direct

    expected_matches = _expected_subject_matches(sentence, expected_subject)
    action_key = action.group(0).casefold()
    if action_key in {"notified", "sent", "issued", "provided"}:
        relation_tail = sentence[action.end() : trigger.end()]
        for expected in expected_matches:
            if action.end() <= expected.start() <= trigger.start() and re.fullmatch(
                rf"\s+(?:the\s+)?{re.escape(expected.group(0))}\s*(?:,|that|of|with|about)?[^.;:]*",
                relation_tail,
                re.I,
            ):
                return expected.group(0), "EXPLICIT_ISSUER_CONTEXT", True

    if "delist" in action_key or action_key in {"would be", "will be"}:
        named_before = [
            item for item in _NAMED_COMPANY_RE.finditer(sentence[: action.start()])
        ]
        if named_before:
            nearest = named_before[-1]
            if _clean(nearest.group(0)).casefold() == _clean(expected_subject).casefold():
                bridge = sentence[nearest.end() : action.start()]
                if re.fullmatch(
                    r"[\s,]*(?:that\s+)?(?:its|whose)?\s*(?:the\s+)?"
                    r"(?:common\s+stock|common\s+shares|ordinary\s+shares|securities)?\s*",
                    bridge,
                    re.I,
                ):
                    return nearest.group(0), "EXPLICIT_ISSUER_CONTEXT", True
    return direct


def _modality(sentence: str, action_start: int, action_text: str) -> tuple[str, str | None]:
    prefix = sentence[max(0, action_start - 60) : action_start]
    patterns = (
        ("POSSIBLE", r"\b(?:may|might|could)\s+$"),
        ("EXPECTED", r"\b(?:expects?\s+to|expected\s+to)\s+$"),
        ("INTENDED", r"\b(?:intends?\s+to|plans?\s+to|proposes?\s+to)\s+$"),
        ("ANNOUNCED_FUTURE", r"\b(?:will|shall)\s+$"),
    )
    for label, pattern in patterns:
        match = re.search(pattern, prefix, re.I)
        if match:
            return label, match.group(0).strip()
    purpose = re.search(r"\bto\s+$", prefix, re.I)
    if purpose and action_text.casefold() in {
        "appoint",
        "name",
        "elect",
        "designate",
        "issue",
        "repay",
        "refinance",
        "delist",
    }:
        return "INTENDED", purpose.group(0).strip()
    if action_text.casefold() in {"would be", "intends to", "intend to", "plans to", "plan to"}:
        label = "ANNOUNCED_FUTURE" if action_text.casefold() == "would be" else "INTENDED"
        return label, action_text
    if action_text.casefold() in {"authorized", "approved"}:
        return "AUTHORIZED", None
    return "ASSERTED", None


def _temporal_slots(sentence: str) -> tuple[str | None, str | None]:
    effective = _EFFECTIVE_RE.search(sentence)
    date = _DATE_RE.search(sentence)
    return _matched_text(date), _matched_text(effective)


def _action_is_affirmed(
    sentence: str,
    action: re.Match[str],
    *,
    support_start: int | None = None,
    support_end: int | None = None,
) -> bool:
    """Reject locally negated, denied, refused and hypothetical action clauses.

    Classification keywords only route a filing.  This guard therefore checks
    the action clause independently and on both sides of the matched verb.
    False negatives are preferable to turning a denial into a public event.
    """

    clause_start = max(
        sentence.rfind(".", 0, action.start()),
        sentence.rfind(";", 0, action.start()),
        sentence.rfind(":", 0, action.start()),
    ) + 1
    clause_end_candidates = [
        position
        for token in (".", ";", ":")
        if (position := sentence.find(token, action.end())) >= 0
    ]
    clause_end = min(clause_end_candidates) if clause_end_candidates else len(sentence)
    full_left = sentence[clause_start : action.start()]
    right = sentence[action.end() : clause_end]
    relation_start = max(clause_start, support_start if support_start is not None else action.start())
    relation_end = min(clause_end, support_end if support_end is not None else action.end())
    relation = sentence[relation_start:relation_end]

    quoted_action = False
    for quote_pattern in (r'"[^"\r\n]*"', r"“[^”\r\n]*”", r"‘[^’\r\n]*’"):
        for quoted in re.finditer(quote_pattern, sentence):
            if quoted.start() < action.start() and quoted.end() > action.end():
                quoted_action = True
                break
        if quoted_action:
            break
    if quoted_action and re.search(
        r"\b(?:keyword|phrase|headline|title|caption|quotation|quoted|"
        r"reproduced|appears?|contained?|excerpt)\b",
        sentence,
        re.I,
    ):
        return False

    full_left_blockers = (
        r"\b(?:never|neither|no|not|without)\b",
        r"\b(?:declin(?:e|ed|es)|refus(?:e|ed|es)|fail(?:ed|s)?|"
        r"den(?:y|ied|ies)|disput(?:e|ed|es)|reject(?:ed|s)?|"
        r"contradict(?:ed|s)?|rebut(?:ted|s)?)\b",
        r"\b(?:false|untrue|incorrect|inaccurate|hypothetical|rumou?rs?|"
        r"alleg(?:e|ed|es|ation|ations)|claim(?:ed|s)?|suggest(?:ed|s)?|"
        r"possible|possibility|purported(?:ly)?|reportedly|unconfirmed|"
        r"unverified)\b",
    )
    if re.search(
        r"\b(?:if|unless|whether|assuming(?:\s+that)?|assume(?:d|s)?|assumption|"
        r"suppose(?:d|s)?|supposing|scenario|illustrative|hypothetical|"
        r"pro\s+forma|provided(?:\s+that)?|in\s+the\s+event\s+that)\b",
        full_left,
        re.I,
    ):
        return False
    if re.search(r"^\s*had\b", full_left, re.I):
        return False
    if any(re.search(pattern, full_left, re.I) for pattern in full_left_blockers):
        return False
    if re.search(r"^\s*(?:no|not|neither|without)\b", right, re.I):
        return False
    if relation and re.search(r"\b(?:no|not|never|neither|without)\b", relation, re.I):
        return False
    if re.search(
        r"\b(?:is|are|was|were|proved|proven)\s+"
        r"(?:false|untrue|incorrect|inaccurate|denied|disputed)\b",
        right,
        re.I,
    ):
        return False
    if re.search(
        r"\b(?:did|does|has|have|had)\s+not\s+"
        r"(?:occur|happen|take\s+place)\b",
        right,
        re.I,
    ):
        return False
    if re.search(
        r"\b(?:never|not)\b[^.;:]*\b(?:occur(?:red)?|happen(?:ed)?|"
        r"complete(?:d)?|close(?:d)?|consummate(?:d)?|take|took)\b"
        r"(?:\s+place)?\b",
        right,
        re.I,
    ):
        return False
    if re.search(
        r"\bno\b[^.;:]*\b(?:occurred|happened|completed|closed|consummated|"
        r"took\s+place)\b",
        right,
        re.I,
    ):
        return False
    if re.search(
        r"\b(?:withdrawn|withdrew|retracted|cancelled|canceled|abandoned|"
        r"corrected)\b",
        right,
        re.I,
    ):
        return False
    if re.search(
        r"\b(?:alleg(?:e|ed|es|ation|ations)|claim(?:ed|s)?|suggest(?:ed|s)?|"
        r"purported(?:ly)?|reportedly|unconfirmed|unverified)\b",
        right,
        re.I,
    ):
        return False
    if re.search(
        r"\b(?:den(?:y|ied|ies)|reject(?:ed|s)?|disput(?:e|ed|es)|"
        r"contradict(?:ed|s)?|rebut(?:ted|s)?)\b[^.;:]{0,100}"
        r"\b(?:as\s+)?(?:false|untrue|incorrect|inaccurate)\b",
        right,
        re.I,
    ):
        return False
    return True


_DENIAL_MARKER_RE = re.compile(
    r"\b(?:den(?:y|ied|ies)|reject(?:ed|s)?|disput(?:e|ed|es)|"
    r"contradict(?:ed|s)?|rebut(?:ted|s)?|clarif(?:y|ied|ies)|"
    r"correct(?:ed|s)?|withdraw|withdrew|withdrawn|retract(?:ed|s)?|"
    r"cancel(?:led|ed|s)?|abandon(?:ed|s)?)\b",
    re.I,
)
_PREDICATE_DENIAL_TERMS = {
    "OFFICER_DEPARTURE": ("resignation", "departure", "resigned", "departed"),
    "OFFICER_APPOINTMENT": ("appointment", "appointed", "named", "elected"),
    "DELISTING_NOTICE": ("delisting", "notice of delisting"),
    "DELISTING_ACTION": ("delisting", "delisted"),
    "LISTING_NONCOMPLIANCE_NOTICE": ("noncompliance", "listing notice"),
    "MINIMUM_BID_DEFICIENCY_NOTICE": ("bid price deficiency", "deficiency notice"),
    "SECURITIES_ISSUANCE": ("issuance", "issued", "offering"),
    "SECURITIES_SALE": ("sale", "sold", "offering"),
    "OFFERING_PRICED": ("offering", "priced"),
    "TRANSACTION_COMPLETED": ("transaction", "merger", "acquisition", "completed"),
    "TRANSACTION_CLOSED": ("transaction", "merger", "acquisition", "closed"),
    "TRANSACTION_CONSUMMATED": ("transaction", "merger", "acquisition", "consummated"),
}


def _later_sentence_denies_fact(sentence: str, fact: EvidenceFactSlots) -> bool:
    """Return true only for a nearby denial that refers back to this fact."""

    denial = _DENIAL_MARKER_RE.search(sentence)
    lowered = sentence.casefold()
    references = tuple(
        value.casefold()
        for value in (
            fact.action_text,
            fact.object_text,
            *_PREDICATE_DENIAL_TERMS.get(fact.predicate, ()),
        )
        if value and len(value.strip()) >= 4
    )
    reference_positions = [lowered.find(value) for value in references if lowered.find(value) >= 0]
    if not reference_positions:
        return False
    reference_start = min(reference_positions)
    direct_negation = bool(
        re.search(
            r"\b(?:did|does|do|has|have|had)\s+not\s+"
            r"(?:occur|happen|take\s+place|complete|close)\b",
            sentence,
            re.I,
        )
        or re.search(
            r"\b(?:is|are|was|were|proved|proven)\s+"
            r"(?:false|untrue|incorrect|inaccurate)\b",
            sentence,
            re.I,
        )
        or re.search(
            r"\bno\b[^.;:]{0,120}\b(?:occurred|happened|completed|closed|"
            r"was\s+completed|was\s+closed|took\s+place)\b",
            sentence,
            re.I,
        )
        or re.search(
            r"\b(?:never|not)\b[^.;:]{0,160}\b(?:occurred|happened|completed|"
            r"closed|consummated|took\s+place)\b",
            sentence,
            re.I,
        )
        or re.search(
            r"\b(?:withdrawn|withdrew|retracted|cancelled|canceled|abandoned)\b",
            sentence,
            re.I,
        )
    )
    if denial is None:
        return direct_negation
    denial_reference_positions = [
        lowered.find(value, denial.end())
        for value in references
        if lowered.find(value, denial.end()) >= 0
    ]
    if not denial_reference_positions:
        return direct_negation
    reference_start = min(denial_reference_positions)
    bridge = sentence[denial.end() : reference_start]
    expected = _clean(fact.subject_text) if fact.issuer_name_explicit else ""
    bridge_entities = list(_NAMED_COMPANY_RE.finditer(bridge))
    if any(
        not expected
        or _clean(item.group(0)).casefold() != expected.casefold()
        for item in bridge_entities
    ):
        return False
    before_denial = sentence[max(0, denial.start() - 140) : denial.start()]
    expected_controls_denial = bool(
        expected
        and re.search(rf"(?<!\w){re.escape(expected)}(?!\w)", before_denial, re.I)
    )
    expected_controls_reference = bool(
        expected
        and any(
            _clean(item.group(0)).casefold() == expected.casefold()
            for item in bridge_entities
        )
    )
    pronoun_only_context = not expected and not list(
        _NAMED_COMPANY_RE.finditer(sentence[:reference_start])
    )
    return expected_controls_denial or expected_controls_reference or pronoun_only_context


def _drop_postposed_denied_facts(
    facts: list[EvidenceFactSlots], sentences: tuple[str, ...]
) -> list[EvidenceFactSlots]:
    """Fail closed when any later sentence in the passage denies a fact.

    A filing passage is the smallest evidence receipt admitted by this module.
    A denial or correction later in that same receipt therefore invalidates the
    earlier machine fact even when an intervening sentence separates them.
    """

    retained: list[EvidenceFactSlots] = []
    for fact in facts:
        try:
            sentence_index = sentences.index(fact.evidence_sentence)
        except ValueError:
            retained.append(fact)
            continue
        later_sentences = sentences[sentence_index + 1 :]
        if any(_later_sentence_denies_fact(sentence, fact) for sentence in later_sentences):
            continue
        retained.append(fact)
    return retained


def _drop_cross_sentence_ambiguous_pronouns(
    facts: list[EvidenceFactSlots],
    sentences: tuple[str, ...],
    expected_subject: str,
) -> list[EvidenceFactSlots]:
    """Do not let a prior sentence's other company become the filing issuer."""

    expected = _clean(expected_subject).casefold()
    retained: list[EvidenceFactSlots] = []
    for fact in facts:
        if fact.subject_binding != "DOCUMENT_ISSUER_PRONOUN":
            retained.append(fact)
            continue
        try:
            sentence_index = sentences.index(fact.evidence_sentence)
        except ValueError:
            retained.append(fact)
            continue
        previous_sentence = sentences[sentence_index - 1] if sentence_index > 0 else ""
        competing_antecedent = any(
            not expected or _clean(item.group(0)).casefold() != expected
            for item in _NAMED_COMPANY_RE.finditer(previous_sentence)
        )
        leading_actor = re.match(
            r"\s*(?P<actor>[A-Z][A-Za-z0-9&.'’\-]*"
            r"(?:\s+[A-Z][A-Za-z0-9&.'’\-]*){0,5})\s+"
            r"(?:is|was|serves?|acts?|stated|reported|announced|said|confirmed|disclosed)\b",
            previous_sentence,
        )
        if leading_actor is not None and _clean(
            leading_actor.group("actor")
        ).casefold() != expected:
            competing_antecedent = True
        if competing_antecedent:
            continue
        retained.append(fact)
    return retained


def _append_fact(
    facts: list[EvidenceFactSlots],
    *,
    sentence: str,
    expected_subject: str,
    predicate: str,
    action: re.Match[str],
    object_text: str | None = None,
    role_text: str | None = None,
    person_text: str | None = None,
    counterparty_text: str | None = None,
    actor_text: str | None = None,
    amount_text: str | None = None,
    subject_override: tuple[str | None, str, bool] | None = None,
    extraction_rule: str,
) -> None:
    subject_text, subject_binding, issuer_name_explicit = subject_override or _subject_from_sentence(
        sentence, expected_subject, action_start=action.start()
    )
    date_text, effective_text = _temporal_slots(sentence)
    modality, modality_text = _modality(sentence, action.start(), action.group(0))
    candidate = EvidenceFactSlots(
        subject_text=subject_text,
        subject_binding=subject_binding,
        issuer_name_explicit=issuer_name_explicit,
        actor_text=actor_text,
        predicate=predicate,
        action_text=action.group(0),
        object_text=object_text,
        role_text=role_text,
        person_text=person_text,
        counterparty_text=counterparty_text,
        amount_text=amount_text or _matched_text(_AMOUNT_RE.search(sentence)),
        date_text=date_text,
        effective_text=effective_text,
        modality=modality,
        modality_text=modality_text,
        evidence_sentence=sentence,
        extraction_rule=extraction_rule,
    )
    identity = (
        candidate.predicate,
        candidate.action_text.casefold(),
        (candidate.object_text or "").casefold(),
        candidate.evidence_sentence.casefold(),
    )
    if not any(
        (
            current.predicate,
            current.action_text.casefold(),
            (current.object_text or "").casefold(),
            current.evidence_sentence.casefold(),
        )
        == identity
        for current in facts
    ):
        facts.append(candidate)


def _extract_management_facts(
    sentences: tuple[str, ...], expected_subject: str
) -> list[EvidenceFactSlots]:
    facts: list[EvidenceFactSlots] = []
    for sentence in sentences:
        roles = list(_ROLE_RE.finditer(sentence))
        if not roles:
            continue
        for action in re.finditer(
            r"\b(?:resigned|retires?|retired|departed|ceased\s+to\s+serve|was\s+terminated)\b",
            sentence,
            re.I,
        ):
            roles_before = [role for role in roles if role.end() <= action.start()]
            if not roles_before:
                continue
            role = roles_before[-1]
            if not _action_is_affirmed(
                sentence,
                action,
                support_start=role.start(),
                support_end=action.end(),
            ):
                continue
            subject = _management_role_subject(
                sentence,
                expected_subject,
                role=role,
                action=action,
            )
            _append_fact(
                facts,
                sentence=sentence,
                expected_subject=expected_subject,
                predicate="OFFICER_DEPARTURE",
                action=action,
                object_text=role.group(0),
                role_text=role.group(0),
                subject_override=subject,
                extraction_rule="management-departure-v1",
            )
        for action in re.finditer(
            r"\b(?:appoint(?:ed|s)?|name(?:d|s)?|elect(?:ed|s)?|designate(?:d|s)?)\b",
            sentence,
            re.I,
        ):
            tail = sentence[action.end() :]
            appointment = re.search(
                r"\s+(?:(?P<person>[A-Z][A-Za-z.'’\-]+(?:\s+[A-Z][A-Za-z.'’\-]+){1,4})"
                r"\s+(?:to\s+serve\s+)?as\s+(?:the\s+)?)?"
                r"(?P<role>(?i:(?:interim\s+|acting\s+)?(?:chief\s+(?:executive|financial|operating|accounting|legal)\s+officer|principal\s+(?:executive|financial|accounting)\s+officer|president|director|chair(?:man|woman|person)?|CEO|CFO)))\b",
                tail,
            )
            if appointment is None:
                continue
            if not _action_is_affirmed(
                sentence,
                action,
                support_start=action.start(),
                support_end=action.end() + appointment.end(),
            ):
                continue
            chosen_role = appointment.group("role")
            person = appointment.group("person")
            role_end = action.end() + appointment.end("role")
            role_owner = re.match(
                rf"\s+(?:of|for|at)\s+(?P<company>{_POSITIONAL_ORG_NAME})\b",
                sentence[role_end:],
            )
            subject_override = None
            if role_owner is not None and _clean(
                role_owner.group("company")
            ).casefold() != _clean(expected_subject).casefold():
                subject_override = (
                    role_owner.group("company"),
                    "OTHER_NAMED_ENTITY",
                    False,
                )
            if subject_override is None:
                subject_override = _management_appointment_subject(
                    sentence,
                    expected_subject,
                    action=action,
                )
            _append_fact(
                facts,
                sentence=sentence,
                expected_subject=expected_subject,
                predicate="OFFICER_APPOINTMENT",
                action=action,
                object_text=person or chosen_role,
                role_text=chosen_role,
                person_text=person,
                subject_override=subject_override,
                extraction_rule="management-appointment-v1",
            )
    return facts


def _extract_listing_facts(
    sentences: tuple[str, ...], expected_subject: str
) -> list[EvidenceFactSlots]:
    facts: list[EvidenceFactSlots] = []
    triggers = (
        ("DELISTING_NOTICE", r"\bnotice\s+of\s+delisting\b"),
        (
            "DELISTING_ACTION",
            r"\b(?:(?:would|will)\s+be\s+delisted\s+from|"
            r"(?:is|was|has\s+been)\s+delisted\s+from|"
            r"delisted\s+from|delist\s+from|voluntarily\s+delist)\b",
        ),
        ("LISTING_NONCOMPLIANCE_NOTICE", r"\blisting\s+noncompliance\b"),
        ("MINIMUM_BID_DEFICIENCY_NOTICE", r"\bminimum\s+bid\s+price\s+deficiency\b"),
    )
    action_pattern = re.compile(
        r"\b(?:received|notified|sent|issued|provided|announced|intends?\s+to|plans?\s+to|"
        r"would\s+be|will\s+be|was\s+delisted|is\s+delisted|delisted|voluntarily\s+delist)\b",
        re.I,
    )
    for sentence in sentences:
        for predicate, trigger_pattern in triggers:
            trigger = re.search(trigger_pattern, sentence, re.I)
            if not trigger:
                continue
            action_candidates = list(action_pattern.finditer(sentence))
            if not action_candidates:
                continue
            action = min(action_candidates, key=lambda item: abs(item.start() - trigger.start()))
            if not _action_is_affirmed(
                sentence,
                action,
                support_start=min(action.start(), trigger.start()),
                support_end=max(action.end(), trigger.end()),
            ):
                continue
            security = _SECURITY_RE.search(sentence)
            actor = re.search(
                r"\b(?:Nasdaq(?:\s+Stock\s+Market)?|NYSE(?:\s+American)?|"
                r"New\s+York\s+Stock\s+Exchange|the\s+exchange)\b",
                sentence[: action.start()],
                re.I,
            )
            subject = _listing_subject(
                sentence,
                expected_subject,
                action=action,
                trigger=trigger,
            )
            _append_fact(
                facts,
                sentence=sentence,
                expected_subject=expected_subject,
                predicate=predicate,
                action=action,
                object_text=security.group(0) if security else trigger.group(0),
                actor_text=actor.group(0) if actor else None,
                subject_override=subject,
                extraction_rule="listing-status-v1",
            )
    return facts


def _extract_financing_facts(
    sentences: tuple[str, ...], expected_subject: str
) -> list[EvidenceFactSlots]:
    facts: list[EvidenceFactSlots] = []
    instrument = (
        r"common\s+stock|common\s+shares|ordinary\s+shares|preferred\s+stock|"
        r"pre-funded\s+warrants?|warrants?|convertible\s+(?:senior\s+)?notes?|"
        r"senior\s+(?:unsecured\s+)?notes?|debt\s+securities|credit\s+facility|"
        r"registered\s+direct\s+offering|public\s+offering|at-the-market\s+offering|"
        r"private\s+placement|refinancing"
    )
    amount = r"(?:(?:US\$|\$)\s*\d[\d,.]*(?:\s*(?:million|billion|thousand))?)"
    patterns = (
        re.compile(
            rf"(?P<action>issue(?:d|s)?|sold)\s+"
            rf"(?:(?:an?|the|of|additional|approximately|up\s+to|aggregate\s+principal\s+amount\s+of)\s+|{amount}\s+){{0,8}}"
            rf"(?P<object>{instrument})\b",
            re.I,
        ),
        re.compile(
            rf"(?P<action>priced|completed|closed|commenced|announced|authorized)\s+"
            rf"(?:(?:its|an?|the|approximately|up\s+to)\s+|{amount}\s+){{0,6}}"
            rf"(?P<object>{instrument})\b",
            re.I,
        ),
        re.compile(
            rf"(?P<action>entered\s+into|amend(?:ed|s)?|"
            rf"extend(?:ed|s)?(?:\s+and\s+increase(?:d|s)?)?|increase(?:d|s)?)\s+"
            rf"(?:(?:an?|the|its|existing|amended|restated|revolving|senior|secured)\s+){{0,8}}"
            rf"(?P<object>credit\s+facility)\b",
            re.I,
        ),
        re.compile(
            rf"(?P<action>refinance(?:d|s)?|repay(?:ed|s)?)\s+"
            rf"(?:(?:an?|the|its|existing|outstanding|in\s+full)\s+){{0,8}}"
            rf"(?P<object>debt|notes?|credit\s+facility|refinancing)\b",
            re.I,
        ),
        re.compile(
            rf"(?P<action>advanced)\s+(?:(?:an?|the|additional)\s+|{amount}\s+){{0,5}}"
            rf"(?P<object>working\s+capital|working\s+capital\s+note|promissory\s+note)\b",
            re.I,
        ),
    )
    for sentence in sentences:
        relation = None
        for pattern in patterns:
            relation = pattern.search(sentence)
            if relation is not None:
                break
        if relation is None:
            continue
        action = re.compile(re.escape(relation.group("action")), re.I).search(
            sentence, relation.start("action"), relation.end("action")
        )
        if action is None:
            continue
        if not _action_is_affirmed(
            sentence,
            action,
            support_start=relation.start(),
            support_end=relation.end(),
        ):
            continue
        predicate_map = {
            "issue": "SECURITIES_ISSUANCE",
            "issued": "SECURITIES_ISSUANCE",
            "issues": "SECURITIES_ISSUANCE",
            "sold": "SECURITIES_SALE",
            "priced": "OFFERING_PRICED",
            "completed": "FINANCING_COMPLETED",
            "closed": "FINANCING_CLOSED",
            "commenced": "OFFERING_COMMENCED",
            "announced": "FINANCING_ANNOUNCED",
            "entered into": "FINANCING_AGREEMENT",
            "amend": "FINANCING_AMENDMENT",
            "amended": "FINANCING_AMENDMENT",
            "amends": "FINANCING_AMENDMENT",
            "extend": "FACILITY_EXTENSION",
            "extended": "FACILITY_EXTENSION",
            "extends": "FACILITY_EXTENSION",
            "extend and increase": "FACILITY_EXTENSION",
            "extended and increased": "FACILITY_EXTENSION",
            "extends and increases": "FACILITY_EXTENSION",
            "increase": "FACILITY_INCREASE",
            "increased": "FACILITY_INCREASE",
            "increases": "FACILITY_INCREASE",
            "refinance": "DEBT_REFINANCING",
            "refinanced": "DEBT_REFINANCING",
            "refinances": "DEBT_REFINANCING",
            "repay": "DEBT_REPAYMENT",
            "repaid": "DEBT_REPAYMENT",
            "repays": "DEBT_REPAYMENT",
            "advanced": "SPONSOR_ADVANCE",
            "authorized": "FINANCING_AUTHORIZATION",
        }
        action_key = action.group(0).casefold()
        if action_key.startswith("extend"):
            predicate = "FACILITY_EXTENSION"
        else:
            predicate = predicate_map[action_key]
        attributed_subject: tuple[str | None, str, bool] | None = None
        beneficiary = re.search(
            r"\b(?i:by|for|of|involving|on\s+behalf\s+of)\s+"
            rf"(?P<company>{_POSITIONAL_ORG_NAME})\b",
            sentence[relation.end() :],
        )
        if beneficiary is not None and _clean(
            beneficiary.group("company")
        ).casefold() != _clean(expected_subject).casefold():
            attributed_subject = (
                beneficiary.group("company"),
                "OTHER_NAMED_ENTITY",
                False,
            )
        _append_fact(
            facts,
            sentence=sentence,
            expected_subject=expected_subject,
            predicate=predicate,
            action=action,
            object_text=relation.group("object"),
            subject_override=attributed_subject,
            extraction_rule="financing-action-v1",
        )
    return facts


def _extract_transaction_facts(
    sentences: tuple[str, ...], expected_subject: str
) -> list[EvidenceFactSlots]:
    facts: list[EvidenceFactSlots] = []
    predicate_map = {
        "entered into": "TRANSACTION_AGREEMENT_ENTERED",
        "signed": "TRANSACTION_AGREEMENT_SIGNED",
        "executed": "TRANSACTION_AGREEMENT_EXECUTED",
        "approved": "TRANSACTION_APPROVED",
        "completed": "TRANSACTION_COMPLETED",
        "closed": "TRANSACTION_CLOSED",
        "consummated": "TRANSACTION_CONSUMMATED",
        "terminated": "TRANSACTION_TERMINATED",
        "abandoned": "TRANSACTION_ABANDONED",
    }
    # Each rule binds the action to the transaction object syntactically.  A
    # loose same-sentence co-occurrence (for example, "completed diligence"
    # near an attached merger agreement) is intentionally insufficient.
    patterns = (
        re.compile(
            r"(?P<action>entered\s+into|signed|executed)\s+(?:a|an|the)?\s*"
            r"(?P<object>merger\s+agreement|business\s+combination\s+agreement)",
            re.I,
        ),
        re.compile(
            r"(?P<action>approved)\s+(?:a|an|the)?\s*"
            r"(?P<object>merger|merger\s+agreement|business\s+combination|acquisition)",
            re.I,
        ),
        re.compile(
            r"(?P<action>completed|closed|consummated)\s+(?:a|an|the)?\s*"
            r"(?P<object>merger|business\s+combination|acquisition(?:\s+of\s+[^,.;]+?)?)"
            r"(?=\s+(?:for|on|under|pursuant\s+to|effective)\b|[.,;]|$)",
            re.I,
        ),
        re.compile(
            r"(?P<object>merger|business\s+combination|acquisition)\s+"
            r"(?:was|has\s+been|had\s+been)\s+(?P<action>approved|completed|closed|consummated|terminated|abandoned)",
            re.I,
        ),
        re.compile(
            r"(?P<object>merger\s+agreement|merger|business\s+combination|acquisition)\s+"
            r"(?P<action>closed|completed|terminated|abandoned)\b",
            re.I,
        ),
        re.compile(
            r"(?P<action>terminated|abandoned)\s+(?:a|an|the)?\s*"
            r"(?P<object>merger|merger\s+agreement|business\s+combination|acquisition)",
            re.I,
        ),
    )
    for sentence in sentences:
        relation = None
        for pattern in patterns:
            relation = pattern.search(sentence)
            if relation is not None:
                break
        if relation is None:
            continue
        action = re.compile(re.escape(relation.group("action")), re.I).search(
            sentence, relation.start("action"), relation.end("action")
        )
        if action is None:
            continue
        if not _action_is_affirmed(
            sentence,
            action,
            support_start=relation.start(),
            support_end=relation.end(),
        ):
            continue
        counterparty = re.search(
            r"\b(?:with|by)\s+(?P<party>[A-Z][A-Za-z0-9&.'’\-]*(?:\s+[A-Z][A-Za-z0-9&.'’\-]*){0,5})",
            sentence,
        )
        _append_fact(
            facts,
            sentence=sentence,
            expected_subject=expected_subject,
            predicate=predicate_map[action.group(0).casefold()],
            action=action,
            object_text=relation.group("object"),
            counterparty_text=counterparty.group("party") if counterparty else None,
            extraction_rule="corporate-transaction-v1",
        )
    return facts


def extract_evidence_fact_slots(
    *, evidence_passage: str, event_type: str, expected_subject: str = ""
) -> EvidenceFactExtraction:
    """Extract narrow event facts without an LLM or unstated completion.

    Unsupported event types deliberately return no fact rather than treating a
    category label as a factual sentence.  Callers can retain the lead for
    human review and display the limitation instead of inventing slot values.
    """

    raw_passage = str(evidence_passage or "")
    compact = _clean(raw_passage)
    sentences = _sentence_windows(compact)
    normalized_type = _clean(event_type).casefold()
    if normalized_type in {"management_change", "chief_financial_officer_appointment"}:
        facts = _extract_management_facts(sentences, expected_subject)
    elif normalized_type in {"delisting", "minimum_bid_price_deficiency_notice"}:
        facts = _extract_listing_facts(sentences, expected_subject)
    elif normalized_type in {
        "offering_or_dilution",
        "debt_refinancing",
        "convertible_debt_financing",
        "senior_unsecured_debt_financing",
        "credit_facility_amendment",
    }:
        facts = _extract_financing_facts(sentences, expected_subject)
    elif normalized_type in {
        "merger_or_acquisition",
        "material_corporate_transaction",
    }:
        facts = _extract_transaction_facts(sentences, expected_subject)
    else:
        facts = []
    facts = _drop_cross_sentence_ambiguous_pronouns(
        facts,
        sentences,
        expected_subject,
    )
    facts = _drop_postposed_denied_facts(facts, sentences)

    missing: list[str] = []
    compatible_supported = tuple(
        fact.subject_binding
        in {"EXPLICIT_ISSUER", "EXPLICIT_ISSUER_CONTEXT"}
        and fact_supports_event_type(normalized_type, fact)
        for fact in facts
    )
    subject_supported = any(compatible_supported)
    if not facts:
        missing.extend(("predicate", "action_text"))
    else:
        if not any(fact_supports_event_type(normalized_type, fact) for fact in facts):
            missing.append("event_type_compatible_predicate")
        if all(not fact.subject_text for fact in facts):
            missing.append("subject_in_passage")
        if not subject_supported:
            missing.append("event_subject_binding")
        if all(not fact.object_text for fact in facts):
            missing.append("object")
    limitation = None
    if not facts:
        limitation = "no_supported_deterministic_fact_pattern"
    elif not any(fact_supports_event_type(normalized_type, fact) for fact in facts):
        limitation = "extracted_fact_does_not_support_requested_event_type"
    elif not subject_supported:
        limitation = "event_subject_not_bound_to_fact_actor"
    elif any(fact.subject_binding == "OTHER_NAMED_ENTITY" for fact in facts):
        limitation = "unbound_fact_actors_omitted_from_public_claim"
    elif any(not fact.issuer_name_explicit for fact in facts):
        limitation = "issuer_name_not_explicit_in_every_fact_sentence"
    return EvidenceFactExtraction(
        contract_version=FACT_SLOT_CONTRACT_VERSION,
        event_type=normalized_type,
        passage_sha256=hashlib.sha256(raw_passage.encode("utf-8")).hexdigest(),
        canonical_passage_sha256=hashlib.sha256(compact.encode("utf-8")).hexdigest(),
        facts=tuple(facts),
        missing_slots=tuple(missing),
        limitation=limitation,
    )


def _is_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _aware_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None


def evidence_relation_fingerprint(
    *,
    event_id: str,
    event_version: int,
    evidence_id: str,
    content_sha256: str,
    subject: str,
    action: str,
    stage: str,
    known_at: str,
    contract_version: str = LEGACY_ADMISSION_CONTRACT_VERSION,
    evidence_passage_sha256: str = "",
    fact_slot_receipt_sha256: str = "",
    public_fact_summary_sha256: str = "",
) -> str:
    payload = {
        "action": _clean(action),
        "content_sha256": _clean(content_sha256).lower(),
        "event_id": _clean(event_id),
        "event_version": int(event_version),
        "evidence_id": _clean(evidence_id),
        "known_at": _clean(known_at),
        "stage": _clean(stage).upper(),
        "subject": _clean(subject),
    }
    if contract_version == ADMISSION_CONTRACT_VERSION:
        payload.update(
            {
                "contract_version": ADMISSION_CONTRACT_VERSION,
                "evidence_passage_sha256": _clean(evidence_passage_sha256).lower(),
                "fact_slot_receipt_sha256": _clean(fact_slot_receipt_sha256).lower(),
                "public_fact_summary_sha256": _clean(public_fact_summary_sha256).lower(),
            }
        )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def fact_slot_receipt_sha256(
    *,
    extraction: EvidenceFactExtraction | None,
    public_fact_summary_text: str,
) -> str:
    payload = {
        "admission_contract_version": ADMISSION_CONTRACT_VERSION,
        "fact_slots": extraction.as_dict() if extraction is not None else None,
        "public_fact_summary": str(public_fact_summary_text or ""),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def evaluate_event_admission(
    *,
    event_id: str,
    event_version: int,
    evidence_id: str,
    subject: str,
    action: str,
    stage: str,
    known_at: str,
    source_authority_tier: str,
    evidence_url: str,
    evidence_passage: str,
    evidence_status: str,
    content_sha256: str,
    subject_match: bool,
    event_claim_supported: bool,
    date_coherent: bool,
    fact_extraction: EvidenceFactExtraction | None = None,
    public_fact_summary_text: str = "",
) -> AdmissionDecision:
    """Fail closed before a discovery lead may become a canonical event claim.

    Passing this contract creates an evidence-supported *candidate*.  It does
    not verify the fact, assign materiality, create a trading signal, or grant
    an automated canonical-conclusion write.
    """

    normalized_stage = _clean(stage).upper()
    normalized_status = _clean(evidence_status)
    reasons: list[str] = []
    if len(_clean(subject)) < 2:
        reasons.append("MISSING_NAMED_SUBJECT")
    if len(_clean(action)) < 3:
        reasons.append("MISSING_EVENT_ACTION")
    if normalized_stage not in EVENT_STAGES:
        reasons.append("MISSING_OR_INVALID_STAGE")
    if not _aware_timestamp(_clean(known_at)):
        reasons.append("MISSING_OR_NAIVE_KNOWN_AT")
    if not is_primary_authority_tier(source_authority_tier):
        reasons.append("SOURCE_NOT_P0_P1")
    if not _is_http_url(_clean(evidence_url)):
        reasons.append("MISSING_CITABLE_URL")
    if len(_clean(evidence_passage)) < 40:
        reasons.append("MISSING_EXACT_PASSAGE")
    if normalized_status not in READER_ALLOWED_EVIDENCE_STATUSES:
        reasons.append("EVIDENCE_STATUS_NOT_SUPPORTIVE")
    if normalized_status in READER_BLOCKED_EVIDENCE_STATUSES:
        reasons.append("EVIDENCE_STATUS_EXPLICITLY_BLOCKED")
    if not subject_match:
        reasons.append("SUBJECT_NOT_BOUND_TO_EVIDENCE")
    if not event_claim_supported:
        reasons.append("EVENT_PREDICATE_NOT_SUPPORTED")
    if not date_coherent:
        reasons.append("EVENT_DATE_NOT_COHERENT")
    if len(_clean(content_sha256)) != 64:
        reasons.append("MISSING_SOURCE_CONTENT_HASH")

    raw_passage_sha256 = hashlib.sha256(str(evidence_passage or "").encode("utf-8")).hexdigest()
    replayed_extraction = extract_evidence_fact_slots(
        evidence_passage=evidence_passage,
        event_type=action,
        expected_subject=subject,
    )
    expected_summary = ""
    if fact_extraction is None:
        reasons.append("MISSING_CURRENT_FACT_SLOT_RECEIPT")
    else:
        if fact_extraction.contract_version != FACT_SLOT_CONTRACT_VERSION:
            reasons.append("FACT_SLOT_CONTRACT_VERSION_MISMATCH")
        if fact_extraction.event_type != _clean(action).casefold():
            reasons.append("FACT_SLOT_EVENT_TYPE_MISMATCH")
        if fact_extraction.passage_sha256 != raw_passage_sha256:
            reasons.append("FACT_SLOT_PASSAGE_HASH_MISMATCH")
        if fact_extraction.as_dict() != replayed_extraction.as_dict():
            reasons.append("FACT_SLOT_EXTRACTION_NOT_REPRODUCIBLE")
        if not replayed_extraction.supports_specific_fact:
            reasons.append("FACT_SLOT_HAS_NO_ISSUER_BOUND_FACT")
        else:
            expected_summary = public_fact_summary(
                subject=subject,
                action_label=action,
                stage_label=normalized_stage,
                extraction=replayed_extraction,
            )
            if str(public_fact_summary_text or "") != expected_summary:
                reasons.append("PUBLIC_FACT_SUMMARY_NOT_REPRODUCIBLE")

    slot_receipt_sha256 = fact_slot_receipt_sha256(
        extraction=fact_extraction,
        public_fact_summary_text=public_fact_summary_text,
    )
    summary_sha256 = hashlib.sha256(
        str(public_fact_summary_text or "").encode("utf-8")
    ).hexdigest()

    fingerprint = evidence_relation_fingerprint(
        event_id=event_id,
        event_version=event_version,
        evidence_id=evidence_id,
        content_sha256=content_sha256,
        subject=subject,
        action=action,
        stage=normalized_stage,
        known_at=known_at,
        contract_version=ADMISSION_CONTRACT_VERSION,
        evidence_passage_sha256=raw_passage_sha256,
        fact_slot_receipt_sha256=slot_receipt_sha256,
        public_fact_summary_sha256=summary_sha256,
    )
    return AdmissionDecision(
        admitted=not reasons,
        workflow_state="EVIDENCE_READY" if not reasons else "NEEDS_EVIDENCE",
        reasons=tuple(reasons),
        evidence_fingerprint=fingerprint,
        fact_slot_receipt_sha256=slot_receipt_sha256,
    )


_PREDICATE_LABELS = {
    "OFFICER_DEPARTURE": "辞任或离任",
    "OFFICER_APPOINTMENT": "任命",
    "DELISTING_NOTICE": "披露退市通知",
    "DELISTING_ACTION": "披露退市动作",
    "LISTING_NONCOMPLIANCE_NOTICE": "披露上市不合规",
    "MINIMUM_BID_DEFICIENCY_NOTICE": "披露最低买价缺陷",
    "SECURITIES_ISSUANCE": "发行",
    "SECURITIES_SALE": "出售",
    "OFFERING_PRICED": "定价",
    "FINANCING_COMPLETED": "完成融资",
    "FINANCING_CLOSED": "完成交割",
    "OFFERING_COMMENCED": "启动发行",
    "FINANCING_ANNOUNCED": "宣布融资",
    "FINANCING_AGREEMENT": "签订融资安排",
    "FINANCING_AMENDMENT": "修订融资安排",
    "FACILITY_EXTENSION": "延长融资额度",
    "FACILITY_INCREASE": "增加融资额度",
    "DEBT_REFINANCING": "再融资",
    "DEBT_REPAYMENT": "偿还债务",
    "SPONSOR_ADVANCE": "提供垫款",
    "FINANCING_AUTHORIZATION": "授权融资",
    "TRANSACTION_AGREEMENT_ENTERED": "签订交易协议",
    "TRANSACTION_AGREEMENT_SIGNED": "签署交易协议",
    "TRANSACTION_AGREEMENT_EXECUTED": "签署交易协议",
    "TRANSACTION_APPROVED": "批准交易",
    "TRANSACTION_COMPLETED": "完成交易",
    "TRANSACTION_CLOSED": "交易交割",
    "TRANSACTION_CONSUMMATED": "完成交易",
    "TRANSACTION_TERMINATED": "终止交易",
    "TRANSACTION_ABANDONED": "放弃交易",
}


def _public_subject(fact: EvidenceFactSlots) -> str:
    if fact.issuer_name_explicit and fact.subject_text:
        return f"“{fact.subject_text}”"
    if fact.subject_text:
        return f"原文中的“{fact.subject_text}”（该句未重述发行人名称）"
    return "该证据句（未出现执行主体）"


def _render_public_fact(fact: EvidenceFactSlots) -> str:
    label = _PREDICATE_LABELS.get(fact.predicate, "执行相关动作")
    subject = _public_subject(fact)
    if fact.predicate == "OFFICER_DEPARTURE":
        actor = fact.person_text or fact.role_text or fact.object_text
        core = f"{subject}的“{actor}”{label}" if actor else f"{subject}{label}"
    elif fact.predicate == "OFFICER_APPOINTMENT":
        target = fact.person_text or fact.role_text or fact.object_text
        core = f"{subject}{label}“{target}”" if target else f"{subject}{label}"
    else:
        core = f"“{fact.actor_text}”对{subject}{label}" if fact.actor_text else f"{subject}{label}"
        if fact.object_text:
            core += f"“{fact.object_text}”"
    core += f"（原文动作“{fact.action_text}”）"
    if fact.counterparty_text:
        core += f"，交易对方“{fact.counterparty_text}”"
    if fact.amount_text:
        core += f"，金额“{fact.amount_text}”"
    if fact.effective_text:
        core += f"，生效表述“{fact.effective_text}”"
        if fact.date_text and fact.date_text not in fact.effective_text:
            core += f"，日期“{fact.date_text}”"
    elif fact.date_text:
        core += f"，日期“{fact.date_text}”"
    if fact.modality != "ASSERTED":
        original = f"，原文情态“{fact.modality_text}”" if fact.modality_text else ""
        core += f"，情态为 {fact.modality}{original}"
    return core + "。"


def public_fact_summary(
    *,
    subject: str,
    action_label: str,
    stage_label: str,
    extraction: EvidenceFactExtraction | None = None,
) -> str:
    """Build public copy from deterministic passage slots, never a type label alone."""

    supported_facts = extraction.supported_facts if extraction is not None else ()
    if extraction is None or not supported_facts:
        return (
            "该精确证据段落尚不能通过确定性规则回答“谁做了什么”；"
            f"“{_clean(action_label)}”仅是候选分类，不能当作已发生事实。"
            "缺失动作或对象槽位，需人工核验原文。"
        )
    rendered = "".join(_render_public_fact(fact) for fact in supported_facts[:3])
    limitation = ""
    if extraction.missing_slots:
        limitation = f"仍缺少槽位：{'、'.join(extraction.missing_slots)}。"
    elif extraction.limitation == "unbound_fact_actors_omitted_from_public_claim":
        limitation = "段落中另有其他具名主体的动作，未并入本事件摘要。"
    elif extraction.limitation == "issuer_name_not_explicit_in_every_fact_sentence":
        limitation = "部分句子未重述发行人名称，未用外部元数据补写主体。"
    return (
        rendered
        + limitation
        + f"系统记录阶段为“{_clean(stage_label)}”；以上仅为规则抽取，尚待人工核验。"
    )
