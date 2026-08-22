"""Guard rails for the evidence-reading playbook.

The playbook is prose that describes deterministic behaviour, which makes it the
easiest artefact in the repository to drift.  These tests bind it three ways: the
boundary the product claims (no investment language), the gates it says it
describes (they must still exist), and the sources it names (they must be real
primary sources in the collector registry).
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.models.event_playbook import (  # noqa: E402
    CARD_KINDS,
    TIME_ANCHORS,
    cards_for_family,
    covered_families,
    load_playbook,
    time_anchor_for_family,
)
from app.models.risk_scope_gate import POSITIVE_PATTERNS, RISK_PATTERNS  # noqa: E402
from app.services.light_verification import AUTO_FORMAL_EVENT_TYPES  # noqa: E402
import official_event_collector as collector  # noqa: E402


# FR-SHORT-004 forbids the system from emitting or deriving position, direction,
# target, expected return, timing and order vocabulary.  A playbook card is
# system output, so the same list applies to it.  The terms below are the
# actionable forms only: the doctrine's own "下行风险" routing language and the
# word "做空研究" remain legal, so they are deliberately absent.
FORBIDDEN_INVESTMENT_TERMS = (
    "目标价",
    "预期收益",
    "预期跌幅",
    "预期涨幅",
    "平均跌幅",
    "平均涨幅",
    "通常下跌",
    "通常上涨",
    "往往下跌",
    "往往上涨",
    "建议买入",
    "建议卖出",
    "应当买入",
    "应当卖出",
    "止损",
    "止盈",
    "仓位",
    "杠杆",
    "加仓",
    "减仓",
    "评级上调",
    "评级下调",
    "price target",
    "target price",
    "expected return",
    "buy rating",
    "sell rating",
    "stop loss",
    "take profit",
    "position size",
)

RISK_SCOPE_PREFIX = "risk_scope_gate:"
LIGHT_VERIFICATION_PREFIX = "light_verification:auto_formal_event_types:"
LABEL_CONTRACT_PREFIX = "risk_label_contract_v3:"


def _known_risk_scope_cues() -> set[str]:
    return {code for code, _pattern in RISK_PATTERNS} | {code for code, _pattern in POSITIVE_PATTERNS}


def _known_label_contract_sections() -> set[str]:
    contract = json.loads((ROOT / "config" / "risk_label_contract_v3.json").read_text(encoding="utf-8"))
    sections = set(contract)
    sections |= set(contract.get("independent_axes") or {})
    return sections


def _known_source_ids() -> set[str]:
    specs = [collector.SEC_FEED, collector.FED_FEED, *collector.ADDITIONAL_OFFICIAL_FEEDS]
    return {spec.source_id for spec in specs}


class EventPlaybookContractTests(unittest.TestCase):
    def test_playbook_loads_and_covers_twelve_families_with_both_card_kinds(self) -> None:
        cards = load_playbook()
        self.assertEqual(len(cards), 24)
        self.assertEqual(len(covered_families()), 12)
        for family in covered_families():
            kinds = {card.kind for card in cards_for_family(family)}
            self.assertEqual(kinds, set(CARD_KINDS), f"{family} must carry both a confirm and an impostor card")

    def test_no_card_carries_investment_language(self) -> None:
        """The product boundary is enforced on the prose, not just on the model."""

        for card in load_playbook():
            blob = json.dumps(card.as_dict(), ensure_ascii=False).casefold()
            for term in FORBIDDEN_INVESTMENT_TERMS:
                self.assertNotIn(
                    term.casefold(),
                    blob,
                    f"playbook card {card.id} carries forbidden investment language: {term}",
                )

    def test_every_gate_reference_resolves_to_a_live_gate(self) -> None:
        """A renamed or deleted gate must fail the build, not orphan a card."""

        cues = _known_risk_scope_cues()
        sections = _known_label_contract_sections()
        for card in load_playbook():
            for ref in card.gate_refs:
                if ref.startswith(RISK_SCOPE_PREFIX):
                    self.assertIn(ref[len(RISK_SCOPE_PREFIX):], cues, f"{card.id}: unknown risk-scope cue {ref}")
                elif ref.startswith(LIGHT_VERIFICATION_PREFIX):
                    self.assertIn(
                        ref[len(LIGHT_VERIFICATION_PREFIX):],
                        AUTO_FORMAL_EVENT_TYPES,
                        f"{card.id}: {ref} is not in the automatic-confirmation whitelist",
                    )
                elif ref.startswith(LABEL_CONTRACT_PREFIX):
                    self.assertIn(
                        ref[len(LABEL_CONTRACT_PREFIX):],
                        sections,
                        f"{card.id}: unknown label-contract section {ref}",
                    )
                else:
                    self.fail(f"{card.id}: gate reference {ref} uses an unregistered namespace")

    def test_authoritative_sources_are_real_primary_feeds(self) -> None:
        """A card may not invent a source that the collector never registers."""

        known = _known_source_ids()
        for card in load_playbook():
            for source_id in card.authoritative_sources:
                self.assertIn(source_id, known, f"{card.id}: unknown authoritative source {source_id}")

    def test_time_anchors_are_declared_and_never_silently_degraded(self) -> None:
        """Every family declares its anchor, and no card ships the degraded one."""

        for family in covered_families():
            anchor = time_anchor_for_family(family)
            self.assertIsNotNone(anchor, f"{family} has no confirm card declaring a time anchor")
            self.assertIn(anchor, TIME_ANCHORS)
        for card in load_playbook():
            self.assertFalse(
                card.anchor_is_degraded,
                f"{card.id} declares the degraded first_capture anchor; that value exists to describe "
                "the observer's current behaviour, not to be adopted as a family's contract",
            )

    def test_every_card_states_when_evidence_is_insufficient(self) -> None:
        """Honest uncertainty is a required field, not an optional flourish."""

        for card in load_playbook():
            self.assertTrue(card.insufficient_when, f"{card.id} must say when evidence is insufficient")


if __name__ == "__main__":
    unittest.main()
