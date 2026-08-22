from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_human_gold_web_price_maturity as maturity


class WebPriceMaturityTests(unittest.TestCase):
    def test_assess_row_counts_trading_sessions_without_returns(self) -> None:
        readiness = {
            "sample_id": "S1",
            "event_id": "E1",
            "event_date": "2026-01-05",
            "mapping_status": "MAPPED",
            "ticker_at_event": "AAA",
        }
        days = {
            "AAA": [
                maturity.date(2026, 1, 2),
                maturity.date(2026, 1, 5),
                maturity.date(2026, 1, 6),
                maturity.date(2026, 1, 7),
            ]
        }
        row = maturity.assess_row(readiness, days, {"AAA": "AAA"}, {})
        self.assertEqual(row["available_post_event_sessions"], 2)
        self.assertEqual(row["maturity_1d"], "MATURED")
        self.assertEqual(row["maturity_5d"], "RIGHT_CENSORED")
        self.assertEqual(row["post_event_returns_included"], "false")
        self.assertNotIn("return", row)

    def test_terminal_event_is_not_treated_as_zero_return(self) -> None:
        readiness = {
            "event_date": "2026-01-05",
            "mapping_status": "MAPPED",
            "ticker_at_event": "AAA",
        }
        terminal = {("AAA", "2026-01-05"): {"status": "EQUITY_CANCELLED"}}
        row = maturity.assess_row(readiness, {}, {}, terminal)
        self.assertEqual(row["window_status"], "EQUITY_CANCELLED")
        self.assertEqual(row["maturity_1d"], "TERMINAL_SECURITY_EVENT")


if __name__ == "__main__":
    unittest.main()
