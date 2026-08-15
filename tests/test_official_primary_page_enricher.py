from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import enqueue_observation_job, open_ledger, stable_id, upsert_source, utc_now
import live_candidate_extractor as extractor
import official_primary_page_enricher as enricher


class OfficialPrimaryPageEnricherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.connection = open_ledger(self.root / "ledger.sqlite3")

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def add_candidate(self, *, source_id: str, title: str, url: str) -> str:
        upsert_source(
            self.connection,
            source_id=source_id,
            name=source_id,
            source_type="official_primary_feed",
            authority_tier="P0_official",
        )
        observation_id = stable_id("OBS", source_id, title)
        now = utc_now()
        self.connection.execute(
            """INSERT INTO raw_observations VALUES (
               ?,?,?,?,?,?,?,?,?,'{}','captured')""",
            (
                observation_id,
                source_id,
                title,
                "2026-07-15T12:00:00+00:00",
                now,
                title,
                title,
                url,
                hashlib.sha256(title.encode()).hexdigest(),
            ),
        )
        enqueue_observation_job(
            self.connection,
            observation_id=observation_id,
            job_type="extract_live_event_candidate",
            priority=90,
            payload={},
        )
        self.connection.commit()
        result = extractor.process_pending(self.connection, limit=10)
        self.assertEqual(result["candidates"], 1)
        return result["event_ids"][0]

    def test_extracts_review_only_passage_without_promoting_candidate(self) -> None:
        event_id = self.add_candidate(
            source_id="fda_medwatch",
            title="Heart Pump Recall: Example removes affected devices",
            url="https://www.fda.gov/example-recall",
        )
        body = b"""<html><body><nav>Subscribe and contact us</nav><main>
        <h1>Heart Pump Recall</h1><p>The company is recalling affected heart pump devices.</p>
        <p>The FDA said the failure may cause serious injuries or deaths and customers should stop use.</p>
        </main></body></html>"""

        def fetcher(url: str, user_agent: str, timeout: float) -> enricher.FetchResult:
            return enricher.FetchResult(body, url)

        result = enricher.enrich(
            self.connection,
            cache_dir=self.root / "cache",
            user_agent="FinanceRadar test@example.com",
            limit=10,
            timeout=1,
            max_chars=500,
            fetcher=fetcher,
        )
        self.assertEqual(result["passages"], 1)
        evidence = self.connection.execute("SELECT * FROM event_evidence").fetchone()
        self.assertIn("serious injuries or deaths", evidence["evidence_passage"])
        self.assertEqual(evidence["evidence_status"], "machine_extracted_unreviewed")
        self.assertEqual(evidence["auto_verification_allowed"], 0)
        job = self.connection.execute(
            "SELECT status FROM pipeline_jobs WHERE event_id=?", (event_id,)
        ).fetchone()
        self.assertEqual(job["status"], "PENDING_EVIDENCE_REVIEW")
        self.assertEqual(result["jobs_advanced"], 1)
        event = self.connection.execute(
            "SELECT status,label_status,manual_grade FROM canonical_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        self.assertEqual((event["status"], event["label_status"], event["manual_grade"]), ("candidate", "candidate", None))

    def test_disallowed_source_host_is_rejected(self) -> None:
        self.add_candidate(
            source_id="sec_litigation_releases",
            title="Example Defendant",
            url="https://malicious.example/redirect",
        )

        def fetcher(url: str, user_agent: str, timeout: float) -> enricher.FetchResult:
            raise AssertionError("fetcher must not be called for a disallowed host")

        result = enricher.enrich(
            self.connection,
            cache_dir=self.root / "cache",
            user_agent="FinanceRadar test@example.com",
            limit=10,
            timeout=1,
            max_chars=500,
            fetcher=fetcher,
        )
        self.assertEqual(result["inserted"], 0)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0],
            0,
        )

    def test_title_overlap_without_event_keywords_remains_link_only(self) -> None:
        self.add_candidate(
            source_id="ftc_press",
            title="FTC takes action against Example Corp",
            url="https://www.ftc.gov/example-commentary",
        )
        body = b"""<html><body><h1>FTC takes action against Example Corp</h1>
        <p>This page contains general commentary and event logistics only.</p></body></html>"""

        def fetcher(url: str, user_agent: str, timeout: float) -> enricher.FetchResult:
            return enricher.FetchResult(body, url)

        result = enricher.enrich(
            self.connection,
            cache_dir=self.root / "cache",
            user_agent="FinanceRadar test@example.com",
            limit=10,
            timeout=1,
            max_chars=500,
            fetcher=fetcher,
        )
        self.assertEqual(result["link_only"], 1)
        evidence = self.connection.execute("SELECT * FROM event_evidence").fetchone()
        self.assertEqual(evidence["evidence_passage"], "")
        self.assertEqual(evidence["evidence_status"], "link_only_no_relevant_passage")

    def test_existing_official_evidence_repairs_stale_pending_job(self) -> None:
        event_id = self.add_candidate(
            source_id="fda_medwatch",
            title="Heart Pump Recall: Example removes affected devices",
            url="https://www.fda.gov/existing-evidence",
        )
        observation_id = self.connection.execute(
            "SELECT observation_id FROM event_observations WHERE event_id=?", (event_id,)
        ).fetchone()[0]
        now = utc_now()
        self.connection.execute(
            """INSERT INTO event_evidence VALUES (
               'existing-evidence',?,?, 'https://www.fda.gov/existing-evidence',
               '2026-07-15','fda_medwatch','', 'official recall passage','recall',8,
               'machine_extracted_unreviewed',0,?,?)""",
            (event_id, observation_id, now, now),
        )
        self.connection.commit()

        advanced = enricher.advance_existing_evidence_jobs(self.connection)

        job = self.connection.execute(
            "SELECT status FROM pipeline_jobs WHERE event_id=?", (event_id,)
        ).fetchone()
        event = self.connection.execute(
            "SELECT status,label_status,manual_grade FROM canonical_events WHERE event_id=?",
            (event_id,),
        ).fetchone()
        self.assertEqual(advanced, 1)
        self.assertEqual(job["status"], "PENDING_EVIDENCE_REVIEW")
        self.assertEqual(tuple(event), ("candidate", "candidate", None))

    def test_light_followup_evidence_need_is_prioritized_without_closing_the_task(self) -> None:
        ordinary_event = self.add_candidate(
            source_id="fda_medwatch",
            title="Heart Pump Recall: Ordinary queue event",
            url="https://www.fda.gov/ordinary-queue-event",
        )
        followup_event = self.add_candidate(
            source_id="fda_medwatch",
            title="Heart Pump Recall: Evidence follow-up event",
            url="https://www.fda.gov/evidence-followup-event",
        )
        now = utc_now()
        # Legacy reconciliation may reopen a weak/verified v1 record without
        # changing its canonical status.  Its explicitly queued follow-up must
        # still get bounded official-source evidence collection.
        self.connection.execute(
            "UPDATE canonical_events SET status='weak',label_status='weak' WHERE event_id=?",
            (followup_event,),
        )
        self.connection.execute(
            """INSERT INTO pipeline_jobs VALUES (
               'light-followup-priority',?,'light_verification_followup','PENDING_EVIDENCE_REVIEW',
               95,0,?,NULL,'{}',?,?)""",
            (followup_event, now, now, now),
        )
        self.connection.commit()

        rows = enricher.pending_rows(self.connection, limit=2)
        self.assertEqual(rows[0]["event_id"], followup_event)
        self.assertEqual(rows[1]["event_id"], ordinary_event)

        def fetcher(url: str, user_agent: str, timeout: float) -> enricher.FetchResult:
            return enricher.FetchResult(
                b"<html><body><p>The company is recalling affected heart pump devices after a failure that may cause serious injuries.</p></body></html>",
                url,
            )

        result = enricher.enrich(
            self.connection,
            cache_dir=self.root / "cache",
            user_agent="FinanceRadar test@example.com",
            limit=1,
            timeout=1,
            max_chars=500,
            fetcher=fetcher,
        )
        followup = self.connection.execute(
            "SELECT status FROM pipeline_jobs WHERE job_id='light-followup-priority'"
        ).fetchone()
        canonical = self.connection.execute(
            "SELECT status,label_status FROM canonical_events WHERE event_id=?",
            (followup_event,),
        ).fetchone()
        self.assertEqual(result["light_followup_selected"], 1)
        self.assertEqual(followup["status"], "PENDING_EVIDENCE_REVIEW")
        self.assertEqual(tuple(canonical), ("weak", "weak"))

    def test_extended_official_hosts_require_https(self) -> None:
        self.assertTrue(
            enricher.host_allowed(
                "ecb_press", "https://www.ecb.europa.eu/press/pr/date/2026/html/example.en.html"
            )
        )
        self.assertTrue(
            enricher.host_allowed(
                "federal_reserve_press", "https://www.federalreserve.gov/newsevents/example.htm"
            )
        )
        self.assertFalse(
            enricher.host_allowed(
                "ecb_press", "http://www.ecb.europa.eu/press/pr/date/2026/html/example.en.html"
            )
        )
        self.assertEqual(
            enricher.canonical_official_url(
                "fda_medwatch", "http://www.fda.gov/example-recall?lot=1#notice"
            ),
            "https://www.fda.gov/example-recall?lot=1",
        )
        self.assertIsNone(
            enricher.canonical_official_url(
                "fda_medwatch", "http://user@www.fda.gov/example-recall"
            )
        )
        self.assertIsNone(
            enricher.canonical_official_url(
                "fda_medwatch", "http://www.fda.gov:8080/example-recall"
            )
        )

    def test_registered_http_url_is_upgraded_before_fetch(self) -> None:
        self.add_candidate(
            source_id="fda_medwatch",
            title="Heart Pump Recall: Example removes affected devices",
            url="http://www.fda.gov/example-recall",
        )
        fetched_urls: list[str] = []

        def fetcher(url: str, user_agent: str, timeout: float) -> enricher.FetchResult:
            fetched_urls.append(url)
            return enricher.FetchResult(
                b"<html><body><p>The recall may cause serious injuries or deaths.</p></body></html>",
                url,
            )

        result = enricher.enrich(
            self.connection,
            cache_dir=self.root / "cache",
            user_agent="FinanceRadar test@example.com",
            limit=10,
            timeout=1,
            max_chars=500,
            fetcher=fetcher,
        )

        self.assertEqual(fetched_urls, ["https://www.fda.gov/example-recall"])
        self.assertEqual(result["http_upgraded_to_https"], 1)
        self.assertEqual(result["passages"], 1)

    def test_material_facts_outrank_keyword_heavy_page_title(self) -> None:
        self.add_candidate(
            source_id="fda_medwatch",
            title="Heart Pump Recall: Example removes affected devices",
            url="https://www.fda.gov/factual-recall",
        )
        body = b"""<html><body>
        <h1>Heart Pump Recall: Example removes affected devices</h1>
        <p>The company issued a letter requiring customers to remove 12,400 affected devices.
        The malfunction may cause serious injuries or deaths, and the FDA reported seven injuries.</p>
        </body></html>"""

        def fetcher(url: str, user_agent: str, timeout: float) -> enricher.FetchResult:
            return enricher.FetchResult(body, url)

        result = enricher.enrich(
            self.connection,
            cache_dir=self.root / "cache",
            user_agent="FinanceRadar test@example.com",
            limit=10,
            timeout=1,
            max_chars=500,
            fetcher=fetcher,
        )
        evidence = self.connection.execute("SELECT * FROM event_evidence").fetchone()
        self.assertEqual(result["passages"], 1)
        self.assertIn("12,400 affected devices", evidence["evidence_passage"])
        self.assertIn("seven injuries", evidence["evidence_passage"])

    def test_pdf_payload_uses_pdf_text_extraction(self) -> None:
        pages = [
            mock.Mock(
                extract_text=mock.Mock(
                    return_value="SEC order: potential manipulation through social media. Trading is suspended."
                )
            )
        ]
        with mock.patch.object(enricher, "PdfReader", return_value=mock.Mock(pages=pages)):
            text = enricher.document_text(b"%PDF-test", "https://www.sec.gov/order.pdf")
        self.assertIn("potential manipulation", text)
        self.assertIn("Trading is suspended", text)

    def test_pdf_visual_line_wraps_do_not_split_material_fact(self) -> None:
        pages = [
            mock.Mock(
                extract_text=mock.Mock(
                    return_value=(
                        "Trading was suspended because of potential manipulation in the\n"
                        "securities through recommendations by unknown persons via social media,\n"
                        "which appear designed to artificially inflate price and volume."
                    )
                )
            )
        ]
        with mock.patch.object(enricher, "PdfReader", return_value=mock.Mock(pages=pages)):
            text = enricher.document_text(b"%PDF-test", "https://www.sec.gov/order.pdf")
        passage = enricher.select_passage(
            text,
            title="Trading Suspensions: Example",
            event_type="trading_suspension",
            max_chars=500,
        )
        self.assertIn("unknown persons via social media", passage.text)
        self.assertIn("artificially inflate", passage.text)


if __name__ == "__main__":
    unittest.main()
