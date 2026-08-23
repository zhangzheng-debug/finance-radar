from __future__ import annotations

import csv
import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_human_gold_sharadar_outcomes as audit


TICKER_FIELDS = [
    "table",
    "permaticker",
    "ticker",
    "name",
    "exchange",
    "isdelisted",
    "category",
    "firstpricedate",
    "lastpricedate",
    "secfilings",
]


def write_csv_zip(path: Path, member: str, fields: list[str], rows: list[dict]) -> None:
    text_path = path.with_suffix(".csv")
    with text_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(text_path, member)
    text_path.unlink()


class HumanGoldSharadarOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.package = self.root / "package.zip"
        self.tickers = self.root / "tickers.zip"
        self.sep = self.root / "sep.zip"
        self.output = self.root / "output"
        self.samples = [
            {
                "sample_id": "S1",
                "event_id": "E1",
                "event_family": "earnings",
                "source_id": "sec_current_filings",
                "content": {
                    "event_date": "2026-01-05",
                    "headline": "8-K - Alpha Inc (0000000001) (Filer)",
                },
            },
            {
                "sample_id": "S2",
                "event_id": "E2",
                "event_family": "governance",
                "source_id": "sec_current_filings",
                "content": {
                    "event_date": "2026-02-01",
                    "headline": "8-K - Beta Inc (0000000002) (Filer)",
                },
            },
            {
                "sample_id": "S3",
                "event_id": "E3",
                "event_family": "macro_data",
                "source_id": "bls_key_indicators",
                "content": {"event_date": "2026-01-05", "headline": "Monthly release"},
            },
        ]
        manifest = {"batch_id": "B1", "samples": self.samples}
        with zipfile.ZipFile(self.package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("owner/owner_manifest.json", json.dumps(manifest))
        ticker_rows = [
            {
                "table": "SEP",
                "permaticker": "1",
                "ticker": "AAA",
                "name": "ALPHA INC",
                "exchange": "NYSE",
                "isdelisted": "N",
                "category": "Domestic Common Stock Primary Class",
                "firstpricedate": "2020-01-01",
                "lastpricedate": "2026-01-09",
                "secfilings": "https://sec.test/?CIK=0000000001",
            },
            {
                "table": "SEP",
                "permaticker": "2",
                "ticker": "BBB",
                "name": "BETA INC",
                "exchange": "NASDAQ",
                "isdelisted": "N",
                "category": "Domestic Common Stock",
                "firstpricedate": "2020-01-01",
                "lastpricedate": "2026-01-09",
                "secfilings": "https://sec.test/?CIK=0000000002",
            },
        ]
        write_csv_zip(self.tickers, "SHARADAR_TICKERS.csv", TICKER_FIELDS, ticker_rows)
        sep_rows = []
        days = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]
        for ticker, closes in (("AAA", [9, 10, 11, 12, 13, 14]), ("SPY", [99, 100, 102, 103, 104, 105])):
            for day, close in zip(days, closes):
                sep_rows.append({"ticker": ticker, "date": day, "closeadj": close})
        write_csv_zip(self.sep, "SHARADAR_SEP.csv", ["ticker", "date", "closeadj"], sep_rows)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def args(self, *, mode: str = "readiness", frozen_gold: Path | None = None) -> Namespace:
        return Namespace(
            package=self.package,
            tickers_zip=self.tickers,
            sep_zip=self.sep,
            output_dir=self.output,
            mode=mode,
            frozen_gold=frozen_gold,
            web_prices=None,
            terminal_events=None,
        )

    def freeze(self) -> Path:
        path = self.root / "frozen.jsonl"
        rows = []
        for sample in self.samples:
            rows.append(
                {
                    **sample,
                    "label": "RISK_REVIEW" if sample["sample_id"] == "S1" else "NON_TARGET",
                    "split": "HUMAN_BLIND",
                    "content": {
                        **sample["content"],
                        "post_event_market_data_included": False,
                        "model_output_included": False,
                    },
                }
            )
        text = "".join(json.dumps(row) + "\n" for row in rows)
        path.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_suffix(path.suffix + ".sha256").write_text(
            f"{digest}  {path.name}\n", encoding="utf-8"
        )
        return path

    def test_readiness_separates_mapping_from_price_maturity(self) -> None:
        result = audit.run(self.args())
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["market_data_cutoff"], "2026-01-09")
        self.assertEqual(result["mapping_counts"], {"MAPPED": 2, "NO_FILER_CIK": 1})
        with (self.output / "human_gold_720_sharadar_readiness.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["data_maturity_status"], "EVENT_DAY_WITHIN_DATASET")
        self.assertEqual(rows[1]["data_maturity_status"], "WAITING_FOR_EVENT_DAY_PRICE")
        self.assertEqual(rows[2]["data_maturity_status"], "NOT_MAPPABLE")

    def test_outcomes_require_frozen_gold_and_hash(self) -> None:
        with self.assertRaisesRegex(ValueError, "--frozen-gold"):
            audit.run(self.args(mode="outcomes"))
        frozen = self.freeze()
        frozen.with_suffix(frozen.suffix + ".sha256").unlink()
        with self.assertRaisesRegex(ValueError, "sidecar"):
            audit.run(self.args(mode="outcomes", frozen_gold=frozen))

    def test_outcomes_use_trading_days_and_keep_censoring_missing(self) -> None:
        result = audit.run(self.args(mode="outcomes", frozen_gold=self.freeze()))
        self.assertEqual(result["outcome_status_counts"]["HAS_MATURED_HORIZON"], 1)
        with (self.output / "human_gold_720_sharadar_outcomes.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            rows = {row["sample_id"]: row for row in csv.DictReader(handle)}
        alpha = rows["S1"]
        self.assertAlmostEqual(float(alpha["ret_1d"]), 0.1)
        self.assertAlmostEqual(float(alpha["market_adj_ret_1d"]), 0.08)
        self.assertEqual(alpha["maturity_5d"], "RIGHT_CENSORED")
        self.assertEqual(alpha["allowed_as_model_feature"], "false")
        self.assertEqual(rows["S2"]["outcome_status"], "WAITING_FOR_EVENT_DAY_PRICE")
        self.assertEqual(rows["S3"]["outcome_status"], "NO_UNAMBIGUOUS_SECURITY")

    def test_normalized_web_prices_use_adjusted_close(self) -> None:
        path = self.root / "web.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["ticker", "date", "adj_close"])
            writer.writeheader()
            writer.writerow({"ticker": "AAA", "date": "2026-01-05", "adj_close": "10"})
            writer.writerow({"ticker": "AAA", "date": "2026-01-06", "adj_close": "11"})
            writer.writerow({"ticker": "SPY", "date": "2026-01-06", "adj_close": "101"})
        self.assertEqual(audit.normalized_web_price_cutoff(path).isoformat(), "2026-01-06")
        prices = audit.load_normalized_web_prices(path, {"AAA"})
        self.assertEqual([row.closeadj for row in prices["AAA"]], [10.0, 11.0])


if __name__ == "__main__":
    unittest.main()
