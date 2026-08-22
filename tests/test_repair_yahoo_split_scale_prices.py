from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import repair_yahoo_split_scale_prices as repair


class YahooSplitScaleRepairTests(unittest.TestCase):
    def test_alternating_reverse_split_units_are_normalized(self) -> None:
        raw = [0.60, 18.0, 0.55, 12.0]
        path = repair.choose_scale_path(raw, [1 / 30, 1.0, 30.0])
        corrected = [value * multiplier for value, multiplier in zip(raw, path)]
        self.assertEqual(path[-1], 1.0)
        self.assertLess(repair.max_step_ratio(
            [{"p": value} for value in corrected], "p"
        ), 2.0)
        self.assertAlmostEqual(corrected[0], 18.0)
        self.assertAlmostEqual(corrected[1], 18.0)

    def test_ticker_without_split_metadata_is_not_changed(self) -> None:
        rows = [
            {"ticker": "AAA", "date": "2026-01-01", "adj_close": "10", "close": "10"},
            {"ticker": "AAA", "date": "2026-01-02", "adj_close": "11", "close": "11"},
        ]
        repaired, summary = repair.repair_rows(rows, {})
        self.assertEqual([row["adj_close"] for row in repaired], [10.0, 11.0])
        self.assertTrue(all(row["repair_multiplier"] == 1.0 for row in repaired))
        self.assertEqual(summary[0]["repair_status"], "UNCHANGED_NO_SPLIT_METADATA")


if __name__ == "__main__":
    unittest.main()
