from __future__ import annotations

import math
import re
from typing import Any


EVIDENCE_POSTURES = frozenset(
    {
        "PRIMARY_SUPPORTED",
        "PRIMARY_SOURCE_AVAILABLE",
        "SOURCE_CAPTURED",
        "NO_SOURCE",
    }
)
EVIDENCE_GAP_CODES = frozenset(
    {
        "MISSING_SUBJECT",
        "MISSING_FACT_SUMMARY",
        "MISSING_CITABLE_EVIDENCE",
        "NO_CAPTURED_SOURCE",
    }
)
RISK_ROUTES = frozenset({"RISK_REVIEW", "NON_TARGET", "ABSTAIN"})
RISK_DECISION_SOURCES = frozenset(
    {
        "DETERMINISTIC_EVIDENCE_GATE",
        "DETERMINISTIC_SEMANTIC_POLICY_GATE",
        "TRAINED_SEMANTIC_MODEL",
        "KEYWORD_FALLBACK",
        "LEGACY_SCOPE_GUARDRAIL",
    }
)
RISK_EVIDENCE_STATES = frozenset(
    {
        "CONFLICTED",
        "PRIMARY_SUPPORTED_REVIEWED",
        "PRIMARY_SUPPORTED_LIGHT_VERIFIED",
        "PRIMARY_SUPPORTED_MACHINE_OFFICIAL",
        "DISCOVERY_ONLY",
        "INSUFFICIENT",
        "NOT_PROVIDED",
    }
)
QWEN_POLARITIES = frozenset({"ADVERSE", "POSITIVE", "NEUTRAL", "MIXED", "UNCLEAR"})
QWEN_MATERIALITY = frozenset(
    {"MATERIAL_ADVERSE", "NOT_MATERIAL_ADVERSE", "UNCLEAR"}
)
QWEN_ADVERSE_STRENGTHS = frozenset({"HIGH", "LOW", "NONE", "UNCLEAR"})
QWEN_SEMANTIC_PRIORITIES = frozenset(
    {"PRIORITY_REVIEW", "ROUTINE", "UNDECIDABLE"}
)
QWEN_ASSESSMENT_SCOPES = frozenset({"EVIDENCE_SUPPORTED", "SOURCE_CONDITIONAL"})
PUBLIC_HEADLINE_MODES = frozenset({"FACT", "ATTRIBUTED_SOURCE", "RECORD"})
GENERIC_SOURCE_HEADLINE = re.compile(
    r"^(?:sec\s+)?(?:8-k|6-k|10-k|10-q|20-f|40-f|25(?:-nse)?|15-12g)"
    r"(?:\s+[a-z0-9.\-]+|\s*[-:]\s*[^:]{0,90})?$|"
    r"^[a-z0-9.\-]+\s+[a-z0-9_\-]+\s+candidate$",
    re.I,
)
GENERIC_SOURCE_SUMMARY_PREFIXES = (
    "action in delisted/voluntarydelisting; value=delisted",
    "certifies that it has reasonable grounds to believe that it meets all of the requirements for filing the form 25",
)


def _is_generic_recovery_title(value: str | None) -> bool:
    """Return whether a historical repair title contains no event action."""
    normalized = " ".join(str(value or "").lower().split())
    return normalized.startswith(
        (
            "accepted official evidence for ",
            "official evidence for ",
            "accepted evidence for ",
        )
    )


def _is_generic_source_headline(value: str | None) -> bool:
    """Return whether a source title identifies a filing, not its event."""

    normalized = " ".join(str(value or "").split())
    return bool(normalized and GENERIC_SOURCE_HEADLINE.fullmatch(normalized))


def _is_generic_source_summary(value: str | None) -> bool:
    """Return whether an excerpt is provider/form boilerplate, not event text."""

    normalized = " ".join(str(value or "").casefold().split())
    return normalized.startswith(GENERIC_SOURCE_SUMMARY_PREFIXES)


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def derive_public_event_semantics(event: dict[str, Any]) -> dict[str, Any]:
    """Derive reader-facing evidence semantics from the strict ledger gate.

    These fields describe the evidence available for the current event version;
    they deliberately do not reuse the internal review workflow vocabulary.
    ``reader_ready`` remains the single compatibility source for citation
    readiness, while the posture and gap codes explain why it is or is not set.
    """

    citation_ready = _count(event.get("reader_ready")) == 1
    citable_evidence_count = _count(event.get("citable_evidence_count"))
    captured_source_count = _count(event.get("captured_source_count"))

    if citation_ready:
        evidence_posture = "PRIMARY_SUPPORTED"
    elif citable_evidence_count > 0:
        evidence_posture = "PRIMARY_SOURCE_AVAILABLE"
    elif captured_source_count > 0:
        evidence_posture = "SOURCE_CAPTURED"
    else:
        evidence_posture = "NO_SOURCE"

    gap_codes: list[str] = []
    if _count(event.get("reader_has_subject")) != 1:
        gap_codes.append("MISSING_SUBJECT")
    if _count(event.get("reader_has_fact_summary")) != 1:
        gap_codes.append("MISSING_FACT_SUMMARY")
    if citable_evidence_count == 0:
        gap_codes.append("MISSING_CITABLE_EVIDENCE")
        if captured_source_count == 0:
            gap_codes.append("NO_CAPTURED_SOURCE")

    return {
        "citation_ready": citation_ready,
        "evidence_posture": evidence_posture,
        "evidence_gap_codes": gap_codes,
    }


def derive_public_display_headline(
    event: dict[str, Any],
    captured_source: dict[str, Any] | None = None,
) -> dict[str, str | None]:
    """Return a provenance-aware headline for the public browsing loop.

    A source headline can make a discovery record scannable without promoting
    it to a verified event fact.  The explicit mode is part of the display
    contract so every reader can distinguish a supported fact, an attributed
    source statement, and a minimal ledger record.
    """

    citation_ready = _count(event.get("reader_ready")) == 1
    fact_summary = _bounded_text(event.get("public_fact_summary"), 180)
    if citation_ready and fact_summary:
        return {
            "display_headline": fact_summary,
            "headline_mode": "FACT",
            "headline_source": None,
        }

    source = captured_source if isinstance(captured_source, dict) else event
    source_title = _bounded_text(source.get("source_title") or source.get("title"), 180)
    source_summary = _bounded_text(
        source.get("source_summary")
        or source.get("summary")
        or event.get("unverified_capture_excerpt"),
        180,
    )
    # Recovery titles such as "Accepted official evidence for XYZ" describe
    # an import operation, not the event.  Prefer the attributed source excerpt
    # so the browsing headline answers what the source actually says.
    generic_title = _is_generic_recovery_title(source_title) or _is_generic_source_headline(
        source_title
    )
    useful_summary = source_summary if not _is_generic_source_summary(source_summary) else None
    source_headline = useful_summary if generic_title and useful_summary else source_title or useful_summary
    if _is_generic_source_headline(source_headline):
        source_headline = None
    if source_headline:
        return {
            "display_headline": source_headline,
            "headline_mode": "ATTRIBUTED_SOURCE",
            "headline_source": _bounded_text(
                source.get("source_name")
                or source.get("name"),
                80,
            ),
        }

    subject = _bounded_text(
        event.get("company_name") or event.get("ticker_at_event"), 100
    )
    event_date = _bounded_text(event.get("event_date"), 32)
    record_parts = [part for part in (subject, event_date) if part]
    return {
        "display_headline": " · ".join(record_parts) or "事件记录",
        "headline_mode": "RECORD",
        "headline_source": None,
    }


def _bounded_enum(value: Any, allowed: frozenset[str]) -> str | None:
    normalized = str(value or "").strip().upper()
    return normalized if normalized in allowed else None


def _bounded_text(value: Any, limit: int) -> str | None:
    normalized = " ".join(str(value or "").split())
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    clipped = normalized[: max(1, limit - 1)].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0].rstrip()
    return clipped + "…"


def _applicable_confidence(output: dict[str, Any], run: dict[str, Any]) -> float | None:
    if output.get("confidence_applicable") is not True:
        return None
    raw = output.get("confidence", run.get("confidence"))
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return round(value, 6)


def project_public_risk_assessment(
    run: dict[str, Any] | None,
    *,
    current_version: int,
) -> dict[str, Any] | None:
    """Project one current shadow model run without internal diagnostics.

    A stale result is not a current event assessment.  Callers may therefore
    safely display every non-null projection without comparing versions again.
    Scores from deterministic gates and keyword fallbacks remain intentionally
    hidden because their ``confidence_applicable`` contract is false.
    """

    if not isinstance(run, dict):
        return None
    output = run.get("output")
    if not isinstance(output, dict):
        return None
    try:
        evaluated_version = int(output.get("event_version") or 0)
    except (TypeError, ValueError):
        return None
    if evaluated_version != int(current_version or 0):
        return None

    route = _bounded_enum(output.get("label", run.get("output_label")), RISK_ROUTES)
    if route is None:
        return None
    evidence_gate = output.get("evidence_gate")
    evidence_gate = evidence_gate if isinstance(evidence_gate, dict) else {}
    evidence_state = _bounded_enum(
        output.get("evidence_state") or evidence_gate.get("state") or "NOT_PROVIDED",
        RISK_EVIDENCE_STATES,
    )
    decision_source = _bounded_enum(
        output.get("decision_source") or output.get("call_kind"),
        RISK_DECISION_SOURCES,
    )

    return {
        "route": route,
        "confidence": _applicable_confidence(output, run),
        "confidence_applicable": output.get("confidence_applicable") is True,
        "model_version": _bounded_text(
            output.get("model_version") or run.get("model_version"), 160
        ),
        "decision_source": decision_source,
        "evidence_state": evidence_state or "NOT_PROVIDED",
        "evaluated_at": _bounded_text(run.get("created_at"), 80),
        "shadow": bool(run.get("shadow", output.get("shadow", True))),
        "current": True,
    }


def project_public_qwen_semantics(
    run: dict[str, Any] | None,
    *,
    current_version: int,
) -> dict[str, Any] | None:
    """Project human-gold-trained semantics without implying fact verification."""

    if not isinstance(run, dict):
        return None
    if run.get("publication_state") != "PUBLIC_APPROVED":
        return None
    if run.get("current_input") is not True:
        return None
    output = run.get("output")
    if not isinstance(output, dict) or output.get("model_task") != "QWEN_RISK_SEMANTICS":
        return None
    try:
        evaluated_version = int(output.get("event_version") or 0)
    except (TypeError, ValueError):
        return None
    if evaluated_version != int(current_version or 0):
        return None
    polarity = _bounded_enum(output.get("polarity"), QWEN_POLARITIES)
    materiality = _bounded_enum(output.get("materiality"), QWEN_MATERIALITY)
    strength = _bounded_enum(output.get("adverse_strength"), QWEN_ADVERSE_STRENGTHS)
    priority = _bounded_enum(output.get("semantic_priority"), QWEN_SEMANTIC_PRIORITIES)
    scope = _bounded_enum(output.get("assessment_scope"), QWEN_ASSESSMENT_SCOPES)
    if None in {polarity, materiality, strength, priority, scope}:
        return None
    return {
        "polarity": polarity,
        "materiality": materiality,
        "adverse_strength": strength,
        "semantic_priority": priority,
        "assessment_scope": scope,
        "conditional_language_required": scope == "SOURCE_CONDITIONAL",
        "model_version": _bounded_text(
            output.get("model_version") or run.get("model_version"), 160
        ),
        "evaluated_at": _bounded_text(run.get("created_at"), 80),
        "training_basis": "INDEPENDENT_DUAL_HUMAN_GOLD",
        "automatic": True,
        "confirms_event_fact": False,
        "confidence": None,
        "current": True,
        "no_trading": True,
        "publication_state": "PUBLIC_APPROVED",
        "shadow": False,
    }
