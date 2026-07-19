from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

import polars as pl


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_active_event_market_outcomes as outcomes


class ActiveEventMarketOutcomeTests(unittest.TestCase):
    def test_outcomes_are_audit_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.csv"
            with queue.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["event_candidate_id"])
                writer.writeheader()
                writer.writerow({"event_candidate_id": "C1"})
            row = {
                "event_id": "C1",
                "stable_id": "permaticker:1",
                "ticker_at_event": "AAA",
                "event_date": "2026-01-01",
                "event_trade_date": "2026-01-02",
                "benchmark_ticker": "SPY",
                "delist_within_1y": False,
                **{metric: None for metric in outcomes.NUMERIC_METRICS},
            }
            row["ret_1d"] = -0.25
            parquet = root / "returns.parquet"
            pl.DataFrame([row]).write_parquet(parquet)
            frame, matched = outcomes.build_market_outcomes(queue, parquet)
        self.assertEqual(matched, 1)
        self.assertEqual(set(frame["metric_name"]), {"ret_1d", "delist_within_1y"})
        self.assertEqual(frame["allowed_for_discovery_rank"].unique().to_list(), ["false"])
        self.assertEqual(frame["allowed_as_model_feature"].unique().to_list(), ["false"])


if __name__ == "__main__":
    unittest.main()
