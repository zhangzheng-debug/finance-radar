from __future__ import annotations

import datetime as dt
import csv
import sys
import unittest
import urllib.error
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import active_sec_evidence as evidence


class ActiveSecEvidenceTests(unittest.TestCase):
    def test_sec_client_retries_transient_transport_error(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self) -> bytes:
                return b'{"filings": {"recent": {}}}'

        failures = [
            urllib.error.URLError("temporary TLS EOF"),
            Response(),
        ]
        with TemporaryDirectory() as directory:
            client = evidence.SecClient(
                "Research Bot test@example.com", Path(directory), min_interval=0
            )
            with patch.object(evidence.urllib.request, "urlopen", side_effect=failures) as mocked:
                with patch.object(evidence.time, "sleep"):
                    payload = client._get_json(
                        "https://data.sec.gov/submissions/CIK0000000001.json",
                        Path(directory) / "CIK0000000001.json",
                    )
        self.assertEqual(payload, {"filings": {"recent": {}}})
        self.assertEqual(mocked.call_count, 2)

    def test_load_queue_can_target_event_beyond_top_n(self) -> None:
        columns = ["event_candidate_id", "cik"]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "queue.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows(
                    [
                        {"event_candidate_id": "C1", "cik": "1"},
                        {"event_candidate_id": "C99", "cik": "99"},
                    ]
                )
            rows = evidence.load_queue(path, 1, {"C99"})
        self.assertEqual([row["event_candidate_id"] for row in rows], ["C99"])

    def test_rows_from_recent_and_url_construction(self) -> None:
        rows = evidence.rows_from_recent(
            {
                "accessionNumber": ["0001234567-26-000001"],
                "filingDate": ["2026-05-20"],
                "form": ["8-K"],
                "primaryDocument": ["event.htm"],
            }
        )
        self.assertEqual(rows[0]["form"], "8-K")
        index_url, document_url = evidence.filing_urls(
            "0001234567", "0001234567-26-000001", "event.htm"
        )
        self.assertIn("000123456726000001", index_url)
        self.assertTrue(document_url.endswith("/event.htm"))

    def test_select_filings_prefers_relevant_nearby_form(self) -> None:
        filings = [
            {
                "accessionNumber": "1",
                "filingDate": "2026-05-20",
                "form": "8-K",
                "primaryDocument": "a.htm",
            },
            {
                "accessionNumber": "2",
                "filingDate": "2026-05-21",
                "form": "4",
                "primaryDocument": "b.htm",
            },
            {
                "accessionNumber": "3",
                "filingDate": "2025-01-01",
                "form": "10-K",
                "primaryDocument": "c.htm",
            },
        ]
        selected = evidence.select_filings(
            filings,
            event_date=dt.date(2026, 5, 20),
            event_type="bankruptcy_liquidation",
            before_days=10,
            after_days=45,
            limit=5,
        )
        self.assertEqual([row["accessionNumber"] for row in selected], ["1"])

    def test_form_item_hint_identifies_bankruptcy_and_delisting(self) -> None:
        bonus, hint = evidence.item_match_hint("bankruptcy_liquidation", "8-K", "1.03,2.04")
        self.assertEqual(bonus, 60)
        self.assertIn("bankruptcy", hint)
        bonus, hint = evidence.item_match_hint("delisted", "25-NSE", "")
        self.assertEqual(bonus, 55)
        self.assertIn("termination", hint)

    def test_reverse_split_includes_recent_financing_forms_and_wider_lookback(self) -> None:
        filings = [
            {
                "accessionNumber": "atm",
                "filingDate": "2025-12-08",
                "form": "424B5",
                "primaryDocument": "atm.htm",
            }
        ]
        selected = evidence.select_filings(
            filings,
            event_date=dt.date(2026, 1, 26),
            event_type="reverse_split",
            before_days=10,
            after_days=45,
            limit=5,
        )
        self.assertEqual([row["accessionNumber"] for row in selected], ["atm"])
        self.assertEqual(
            selected[0]["form_item_match_hint"],
            "possible_financing_context_for_reverse_split",
        )

    def test_recent_submission_window_avoids_unneeded_old_file_fetch(self) -> None:
        filings = [
            {"filingDate": "2025-01-01"},
            {"filingDate": "2026-03-01"},
        ]
        self.assertTrue(
            evidence.recent_submissions_cover_window(filings, dt.date(2026, 1, 1))
        )
        self.assertFalse(
            evidence.recent_submissions_cover_window(filings, dt.date(2024, 12, 31))
        )

    def test_event_lookback_days_tracks_semantic_windows(self) -> None:
        self.assertEqual(evidence.event_lookback_days("delisted", "25-NSE", 10), 45)
        self.assertEqual(evidence.event_lookback_days("reverse_split", "424B5", 10), 60)
        self.assertEqual(evidence.event_lookback_days("one_day_crash", "8-K", 10), 90)
        self.assertEqual(evidence.event_lookback_days("negative_equity", "10-Q", 10), 10)

    def test_delisting_includes_earlier_cause_announcement(self) -> None:
        filings = [
            {
                "accessionNumber": "cause",
                "filingDate": "2025-02-18",
                "form": "8-K",
                "items": "8.01,9.01",
                "primaryDocument": "cause.htm",
            },
            {
                "accessionNumber": "form25",
                "filingDate": "2025-03-07",
                "form": "25",
                "primaryDocument": "form25.htm",
            },
        ]
        selected = evidence.select_filings(
            filings,
            event_date=dt.date(2025, 3, 7),
            event_type="voluntarydelisting",
            before_days=10,
            after_days=45,
            limit=5,
        )
        self.assertEqual(
            {row["accessionNumber"] for row in selected}, {"cause", "form25"}
        )
        self.assertEqual(
            next(row for row in selected if row["accessionNumber"] == "cause")[
                "days_from_event"
            ],
            -17,
        )

    def test_price_crash_prefers_recent_pre_event_financial_warning(self) -> None:
        filings = [
            {
                "accessionNumber": "prior",
                "filingDate": "2026-01-05",
                "form": "NT 10-Q",
                "primaryDocument": "warning.htm",
            },
            {
                "accessionNumber": "later",
                "filingDate": "2026-02-18",
                "form": "8-K",
                "primaryDocument": "later.htm",
            },
        ]
        selected = evidence.select_filings(
            filings,
            event_date=dt.date(2026, 1, 31),
            event_type="one_day_crash",
            before_days=10,
            after_days=45,
            limit=2,
        )
        self.assertEqual([row["accessionNumber"] for row in selected], ["prior", "later"])
        self.assertEqual(selected[0]["days_from_event"], -26)

    def test_price_crash_surfaces_older_bankruptcy_item_in_same_crisis_chain(self) -> None:
        filings = [
            {
                "accessionNumber": "bankruptcy",
                "filingDate": "2025-11-16",
                "form": "8-K",
                "items": "1.03,2.04",
                "primaryDocument": "bankruptcy.htm",
            },
            {
                "accessionNumber": "nearby",
                "filingDate": "2026-02-02",
                "form": "8-K",
                "items": "5.02",
                "primaryDocument": "nearby.htm",
            },
        ]
        selected = evidence.select_filings(
            filings,
            event_date=dt.date(2026, 1, 31),
            event_type="one_day_crash",
            before_days=10,
            after_days=45,
            limit=2,
        )
        self.assertEqual(selected[0]["accessionNumber"], "bankruptcy")
        self.assertIn("8-K_1.03", selected[0]["form_item_match_hint"])

    def test_cik_validation(self) -> None:
        self.assertEqual(evidence.normalize_cik("1817511"), "0001817511")
        with self.assertRaises(ValueError):
            evidence.normalize_cik("not-a-cik")


if __name__ == "__main__":
    unittest.main()
