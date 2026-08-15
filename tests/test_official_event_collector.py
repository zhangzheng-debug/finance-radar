from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from event_ledger import open_ledger
import official_event_collector as collector


RSS = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel><item>
<title>Federal Reserve announces interest rate decision</title>
<link>https://www.federalreserve.gov/example.htm</link>
<guid>fed-example</guid><description>Federal Reserve rate decision</description>
<category>Monetary Policy</category><pubDate>Wed, 15 Jul 2026 18:00:00 GMT</pubDate>
</item></channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>8-K - Example Corp (0001234567) (Filer)</title>
    <id>urn:accession:one</id><updated>2026-07-15T16:00:00-04:00</updated>
    <link rel="alternate" href="https://www.sec.gov/Archives/example-index.htm" />
    <summary type="html">&lt;b&gt;Filed:&lt;/b&gt; 2026-07-15 &lt;br&gt;Item 2.02: Results</summary>
  </entry>
  <entry><title>424B2 - Note Issuer (0007654321) (Filer)</title>
    <id>urn:accession:two</id><updated>2026-07-15T16:01:00-04:00</updated>
    <link rel="alternate" href="https://www.sec.gov/Archives/note-index.htm" />
    <summary type="html">Filed 2026-07-15</summary>
  </entry>
</feed>"""


class OfficialEventCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = open_ledger(Path(self.temp_dir.name) / "ledger.sqlite3")

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_rss_capture_is_idempotent_and_uses_http_cursor(self) -> None:
        calls: list[dict[str, str]] = []

        def fetcher(url: str, headers: dict[str, str], timeout: float, data: bytes | None):
            calls.append(headers)
            if len(calls) == 2:
                return collector.HttpResult(304, b"", {"etag": '"v1"'})
            return collector.HttpResult(200, RSS, {"etag": '"v1"'})

        first = collector.collect_feed(
            self.connection,
            collector.FED_FEED,
            user_agent="test",
            fetcher=fetcher,
            force=True,
        )
        second = collector.collect_feed(
            self.connection,
            collector.FED_FEED,
            user_agent="test",
            fetcher=fetcher,
            force=True,
        )
        self.assertEqual(first["items"], 1)
        self.assertEqual(first["new_revisions"], 1)
        self.assertEqual(first["jobs"], 1)
        self.assertEqual(second["not_modified"], 1)
        self.assertEqual(calls[1]["If-None-Match"], '"v1"')
        cursor = self.connection.execute("SELECT * FROM source_cursors").fetchone()
        self.assertEqual(cursor["status"], "NOT_MODIFIED")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM source_revisions").fetchone()[0], 1
        )

    def test_rss_repairs_bare_ampersand_without_failing_source(self) -> None:
        malformed = RSS.replace(
            b"Federal Reserve announces interest rate decision",
            b"Federal Reserve & markets announce interest rate decision",
        )

        def fetcher(url: str, headers: dict[str, str], timeout: float, data: bytes | None):
            return collector.HttpResult(200, malformed, {})

        result = collector.collect_feed(
            self.connection,
            collector.FED_FEED,
            user_agent="test",
            fetcher=fetcher,
            force=True,
        )
        self.assertEqual(result["xml_repaired"], 1)
        self.assertEqual(result["items"], 1)
        observation = self.connection.execute("SELECT title FROM raw_observations").fetchone()
        self.assertIn("& markets", observation["title"])
        cursor = self.connection.execute("SELECT status FROM source_cursors").fetchone()
        self.assertEqual(cursor["status"], "SUCCESS")

    def test_sec_atom_filters_noise_and_preserves_form_metadata(self) -> None:
        def fetcher(url: str, headers: dict[str, str], timeout: float, data: bytes | None):
            return collector.HttpResult(200, ATOM, {})

        result = collector.collect_feed(
            self.connection,
            collector.SEC_FEED,
            user_agent="Example test@example.com",
            fetcher=fetcher,
            force=True,
        )
        self.assertEqual(result["items"], 1)
        self.assertEqual(result["filtered"], 1)
        raw = self.connection.execute("SELECT raw_json FROM raw_observations").fetchone()
        item = json.loads(raw["raw_json"])["item"]
        self.assertEqual(item["form"], "8-K")
        self.assertEqual(item["company"], "Example Corp")
        self.assertEqual(item["cik"], "0001234567")
        self.assertEqual(item["items"], ["2.02"])

    def test_additional_official_feed_registry_covers_high_value_event_families(self) -> None:
        source_ids = {spec.source_id for spec in collector.ADDITIONAL_OFFICIAL_FEEDS}
        self.assertEqual(
            source_ids,
            {
                "cftc_enforcement",
                "fda_medwatch",
                "ftc_press",
                "sec_litigation_releases",
                "sec_trading_suspensions",
                "fdic_press_releases",
                "nvidia_official_news",
                "ecb_press",
                "ecb_statistical_press",
                "eia_press",
            },
        )
        self.assertTrue(all(spec.format == "rss" for spec in collector.ADDITIONAL_OFFICIAL_FEEDS))
        self.assertTrue(all(spec.priority >= 82 for spec in collector.ADDITIONAL_OFFICIAL_FEEDS))
        self.assertTrue(
            all(spec.max_entry_age_days is not None for spec in collector.ADDITIONAL_OFFICIAL_FEEDS)
        )
        self.assertEqual(collector.NVIDIA_OFFICIAL_FEED.authority_tier, "P1_issuer_official")
        self.assertEqual(collector.ECB_PRESS_FEED.authority_tier, "P0_official")

    def test_feed_preserves_per_source_authority_tier_in_registry_and_job(self) -> None:
        def fetcher(url: str, headers: dict[str, str], timeout: float, data: bytes | None):
            return collector.HttpResult(200, RSS, {})

        spec = collector.FeedSpec(
            source_id="issuer_press_test",
            name="Issuer press test",
            url="https://example.test/rss",
            format="rss",
            priority=80,
            min_interval_seconds=0,
            max_entry_age_days=30,
            source_type="issuer_official_feed",
            authority_tier="P1_issuer_official",
        )
        collector.collect_feed(
            self.connection,
            spec,
            user_agent="test",
            fetcher=fetcher,
            force=True,
            now=dt.datetime(2026, 7, 16, tzinfo=dt.timezone.utc),
        )
        source = self.connection.execute(
            "SELECT source_type,authority_tier FROM sources WHERE source_id=?",
            (spec.source_id,),
        ).fetchone()
        job = self.connection.execute(
            "SELECT payload_json FROM observation_jobs WHERE job_type='extract_live_event_candidate'"
        ).fetchone()
        self.assertEqual(source["source_type"], "issuer_official_feed")
        self.assertEqual(source["authority_tier"], "P1_issuer_official")
        self.assertEqual(json.loads(job["payload_json"])["authority_tier"], "P1_issuer_official")

    def test_entry_age_guard_uses_an_injected_clock_at_the_boundary(self) -> None:
        published = "2026-07-15T18:00:00+00:00"
        assert collector.entry_is_recent(
            published,
            max_age_days=30,
            now=dt.datetime(2026, 8, 14, 18, 0, tzinfo=dt.timezone.utc),
        )
        assert not collector.entry_is_recent(
            published,
            max_age_days=30,
            now=dt.datetime(2026, 8, 14, 18, 0, 1, tzinfo=dt.timezone.utc),
        )

    def test_feed_age_guard_filters_bootstrap_history(self) -> None:
        stale_rss = b"""<?xml version=\"1.0\"?><rss><channel><item>
        <title>Stale enforcement action</title><link>https://example.test/stale</link>
        <guid>stale</guid><pubDate>Wed, 15 Jul 2020 18:00:00 GMT</pubDate>
        </item></channel></rss>"""

        def fetcher(url: str, headers: dict[str, str], timeout: float, data: bytes | None):
            return collector.HttpResult(200, stale_rss, {})

        spec = collector.FeedSpec(
            source_id="age_guard_test",
            name="Age guard",
            url="https://example.test/rss",
            format="rss",
            priority=90,
            min_interval_seconds=0,
            max_entry_age_days=30,
        )
        result = collector.collect_feed(
            self.connection,
            spec,
            user_agent="test",
            fetcher=fetcher,
            force=True,
        )
        self.assertEqual(result["items"], 0)
        self.assertEqual(result["filtered"], 1)
        self.assertEqual(result["jobs"], 0)

    def test_bls_groups_series_by_release_and_period(self) -> None:
        payload = {
            "status": "REQUEST_SUCCEEDED",
            "message": [],
            "Results": {
                "series": [
                    {"seriesID": "CUUR0000SA0", "data": [{"year": "2026", "period": "M06", "periodName": "June", "value": "333.952"}, {"year": "2026", "period": "M05", "periodName": "May", "value": "335.123"}, {"year": "2025", "period": "M06", "periodName": "June", "value": "322.561"}]},
                    {"seriesID": "CUSR0000SA0", "data": [{"year": "2026", "period": "M06", "periodName": "June", "value": "332.568"}, {"year": "2026", "period": "M05", "periodName": "May", "value": "333.979"}]},
                    {"seriesID": "CUUR0000SA0L1E", "data": [{"year": "2026", "period": "M06", "periodName": "June", "value": "336.882"}, {"year": "2026", "period": "M05", "periodName": "May", "value": "336.846"}, {"year": "2025", "period": "M06", "periodName": "June", "value": "326.430"}]},
                    {"seriesID": "CUSR0000SA0L1E", "data": [{"year": "2026", "period": "M06", "periodName": "June", "value": "336.065"}, {"year": "2026", "period": "M05", "periodName": "May", "value": "336.121"}]},
                    {"seriesID": "WPUFD4", "data": [{"year": "2026", "period": "M06", "periodName": "June", "value": "157.045"}, {"year": "2026", "period": "M05", "periodName": "May", "value": "157.346"}, {"year": "2025", "period": "M06", "periodName": "June", "value": "148.862"}]},
                    {"seriesID": "WPSFD4", "data": [{"year": "2026", "period": "M06", "periodName": "June", "value": "156.566"}, {"year": "2026", "period": "M05", "periodName": "May", "value": "157.001"}]},
                    {"seriesID": "CES0000000001", "data": [{"year": "2026", "period": "M06", "periodName": "June", "value": "158984"}]},
                    {"seriesID": "LNS14000000", "data": [{"year": "2026", "period": "M06", "periodName": "June", "value": "4.2"}]},
                    {"seriesID": "JTS000000000000000JOL", "data": [{"year": "2026", "period": "M05", "periodName": "May", "value": "7594"}]},
                ]
            },
        }
        requests: list[bytes | None] = []

        def fetcher(url: str, headers: dict[str, str], timeout: float, data: bytes | None):
            requests.append(data)
            return collector.HttpResult(200, json.dumps(payload).encode(), {})

        first = collector.collect_bls(
            self.connection, fetcher=fetcher, force=True, min_interval_seconds=0
        )
        second = collector.collect_bls(
            self.connection, fetcher=fetcher, force=True, min_interval_seconds=0
        )
        self.assertEqual(first["items"], 4)
        self.assertEqual(first["jobs"], 4)
        self.assertEqual(second["new_revisions"], 0)
        self.assertEqual(second["jobs"], 0)
        self.assertEqual(len(requests), 2)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM raw_observations").fetchone()[0], 4
        )
        cpi = self.connection.execute(
            "SELECT raw_json FROM raw_observations WHERE external_id='consumer_price_index:2026:M06'"
        ).fetchone()
        cpi_item = json.loads(cpi["raw_json"])["item"]
        self.assertEqual(cpi_item["derived_metrics"]["all_items_monthly_sa_pct"], -0.4)
        self.assertEqual(cpi_item["derived_metrics"]["all_items_12m_unadjusted_pct"], 3.5)
        self.assertEqual(cpi_item["source_publication_timestamp"], None)
        self.assertEqual(
            self.connection.execute(
                "SELECT source_published_at FROM raw_observations WHERE external_id='consumer_price_index:2026:M06'"
            ).fetchone()[0],
            None,
        )
        self.assertEqual(
            cpi_item["market_expectation_status"],
            "N/A_no_free_official_consensus_source",
        )


if __name__ == "__main__":
    unittest.main()
