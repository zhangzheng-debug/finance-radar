from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from train_risk_router import build_pipeline, time_issuer_chain_split


class RiskRouterSplitTests(unittest.TestCase):
    def test_feature_ablation_variants_are_explicit(self) -> None:
        for mode in ("word_only", "char_only", "combined"):
            pipeline = build_pipeline(mode)
            self.assertEqual([name for name, _ in pipeline.steps], ["features", "classifier"])
        with self.assertRaises(ValueError):
            build_pipeline("opaque_magic_score")

    def test_split_has_zero_issuer_and_chain_overlap(self) -> None:
        labels: list[str] = []
        records: list[dict[str, str]] = []
        for index in range(40):
            labels.append("RISK_REVIEW" if index % 2 == 0 else "NON_TARGET")
            records.append(
                {
                    "event_date": f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}",
                    "issuer_key": f"issuer-{index}",
                    "chain_id": f"chain-{index // 2}" if index < 4 else "",
                }
            )
        train, test, audit = time_issuer_chain_split(labels, records)
        self.assertTrue(train)
        self.assertTrue(test)
        self.assertEqual(audit["issuer_overlap_count"], 0)
        self.assertEqual(audit["event_chain_overlap_count"], 0)
        self.assertEqual(set(train) & set(test), set())
        self.assertEqual(set(labels[index] for index in train), {"RISK_REVIEW", "NON_TARGET"})
        self.assertEqual(set(labels[index] for index in test), {"RISK_REVIEW", "NON_TARGET"})


if __name__ == "__main__":
    unittest.main()
