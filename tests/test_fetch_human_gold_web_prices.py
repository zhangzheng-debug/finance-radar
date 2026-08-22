from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_human_gold_web_prices as fetcher


class WebPriceFetcherTests(unittest.TestCase):
    def test_normalize_chart_requires_adjusted_close(self) -> None:
        payload = {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {
                            "currency": "USD",
                            "exchangeName": "NMS",
                            "instrumentType": "EQUITY",
                        },
                        "timestamp": [1767623400, 1767709800],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [10, 11],
                                    "high": [11, 12],
                                    "low": [9, 10],
                                    "close": [10.5, 11.5],
                                    "volume": [100, 200],
                                }
                            ],
                            "adjclose": [{"adjclose": [10.0, 11.0]}],
                        },
                    }
                ],
            }
        }
        rows = fetcher.normalize_chart(
            payload,
            ticker="AAA",
            source_symbol="AAA",
            fetched_at="2026-01-07T00:00:00+00:00",
            raw_sha256="a" * 64,
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["adj_close"], 10.0)
        self.assertEqual(rows[1]["ticker"], "AAA")

    def test_symbol_variants_are_conservative(self) -> None:
        self.assertEqual(fetcher.source_symbol_variants("BRK.B"), ["BRK.B", "BRK-B"])
        self.assertEqual(
            fetcher.source_symbol_variants("DGAC.U"), ["DGAC.U", "DGAC-UN", "DGAC-U"]
        )
        self.assertEqual(fetcher.source_symbol_variants("AAPL"), ["AAPL"])
        self.assertEqual(fetcher.source_symbol_variants("SKBL", "KAZR"), ["KAZR"])

    def test_fetch_writes_raw_response_and_hash(self) -> None:
        payload = {
            "chart": {
                "error": None,
                "result": [
                    {
                        "meta": {"currency": "USD", "exchangeName": "NMS"},
                        "timestamp": [1767623400],
                        "indicators": {
                            "quote": [{"close": [10], "open": [9], "high": [11], "low": [8], "volume": [1]}],
                            "adjclose": [{"adjclose": [10]}],
                        },
                    }
                ],
            }
        }
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        with tempfile.TemporaryDirectory() as directory, patch(
            "fetch_human_gold_web_prices.requests.get", return_value=response
        ):
            result = fetcher.fetch_ticker(
                "AAA",
                start_day=fetcher.date(2026, 1, 1),
                end_day=fetcher.date(2026, 1, 10),
                raw_dir=Path(directory),
                timeout=1,
                retries=0,
            )
            raw = Path(result["raw_path"])
            self.assertTrue(raw.is_file())
            self.assertEqual(fetcher.sha256_file(raw), result["raw_sha256"])
            self.assertEqual(result["status"], "OK")

    def test_read_csv_returns_empty_for_missing_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(fetcher.read_csv(Path(directory) / "missing.csv"), [])


if __name__ == "__main__":
    unittest.main()
