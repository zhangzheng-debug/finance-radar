"""Canonical evidence-state predicates shared by review and model gates."""

from __future__ import annotations

from typing import Any


# These are terminal contradiction states in the evidence lifecycle.  Keep the
# vocabulary centralized: a contradiction must never be downgraded to a
# supporting edge merely because one caller recognizes a different spelling.
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


def normalize_evidence_status(value: Any) -> str:
    """Normalize a persisted lifecycle state without inventing a new state."""

    return "_".join(str(value or "").replace("-", "_").casefold().split())


def is_conflicting_evidence_status(value: Any) -> bool:
    """Whether a persisted evidence lifecycle state requires human review."""

    return normalize_evidence_status(value) in CONFLICTING_EVIDENCE_STATUSES
