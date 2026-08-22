from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_short_horizon_curve as curve
from build_unlabeled_tn_audit import Price


def event(sample_id: str, ticker: str = "AAA") -> dict[str, str]:
    return {
        "sample_id": sample_id,
        "event_id": f"E-{sample_id}",
        "event_date": "2026-01-05",
        "event_family": "filing",
        "headline": "Example",
        "mapping_status": "MAPPED",
        "ticker_at_event": ticker,
    }


class ShortHorizonCurveTests(unittest.TestCase):
    def test_short_return_is_negative_long_return_for_every_day(self) -> None:
        prices = {
            "AAA": [
                Price(date(2026, 1, 5), 100.0),
                Price(date(2026, 1, 6), 90.0),
                Price(date(2026, 1, 7), 80.0),
                Price(date(2026, 1, 8), 85.0),
            ]
        }
        rows, counts = curve.build_event_daily_returns(
            [event("S1")], prices, {"AAA": "UNCHANGED"}, {}
        )
        self.assertEqual(counts["events_computable_t1"], 1)
        self.assertEqual([row["horizon_trading_days"] for row in rows], [1, 2, 3])
        self.assertAlmostEqual(rows[0]["short_gross_return"], 0.10)
        self.assertAlmostEqual(rows[1]["short_gross_return"], 0.20)
        self.assertAlmostEqual(rows[2]["short_gross_return"], 0.15)
        self.assertAlmostEqual(
            rows[1]["short_gross_return"], -rows[1]["long_total_return"]
        )

    def test_available_case_shrinks_but_fixed_cohort_is_constant(self) -> None:
        daily = [
            {
                "sample_id": "S1",
                "ticker": "AAA",
                "horizon_trading_days": 1,
                "short_gross_return": 0.10,
            },
            {
                "sample_id": "S1",
                "ticker": "AAA",
                "horizon_trading_days": 2,
                "short_gross_return": 0.20,
            },
            {
                "sample_id": "S2",
                "ticker": "BBB",
                "horizon_trading_days": 1,
                "short_gross_return": -0.10,
            },
        ]
        curves, cohort_sizes = curve.build_curves(daily)
        available = [row for row in curves if row["curve_type"] == "AVAILABLE_CASE"]
        self.assertEqual([row["n_events"] for row in available], [2, 1])
        fixed_two = [
            row
            for row in curves
            if row["curve_type"] == "FIXED_MATURITY_COHORT"
            and row["cohort_maturity_horizon"] == 2
        ]
        self.assertEqual(cohort_sizes[2], 1)
        self.assertEqual([row["n_events"] for row in fixed_two], [1, 1])

    def test_terminal_and_unmapped_events_are_excluded(self) -> None:
        mapped = event("S1")
        unmapped = event("S2")
        unmapped["mapping_status"] = "UNMAPPED"
        prices = {
            "AAA": [
                Price(date(2026, 1, 5), 100.0),
                Price(date(2026, 1, 6), 90.0),
            ]
        }
        rows, counts = curve.build_event_daily_returns(
            [mapped, unmapped],
            prices,
            {"AAA": "UNCHANGED"},
            {("AAA", "2026-01-05"): {"status": "EQUITY_CANCELLED"}},
        )
        self.assertEqual(rows, [])
        self.assertEqual(counts["events_unmapped"], 1)
        self.assertEqual(counts["events_terminal_or_complex"], 1)

    def test_primary_fixed_horizon_uses_longest_cohort_above_floor(self) -> None:
        self.assertEqual(
            curve.choose_primary_fixed_horizon({1: 500, 2: 300, 3: 39}, 40), 2
        )

    def test_stability_choice_is_modal_best_day_across_fixed_cohorts(self) -> None:
        rows = []
        for maturity, means in {3: [0.01, 0.03, 0.02], 4: [0.02, 0.04, 0.03, 0.01]}.items():
            for horizon, mean in enumerate(means, start=1):
                rows.append(
                    {
                        "curve_type": "FIXED_MATURITY_COHORT",
                        "cohort_maturity_horizon": maturity,
                        "horizon_trading_days": horizon,
                        "n_events": 100,
                        "mean_short_return": mean,
                    }
                )
        selected, votes, cohorts = curve.stable_fixed_cohort_recommendation(rows)
        self.assertEqual(selected, 2)
        self.assertEqual(votes, {2: 2})
        self.assertEqual(cohorts, 2)


if __name__ == "__main__":
    unittest.main()
