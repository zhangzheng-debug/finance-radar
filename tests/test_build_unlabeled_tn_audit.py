from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_unlabeled_tn_audit as audit


class UnlabeledTnAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.readiness = {
            "sample_id": "S1",
            "event_id": "E1",
            "event_date": "2026-01-05",
            "event_family": "filing",
            "headline": "Example filing",
            "mapping_status": "MAPPED",
            "ticker_at_event": "AAA",
        }
        self.prices = {
            "AAA": [
                audit.Price(date(2026, 1, 2), 100.0),
                audit.Price(date(2026, 1, 5), 110.0),
                audit.Price(date(2026, 1, 6), 121.0),
            ],
            "SPY": [
                audit.Price(date(2026, 1, 2), 200.0),
                audit.Price(date(2026, 1, 5), 202.0),
                audit.Price(date(2026, 1, 6), 204.0),
            ],
        }

    def test_dual_anchor_and_exact_date_benchmark_returns(self) -> None:
        row = audit.compute_event_outcome(
            self.readiness, self.prices, {"AAA": "AAA"}, {}
        )
        self.assertEqual(row["event_trade_date"], "2026-01-05")
        self.assertEqual(row["pre_event_trade_date"], "2026-01-02")
        self.assertAlmostEqual(row["event_day_close_to_close"], 0.10)
        self.assertAlmostEqual(row["market_adj_event_day_close_to_close"], 0.09)
        self.assertAlmostEqual(row["ret_1d"], 0.10)
        self.assertAlmostEqual(row["spy_ret_1d"], 204.0 / 202.0 - 1.0)
        self.assertAlmostEqual(
            row["market_adj_ret_1d"], 0.10 - (204.0 / 202.0 - 1.0)
        )
        self.assertAlmostEqual(row["reaction_ret_1d"], 0.21)
        self.assertAlmostEqual(row["spy_reaction_ret_1d"], 0.02)
        self.assertAlmostEqual(row["market_adj_reaction_ret_1d"], 0.19)
        self.assertEqual(row["maturity_5d"], "RIGHT_CENSORED")

    def test_terminal_security_return_is_blank(self) -> None:
        terminal = {
            ("AAA", "2026-01-05"): {
                "status": "EQUITY_CANCELLED",
                "evidence_url": "https://example.test/filing",
            }
        }
        row = audit.compute_event_outcome(
            self.readiness, self.prices, {"AAA": "AAA"}, terminal
        )
        self.assertEqual(row["outcome_status"], "EQUITY_CANCELLED")
        self.assertIsNone(row["ret_1d"])
        self.assertEqual(row["maturity_1d"], "TERMINAL_SECURITY_EVENT")

    def test_summary_includes_robust_statistics(self) -> None:
        rows = [
            {
                "event_family": "filing",
                "ticker_at_event": f"T{index}",
                "market_adj_ret_1d": value,
            }
            for index, value in enumerate([-0.10, 0.0, 0.05, 0.20])
        ]
        summary = audit.aggregate_rows(rows)
        target = next(
            row
            for row in summary
            if row["group_type"] == "ALL" and row["metric"] == "market_adj_ret_1d"
        )
        self.assertEqual(target["n"], 4)
        self.assertAlmostEqual(target["median"], 0.025)
        self.assertAlmostEqual(target["positive_rate"], 0.5)
        self.assertAlmostEqual(target["p25"], -0.025)

    def test_rejects_readiness_with_gold_label_column(self) -> None:
        unsafe = dict(self.readiness)
        unsafe["label"] = "positive"
        with self.assertRaisesRegex(ValueError, "gold/reviewer fields"):
            audit.validate_unlabeled_readiness([unsafe])


if __name__ == "__main__":
    unittest.main()
