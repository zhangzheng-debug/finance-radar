from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.models.risk_router import RiskRouter, derive_evidence_context
from app.models.semantic_policy_gate import assess_semantic_policy
from scripts.train_risk_router_v4 import DEFAULT_ARTIFACT, DEFAULT_CARD


ROOT = Path(__file__).resolve().parents[1]


class EvidenceGateTests(unittest.TestCase):
    def test_conflict_has_priority_over_accepted_evidence(self) -> None:
        context = derive_evidence_context(
            [
                {"evidence_status": "accepted_manual_primary_evidence"},
                {"evidence_status": "contradicted_by_primary"},
            ]
        )
        self.assertEqual(context["state"], "CONFLICTED")

    def test_official_machine_passage_is_explicitly_distinguished(self) -> None:
        context = derive_evidence_context(
            [
                {
                    "evidence_status": "machine_extracted_unreviewed",
                    "source_id": "sec_litigation_releases",
                    "authority_tier": "P0_OFFICIAL",
                    "evidence_passage": "The Commission filed a complaint and seeks civil penalties. " * 3,
                }
            ]
        )
        self.assertEqual(context["state"], "PRIMARY_SUPPORTED_MACHINE_OFFICIAL")

    def test_discovery_only_never_becomes_primary(self) -> None:
        context = derive_evidence_context(
            [{"evidence_status": "candidate_passage", "authority_tier": "P2_DISCOVERY"}]
        )
        self.assertEqual(context["state"], "DISCOVERY_ONLY")


class SemanticPolicyTests(unittest.TestCase):
    def test_high_precision_risk_and_non_target_rules(self) -> None:
        risk = assess_semantic_policy(
            "The issuer filed a voluntary Chapter 11 petition and continued as debtor in possession."
        )
        normal = assess_semantic_policy(
            "The Federal Reserve released minutes of the scheduled committee meeting."
        )
        paid_exit = assess_semantic_policy(
            "The ADS exited NYSE while the underlying H shares remained listed in Hong Kong."
        )
        self.assertEqual(risk.decision, "RISK_REVIEW")
        self.assertEqual(normal.decision, "NON_TARGET")
        self.assertEqual(paid_exit.decision, "NON_TARGET")


class V4ArtifactTests(unittest.TestCase):
    def test_candidate_requires_structured_evidence_and_remains_shadow(self) -> None:
        self.assertTrue(DEFAULT_ARTIFACT.is_file())
        router = RiskRouter(DEFAULT_ARTIFACT, DEFAULT_CARD)
        withheld = router.predict("The issuer filed Chapter 11.")
        admitted = router.predict(
            "The issuer filed a voluntary Chapter 11 petition.",
            evidence_context={
                "version": "test",
                "state": "PRIMARY_SUPPORTED_REVIEWED",
                "reason_codes": ["test_primary"],
                "evidence_count": 1,
            },
        )
        routine = router.predict(
            "The board released minutes of the scheduled committee meeting.",
            evidence_context={
                "version": "test",
                "state": "PRIMARY_SUPPORTED_REVIEWED",
                "reason_codes": ["test_primary"],
                "evidence_count": 1,
            },
        )
        self.assertEqual(withheld["label"], "ABSTAIN")
        self.assertEqual(withheld["runtime"], "structured_evidence_gate")
        self.assertEqual(admitted["label"], "RISK_REVIEW")
        self.assertEqual(routine["label"], "NON_TARGET")
        self.assertTrue(all(item["shadow"] and item["no_trading"] for item in (withheld, admitted, routine)))

    def test_candidate_card_records_blind_v3_qualification(self) -> None:
        card = json.loads(DEFAULT_CARD.read_text(encoding="utf-8"))
        freeze = json.loads(
            (ROOT / "artifacts" / "risk_router_external_blind_v3_freeze.json").read_text(encoding="utf-8")
        )
        self.assertEqual(card["blind_evaluation"]["freeze_id"], freeze["freeze_id"])
        self.assertTrue(card["blind_evaluation"]["gate_pass"])
        self.assertEqual(card["blind_evaluation"]["promotion_decision"], "QUALIFIED_SHADOW")


if __name__ == "__main__":
    unittest.main()
