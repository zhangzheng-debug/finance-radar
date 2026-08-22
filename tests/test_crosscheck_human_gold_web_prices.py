from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import crosscheck_human_gold_web_prices as crosscheck


class WebPriceCrosscheckTests(unittest.TestCase):
    def test_compare_symbol_reports_relative_difference(self) -> None:
        response = {
            "status": "ok",
            "values": [
                {"datetime": "2026-01-05", "close": "100.0"},
                {"datetime": "2026-01-06", "close": "102.0"},
            ],
        }
        summary, rows = crosscheck.compare_symbol(
            "AAA",
            response,
            {"AAA": {"2026-01-05": 100.0, "2026-01-06": 102.01}},
        )
        self.assertEqual(summary["status"], "OK")
        self.assertEqual(summary["overlap_days"], 2)
        self.assertAlmostEqual(rows[1]["abs_relative_difference"], 0.01 / 102.0)
        self.assertEqual(summary["within_1bp_rate"], 1.0)

    def test_compare_symbol_preserves_provider_error(self) -> None:
        summary, rows = crosscheck.compare_symbol(
            "AAA", {"status": "error", "message": "not found"}, {}
        )
        self.assertEqual(summary["status"], "PROVIDER_ERROR")
        self.assertEqual(rows, [])

    def test_error_sanitizer_redacts_api_key(self) -> None:
        message = "https://example.test/?symbol=AAA&apikey=secret-value&x=1"
        sanitized = crosscheck.sanitize_error(message)
        self.assertNotIn("secret-value", sanitized)
        self.assertIn("apikey=REDACTED", sanitized)


if __name__ == "__main__":
    unittest.main()
