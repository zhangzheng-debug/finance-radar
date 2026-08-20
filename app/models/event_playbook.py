"""Evidence-reading rules for each event family, bound to the deterministic gates.

A playbook card explains how a human establishes that one kind of event actually
happened: who is allowed to say it, what the primary record must contain, what
resembles it but is not it, and which timestamp anchors the event.

A card never states what an event implies for price, return, timing or position.
``FR-SHORT-004`` forbids the system from emitting or deriving that vocabulary,
and ``tests/test_event_playbook.py`` enforces the same rule on this content, so
the prose cannot drift away from the boundary the product claims to honor.

Cards are not free-standing documentation.  Every card names the deterministic
gates it describes through ``gate_refs``; the loader rejects a reference that no
longer resolves, so a gate rename fails the build instead of silently leaving a
card that describes behaviour the system no longer has.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.models.event_taxonomy import classify_event


ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK_PATH = ROOT / "config" / "event_playbook_v1.json"
PLAYBOOK_SCHEMA_VERSION = 1

# A window is only auditable when the anchor it was measured from is recorded.
# ``first_capture`` remains only as an explicit legacy/degraded vocabulary item.
# The v2 observer rejects it as a reaction anchor, while older receipts can still
# be described honestly instead of being silently relabelled.
TIME_ANCHORS = ("event_occurred", "source_published", "filing_effective", "first_capture")
DEGRADED_TIME_ANCHORS = frozenset({"first_capture"})

CARD_KINDS = ("confirm", "impostor")

REQUIRED_CARD_FIELDS = (
    "id",
    "event_family",
    "kind",
    "title",
    "authoritative_sources",
    "gate_refs",
    "time_anchor",
    "corroboration_min",
    "insufficient_when",
)


@dataclass(frozen=True)
class PlaybookCard:
    """One evidence-reading rule for one event family."""

    id: str
    event_family: str
    kind: str
    title: str
    authoritative_sources: tuple[str, ...]
    gate_refs: tuple[str, ...]
    time_anchor: str
    corroboration_min: int
    insufficient_when: tuple[str, ...]
    required_language: tuple[str, ...] = ()
    impostors: tuple[str, ...] = ()
    reader_note: str = ""

    @property
    def anchor_is_degraded(self) -> bool:
        return self.time_anchor in DEGRADED_TIME_ANCHORS

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "event_family": self.event_family,
            "kind": self.kind,
            "title": self.title,
            "authoritative_sources": list(self.authoritative_sources),
            "gate_refs": list(self.gate_refs),
            "time_anchor": self.time_anchor,
            "anchor_is_degraded": self.anchor_is_degraded,
            "corroboration_min": self.corroboration_min,
            "insufficient_when": list(self.insufficient_when),
            "required_language": list(self.required_language),
            "impostors": list(self.impostors),
            "reader_note": self.reader_note,
        }


def _tuple_of_strings(value: Any, *, field: str, card_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"playbook card {card_id}: {field} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _parse_card(raw: Any) -> PlaybookCard:
    if not isinstance(raw, dict):
        raise ValueError("playbook card must be an object")
    card_id = str(raw.get("id") or "").strip()
    if not card_id:
        raise ValueError("playbook card is missing an id")
    for field in REQUIRED_CARD_FIELDS:
        if field not in raw:
            raise ValueError(f"playbook card {card_id}: missing required field {field}")

    kind = str(raw["kind"]).strip()
    if kind not in CARD_KINDS:
        raise ValueError(f"playbook card {card_id}: unknown kind {kind!r}")

    anchor = str(raw["time_anchor"]).strip()
    if anchor not in TIME_ANCHORS:
        raise ValueError(f"playbook card {card_id}: unknown time_anchor {anchor!r}")

    try:
        corroboration = int(raw["corroboration_min"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"playbook card {card_id}: corroboration_min must be an integer") from exc
    if corroboration < 1:
        raise ValueError(f"playbook card {card_id}: corroboration_min must be at least 1")

    gate_refs = _tuple_of_strings(raw["gate_refs"], field="gate_refs", card_id=card_id)
    if not gate_refs:
        raise ValueError(f"playbook card {card_id}: at least one gate_ref is required")

    sources = _tuple_of_strings(raw["authoritative_sources"], field="authoritative_sources", card_id=card_id)
    if not sources:
        raise ValueError(f"playbook card {card_id}: at least one authoritative source is required")

    insufficient = _tuple_of_strings(raw["insufficient_when"], field="insufficient_when", card_id=card_id)
    if not insufficient:
        raise ValueError(f"playbook card {card_id}: insufficient_when must list at least one condition")

    if kind == "impostor" and not raw.get("impostors"):
        raise ValueError(f"playbook card {card_id}: an impostor card must list impostors")

    return PlaybookCard(
        id=card_id,
        event_family=str(raw["event_family"]).strip(),
        kind=kind,
        title=str(raw["title"]).strip(),
        authoritative_sources=sources,
        gate_refs=gate_refs,
        time_anchor=anchor,
        corroboration_min=corroboration,
        insufficient_when=insufficient,
        required_language=_tuple_of_strings(raw.get("required_language"), field="required_language", card_id=card_id),
        impostors=_tuple_of_strings(raw.get("impostors"), field="impostors", card_id=card_id),
        reader_note=str(raw.get("reader_note") or "").strip(),
    )


@lru_cache(maxsize=1)
def load_playbook(path: str | None = None) -> tuple[PlaybookCard, ...]:
    """Load and validate every card. A malformed card fails loudly, not silently."""

    source = Path(path) if path else PLAYBOOK_PATH
    document = json.loads(source.read_text(encoding="utf-8"))
    version = document.get("schema_version")
    if version != PLAYBOOK_SCHEMA_VERSION:
        raise ValueError(f"unsupported playbook schema_version: {version!r}")

    raw_cards = document.get("cards")
    if not isinstance(raw_cards, list) or not raw_cards:
        raise ValueError("playbook contains no cards")

    cards = tuple(_parse_card(item) for item in raw_cards)
    seen: set[str] = set()
    for card in cards:
        if card.id in seen:
            raise ValueError(f"duplicate playbook card id: {card.id}")
        seen.add(card.id)
    return cards


def cards_for_family(
    event_family: str | None, event_type: str | None = None
) -> tuple[PlaybookCard, ...]:
    """Return the cards that explain how to read one event family."""

    normalized = str(event_family or "").strip()
    if not normalized:
        return ()
    direct = tuple(card for card in load_playbook() if card.event_family == normalized)
    if direct:
        return direct
    alias = classify_event(normalized, event_type).playbook_family
    return tuple(card for card in load_playbook() if card.event_family == alias)


def time_anchor_for_family(
    event_family: str | None, event_type: str | None = None
) -> str | None:
    """Return the anchor the confirm card declares for this family.

    The price-window audit consumes this: a scheduled window whose anchor does
    not match the declared anchor is a defect, not a matter of taste.
    """

    for card in cards_for_family(event_family, event_type):
        if card.kind == "confirm":
            return card.time_anchor
    return classify_event(event_family, event_type).time_anchor


def covered_families() -> tuple[str, ...]:
    return tuple(sorted({card.event_family for card in load_playbook()}))


def referenced_gates() -> tuple[str, ...]:
    return tuple(sorted({ref for card in load_playbook() for ref in card.gate_refs}))
