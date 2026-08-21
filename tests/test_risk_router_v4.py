from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import joblib

from app.models.risk_router import RiskRouter, derive_evidence_context
from app.models.semantic_policy_gate import assess_semantic_policy
from scripts.train_risk_router_v4 import DEFAULT_CARD


ROOT = Path(__file__).resolve().parents[1]


class DeterministicSemanticPipeline:
    """Hermetic CI fixture; production binaries are verified by recovery audit."""

    classes_ = ("NON_TARGET", "RISK_REVIEW")

    def predict_proba(self, texts: list[str]) -> list[list[float]]:
        return [[0.2, 0.8] for _ in texts]


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

    def test_dual_human_evidence_requires_strict_current_receipt(self) -> None:
        evidence = {
            "evidence_status": "accepted_dual_human_primary_evidence",
            "relation_status": "HUMAN_CONFIRMED",
            "subject_match": 1,
            "event_claim_supported": 1,
            "date_coherent": 1,
            "dual_human_receipt_consistent": 1,
        }
        supported = derive_evidence_context([evidence])
        self.assertEqual(supported["state"], "PRIMARY_SUPPORTED_REVIEWED")
        self.assertEqual(supported["reason_codes"], ["dual_human_primary_exact_passage"])

        evidence["dual_human_receipt_consistent"] = 0
        stale = derive_evidence_context([evidence])
        self.assertEqual(stale["state"], "INSUFFICIENT")


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
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "risk_router_v4_test.joblib"
            joblib.dump(
                {
                    "model_version": "risk-router-v4-hermetic-test",
                    "architecture": "structured_evidence_gate_plus_binary_semantic_router_v1",
                    "semantic_risk_threshold": 0.51,
                    "pipeline": DeterministicSemanticPipeline(),
                },
                artifact,
            )
            router = RiskRouter(artifact)
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

    def test_input_audit_hash_covers_evidence_context_and_call_counts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "risk_router_v4_test.joblib"
            joblib.dump(
                {
                    "model_version": "risk-router-v4-hermetic-test",
                    "architecture": "structured_evidence_gate_plus_binary_semantic_router_v1",
                    "semantic_risk_threshold": 0.51,
                    "pipeline": DeterministicSemanticPipeline(),
                },
                artifact,
            )
            router = RiskRouter(artifact)
            text = "The issuer disclosed a material liquidity risk in its annual filing."
            primary = router.predict(
                text,
                evidence_context={
                    "version": "test", "state": "PRIMARY_SUPPORTED_REVIEWED",
                    "reason_codes": ["exact_primary"], "evidence_count": 1,
                },
            )
            changed_context = router.predict(
                text,
                evidence_context={
                    "version": "test", "state": "PRIMARY_SUPPORTED_REVIEWED",
                    "reason_codes": ["different_primary_context"], "evidence_count": 2,
                },
            )
            withheld = router.predict(text)

        self.assertNotEqual(primary["input_sha256"], changed_context["input_sha256"])
        self.assertEqual(primary["input_contract"]["version"], "risk-router-decision-input-v2")
        self.assertIn("evidence_context_sha256", primary["input_contract"])
        self.assertEqual(primary["model_call_count"], primary["call_counts"]["trained_model_calls"])
        self.assertEqual(withheld["call_kind"], "DETERMINISTIC_EVIDENCE_GATE")
        self.assertEqual(withheld["model_call_count"], 0)
        self.assertEqual(withheld["call_counts"]["external_model_calls"], 0)


if __name__ == "__main__":
    unittest.main()
