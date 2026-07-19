from __future__ import annotations

import sys
import unittest
import urllib.error
import csv
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import extract_sec_evidence_text as extractor


class ExtractSecEvidenceTextTests(unittest.TestCase):
    def test_delisting_merger_passage_prefers_common_share_consideration_over_options(self) -> None:
        text = (
            "Each Company Option was cancelled and converted into the right to receive Option Consideration. "
            "At the effective time, each Share was cancelled and converted into the right to receive $12.00 cash, "
            "without interest, as the Merger Consideration."
        )
        result = extractor.passage_for_event(text, "delisted", 700)
        self.assertIn("each Share", result.text)
        self.assertIn("$12.00 cash", result.text)

    def test_selected_filing_rows_can_filter_specific_events(self) -> None:
        columns = [
            "queue_rank",
            "event_candidate_id",
            "evidence_relevance_score",
            "days_from_event",
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows(
                    [
                        {"queue_rank": "1", "event_candidate_id": "C1", "evidence_relevance_score": "10", "days_from_event": "0"},
                        {"queue_rank": "99", "event_candidate_id": "C99", "evidence_relevance_score": "8", "days_from_event": "1"},
                    ]
                )
            rows = extractor.selected_filing_rows(path, 1, 2, {"C99"})
        self.assertEqual([row["event_candidate_id"] for row in rows], ["C99"])

    def test_reverse_split_selection_keeps_financing_context_beyond_primary_cap(self) -> None:
        columns = [
            "queue_rank",
            "event_candidate_id",
            "event_type",
            "form",
            "evidence_relevance_score",
            "days_from_event",
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows(
                    [
                        {"queue_rank": "1", "event_candidate_id": "RS", "event_type": "reverse_split", "form": "8-K", "evidence_relevance_score": "120", "days_from_event": "0"},
                        {"queue_rank": "1", "event_candidate_id": "RS", "event_type": "reverse_split", "form": "6-K", "evidence_relevance_score": "110", "days_from_event": "1"},
                        {"queue_rank": "1", "event_candidate_id": "RS", "event_type": "reverse_split", "form": "424B5", "evidence_relevance_score": "60", "days_from_event": "-40"},
                    ]
                )
            rows = extractor.selected_filing_rows(path, 1, 2)
        self.assertEqual([row["form"] for row in rows], ["8-K", "6-K", "424B5"])

    def test_fundamental_selection_keeps_periodic_statement_beyond_primary_cap(self) -> None:
        columns = [
            "queue_rank",
            "event_candidate_id",
            "event_family",
            "event_type",
            "form",
            "evidence_relevance_score",
            "days_from_event",
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "candidates.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=columns)
                writer.writeheader()
                writer.writerows(
                    [
                        {"queue_rank": "1", "event_candidate_id": "FUND", "event_family": "fundamental_shock", "event_type": "negative_equity", "form": "8-K", "evidence_relevance_score": "120", "days_from_event": "1"},
                        {"queue_rank": "1", "event_candidate_id": "FUND", "event_family": "fundamental_shock", "event_type": "negative_equity", "form": "8-K", "evidence_relevance_score": "110", "days_from_event": "2"},
                        {"queue_rank": "1", "event_candidate_id": "FUND", "event_family": "fundamental_shock", "event_type": "negative_equity", "form": "10-Q", "evidence_relevance_score": "80", "days_from_event": "20"},
                    ]
                )
            rows = extractor.selected_filing_rows(path, 1, 2)
        self.assertEqual([row["form"] for row in rows], ["8-K", "8-K", "10-Q"])

    def test_document_client_retries_transient_sec_error(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self) -> bytes:
                return b"ok"

        failures = [
            urllib.error.HTTPError("https://sec.test/doc", 503, "busy", {}, None),
            Response(),
        ]
        with TemporaryDirectory() as directory:
            client = extractor.DocumentClient(
                "Research Bot test@example.com", Path(directory), min_interval=0
            )
            with patch.object(extractor.urllib.request, "urlopen", side_effect=failures) as mocked:
                with patch.object(extractor.time, "sleep"):
                    payload = client.get("https://sec.test/doc", "doc.html")
        self.assertEqual(payload, b"ok")
        self.assertEqual(mocked.call_count, 2)

    def test_document_client_retries_transient_transport_error(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self) -> bytes:
                return b"ok"

        failures = [urllib.error.URLError("temporary TLS EOF"), Response()]
        with TemporaryDirectory() as directory:
            client = extractor.DocumentClient(
                "Research Bot test@example.com", Path(directory), min_interval=0
            )
            with patch.object(extractor.urllib.request, "urlopen", side_effect=failures) as mocked:
                with patch.object(extractor.time, "sleep"):
                    payload = client.get("https://sec.test/doc", "doc.html")
        self.assertEqual(payload, b"ok")
        self.assertEqual(mocked.call_count, 2)

    def test_visible_text_removes_script_and_extracts_body(self) -> None:
        payload = b"<html><script>ignore me</script><p>Company filed Chapter 11.</p></html>"
        text = extractor.visible_text(payload)
        self.assertNotIn("ignore me", text)
        self.assertIn("Chapter 11", text)

    def test_bankruptcy_passage_prefers_equity_outcome(self) -> None:
        text = (
            "The company filed a voluntary petition under Chapter 11. "
            "The plan provides that existing common stock will be canceled for no consideration. "
            "Other operations continue."
        )
        passage = extractor.passage_for_event(text, "bankruptcy_liquidation", 500)
        self.assertIn("no consideration", passage.matched_keywords)
        self.assertIn("canceled", passage.matched_keywords)
        self.assertGreaterEqual(passage.score, 10)

    def test_delisting_passage_retains_merger_context(self) -> None:
        text = (
            "The merger closed on June 1. Each share was converted into the right to receive "
            "0.85 shares as merger consideration. Nasdaq will delist the old shares."
        )
        passage = extractor.passage_for_event(text, "delisted", 500)
        self.assertIn("merger consideration", passage.matched_keywords)
        self.assertIn("delist", passage.matched_keywords)

    def test_delisting_passage_surfaces_unit_transition(self) -> None:
        passage = extractor.passage_for_event(
            "After the closing, the SPAC units ceased trading and the successor common stock began trading on Nasdaq.",
            "delisted",
            500,
        )
        self.assertIn("units ceased trading", passage.matched_keywords)
        self.assertIn("began trading on nasdaq", passage.matched_keywords)

    def test_voluntary_delisting_passage_surfaces_cost_and_liquidity_cause(self) -> None:
        passage = extractor.passage_for_event(
            "Low trading volume and a limited public shareholder base reduced liquidity, while reporting requirements created regulatory burdens.",
            "voluntarydelisting",
            500,
        )
        self.assertIn("low trading volume", passage.matched_keywords)
        self.assertIn("regulatory burdens", passage.matched_keywords)

    def test_delisting_passage_surfaces_going_dark_cashout(self) -> None:
        passage = extractor.passage_for_event(
            "The Going Dark Transaction uses a reverse split and pays the Cash-Out Price before delisting.",
            "voluntarydelisting",
            500,
        )
        self.assertIn("going dark transaction", passage.matched_keywords)
        self.assertIn("cash-out price", passage.matched_keywords)

    def test_delisting_passage_surfaces_otc_transition(self) -> None:
        passage = extractor.passage_for_event(
            "After delisting the shares are expected to be quoted on the OTCQX and reduce our expenses.",
            "voluntarydelisting",
            500,
        )
        self.assertIn("quoted on the otcqx", passage.matched_keywords)

    def test_delisting_passage_surfaces_home_market_consolidation(self) -> None:
        passage = extractor.passage_for_event(
            "The company will move to a sole primary listing and consolidate trading liquidity on the ASX.",
            "voluntarydelisting",
            500,
        )
        self.assertIn("sole primary listing", passage.matched_keywords)
        self.assertIn("consolidate trading liquidity", passage.matched_keywords)

    def test_delisting_passage_surfaces_takeover_and_compulsory_redemption(self) -> None:
        passage = extractor.passage_for_event(
            "The buyer controls more than 90 percent, has offered to purchase all of the outstanding shares and requested delisting before compulsory redemption.",
            "voluntarydelisting",
            500,
        )
        self.assertIn("controls more than 90 percent", passage.matched_keywords)
        self.assertIn("compulsory redemption", passage.matched_keywords)

    def test_delisting_passage_surfaces_spac_combination_deadline(self) -> None:
        passage = extractor.passage_for_event(
            "The special purpose acquisition company did not complete its initial business combination within 36 months and is subject to delisting.",
            "delisted",
            500,
        )
        self.assertIn("initial business combination", passage.matched_keywords)
        self.assertIn("within 36 months", passage.matched_keywords)

    def test_delisting_passage_surfaces_bankruptcy_as_direct_cause(self) -> None:
        passage = extractor.passage_for_event(
            "After the company filed a voluntary petition under Chapter 11, Nasdaq determined to delist its common stock.",
            "delisted",
            500,
        )
        self.assertIn("chapter 11", passage.matched_keywords)
        self.assertIn("voluntary petition", passage.matched_keywords)

    def test_long_chunk_is_cropped_around_strongest_evidence(self) -> None:
        text = (
            "The company filed Chapter 11 and continued normal reporting "
            + "background " * 100
            + "On the effective date all old common stock was canceled for no consideration."
        )
        passage = extractor.passage_for_event(text, "bankruptcy_liquidation", 240)
        self.assertIn("no consideration", passage.text.lower())
        self.assertIn("canceled", passage.text.lower())

    def test_chapter_7_liquidation_is_a_strong_bankruptcy_passage(self) -> None:
        passage = extractor.passage_for_event(
            "The company ceased operations and filed chapter 7. A bankruptcy trustee will liquidate its assets.",
            "bankruptcy_liquidation",
            300,
        )
        self.assertIn("chapter 7", passage.matched_keywords)
        self.assertIn("liquidate", passage.matched_keywords)
        self.assertGreaterEqual(passage.score, 20)

    def test_judicial_management_is_strong_insolvency_evidence(self) -> None:
        passage = extractor.passage_for_event(
            "The High Court placed the company under interim judicial management and appointed a judicial manager.",
            "bankruptcy_liquidation",
            400,
        )
        self.assertIn("judicial management", passage.matched_keywords)
        self.assertGreaterEqual(passage.score, 20)

    def test_liquidated_damages_is_not_liquidation_evidence(self) -> None:
        passage = extractor.passage_for_event(
            "A liquidated damages charge equal to 25 percent becomes payable under the note.",
            "bankruptcy_liquidation",
            400,
        )
        self.assertEqual(passage.text, "")

    def test_low_price_suspension_is_visible_as_bankruptcy_false_positive_evidence(self) -> None:
        passage = extractor.passage_for_event(
            "Because the closing bid price stayed below $0.10, the securities would be suspended by Nasdaq.",
            "bankruptcy_liquidation",
            400,
        )
        self.assertIn("securities would be suspended", passage.matched_keywords)

    def test_acceleration_and_forbearance_are_visible_without_becoming_bankruptcy(self) -> None:
        passage = extractor.passage_for_event(
            "After Events of Default, the lender issued an Acceleration Notice and made the obligations immediately due and payable while a Limited Forbearance remained in effect.",
            "bankruptcy_liquidation",
            500,
        )
        self.assertIn("acceleration notice", passage.matched_keywords)
        self.assertIn("immediately due and payable", passage.matched_keywords)
        self.assertIn("limited forbearance", passage.matched_keywords)

    def test_article_9_collateral_disposition_is_visible_as_debt_enforcement(self) -> None:
        passage = extractor.passage_for_event(
            "The secured lender delivered a UCC Sale Notice for a private disposition of collateral. A buyer agreed to acquire all of the collateral pursuant to Article 9 of the Uniform Commercial Code.",
            "bankruptcy_liquidation",
            500,
        )
        self.assertIn("private disposition of collateral", passage.matched_keywords)
        self.assertIn("acquire all of the collateral", passage.matched_keywords)
        self.assertGreaterEqual(passage.score, 20)

    def test_cash_returning_liquidation_surfaces_distribution_and_trust_terms(self) -> None:
        passage = extractor.passage_for_event(
            "Under the Plan of Sale and Dissolution, the company paid a final cash liquidating distribution and converted each share into beneficial interest units of a liquidating trust.",
            "bankruptcy_liquidation",
            500,
        )
        self.assertIn("final cash liquidating distribution", passage.matched_keywords)
        self.assertIn("plan of sale and dissolution", passage.matched_keywords)
        self.assertIn("liquidating trust", passage.matched_keywords)
        self.assertGreaterEqual(passage.score, 30)

    def test_ads_delisting_surfaces_home_market_continuity(self) -> None:
        passage = extractor.passage_for_event(
            "ADS trading volume was very limited. The common shares will continue to be listed and traded on B3, their principal trading market, and the company will maintain its ADS program.",
            "voluntarydelisting",
            500,
        )
        self.assertIn("continue to be listed and traded", passage.matched_keywords)
        self.assertIn("principal trading market", passage.matched_keywords)
        self.assertIn("maintain its ads program", passage.matched_keywords)

    def test_price_crash_passage_can_surface_warrant_dilution(self) -> None:
        passage = extractor.passage_for_event(
            "The company signed a warrant exchange. It will issue 240 million Exchange Shares in the offering.",
            "volume_crash",
            300,
        )
        self.assertIn("warrant exchange", passage.matched_keywords)
        self.assertIn("exchange shares", passage.matched_keywords)

    def test_price_crash_passage_can_surface_compliance_resolution(self) -> None:
        passage = extractor.passage_for_event(
            "The company made full repayment. Nasdaq confirmed the matter regarding fees is closed.",
            "volume_crash",
            300,
        )
        self.assertIn("full repayment", passage.matched_keywords)
        self.assertIn("matter regarding", passage.matched_keywords)

    def test_price_crash_passage_prioritizes_regulatory_trading_suspension(self) -> None:
        passage = extractor.passage_for_event(
            "The Office of Foreign Assets Control placed the issuer on the Specially Designated Nationals list. Nasdaq said trading in the stock has been suspended.",
            "one_day_crash",
            400,
        )
        self.assertIn("office of foreign assets control", passage.matched_keywords)
        self.assertIn("specially designated nationals", passage.matched_keywords)
        self.assertIn("suspended", passage.matched_keywords)
        self.assertGreaterEqual(passage.score, 28)

    def test_reverse_split_passage_surfaces_listing_and_financing_context(self) -> None:
        passage = extractor.passage_for_event(
            "The reverse stock split was used to regain compliance with the minimum bid price. The company then entered a registered direct offering and agreed to sell new shares.",
            "reverse_split",
            500,
        )
        self.assertIn("regain compliance", passage.matched_keywords)
        self.assertIn("minimum bid price", passage.matched_keywords)
        self.assertIn("registered direct offering", passage.matched_keywords)

    def test_reverse_split_passage_surfaces_transaction_mechanics(self) -> None:
        passage = extractor.passage_for_event(
            "The reverse stock split became effective immediately before the merger agreement and name change.",
            "reverse_split",
            400,
        )
        self.assertIn("merger agreement", passage.matched_keywords)
        self.assertIn("name change", passage.matched_keywords)

    def test_reverse_split_passage_surfaces_conditional_offering_and_cashless_warrants(self) -> None:
        passage = extractor.passage_for_event(
            "The reverse stock split is a condition to the closing of a best efforts public offering. The securities purchase agreement also includes warrants exercisable on an alternate cashless basis.",
            "reverse_split",
            500,
        )
        self.assertIn("best efforts public offering", passage.matched_keywords)
        self.assertIn("securities purchase agreement", passage.matched_keywords)
        self.assertIn("alternate cashless basis", passage.matched_keywords)

    def test_reverse_split_passage_surfaces_atm_and_prefunded_warrants(self) -> None:
        passage = extractor.passage_for_event(
            "Under the sales agreement, the company may conduct an at-the-market offering up to an aggregate offering price of $100 million and previously issued pre-funded warrants.",
            "reverse_split",
            500,
        )
        self.assertIn("at-the-market offering", passage.matched_keywords)
        self.assertIn("aggregate offering price", passage.matched_keywords)
        self.assertIn("pre-funded warrants", passage.matched_keywords)

    def test_reverse_split_passage_surfaces_rescinded_financing(self) -> None:
        passage = extractor.passage_for_event(
            "The company previously described a securities purchase agreement and proposed note issuance. "
            "The company has rescinded the issuance of the note and warrants because of non-payment of the proceeds.",
            "reverse_split",
            500,
        )
        self.assertIn("rescinded the issuance", passage.matched_keywords)
        self.assertIn("non-payment of the proceeds", passage.matched_keywords)

    def test_no_change_in_control_is_not_positive_reverse_split_evidence(self) -> None:
        passage = extractor.passage_for_event(
            "Accounting guidance states that a common-control transaction results in no change in control over the net assets.",
            "reverse_split",
            400,
        )
        self.assertNotIn("change in control", passage.matched_keywords)

    def test_hypothetical_change_in_control_is_not_positive_evidence(self) -> None:
        passage = extractor.passage_for_event(
            "Certain shareholders may limit your ability to influence important transactions, including a change in control.",
            "reverse_split",
            400,
        )
        self.assertNotIn("change in control", passage.matched_keywords)
        passage = extractor.passage_for_event(
            "Concentrated ownership may have the effect of delaying, preventing or deterring a change in control.",
            "reverse_split",
            400,
        )
        self.assertNotIn("change in control", passage.matched_keywords)

    def test_reverse_split_passage_surfaces_court_restructuring(self) -> None:
        passage = extractor.passage_for_event(
            "The court agreed to sanction the restructuring plan. The company will issue new ordinary shares to bondholders and change the ratio of Shares to ADSs.",
            "reverse_split",
            500,
        )
        self.assertIn("court agreed to sanction", passage.matched_keywords)
        self.assertIn("restructuring plan", passage.matched_keywords)
        self.assertIn("new ordinary shares", passage.matched_keywords)

    def test_negative_equity_passage_surfaces_capital_return_or_debt_distress(self) -> None:
        passage = extractor.passage_for_event(
            "Total stockholders' deficit reflects treasury stock after the share repurchase. The company reported net income and net cash provided by operating activities.",
            "negative_equity",
            500,
        )
        self.assertIn("treasury stock", passage.matched_keywords)
        self.assertIn("share repurchase", passage.matched_keywords)
        passage = extractor.passage_for_event(
            "The company has an accumulated deficit and a troubled debt restructuring. Net cash used in operating activities and long-term debt constrain liquidity.",
            "negative_equity",
            500,
        )
        self.assertIn("troubled debt restructuring", passage.matched_keywords)
        self.assertIn("net cash used in operating activities", passage.matched_keywords)
        passage = extractor.passage_for_event(
            "Total stockholders’ deficit includes treasury stock.",
            "negative_equity",
            300,
        )
        self.assertIn("stockholders' deficit", passage.matched_keywords)

    def test_negative_equity_prefers_statement_value_over_unexercised_repurchase_authorization(self) -> None:
        passage = extractor.passage_for_event(
            "The share repurchase authorization permits purchases up to $10 million but no shares were repurchased. "
            "Total stockholders' equity (deficit) was $(72.8) million at April 30, 2026 compared with $(65.0) million.",
            "negative_equity",
            500,
        )
        self.assertIn("total stockholders", passage.text.lower())

    def test_fundamental_passages_prefer_quantified_comparisons(self) -> None:
        revenue = extractor.passage_for_event(
            "Revenue may decline because of competition. Total revenues were $2.0 million for the three months ended March 31, 2025 compared to $8.0 million in 2024.",
            "revenue_collapse_yoy",
            500,
        )
        self.assertIn("$2.0 million", revenue.text)
        margin = extractor.passage_for_event(
            "Gross margin may fluctuate. Gross profit decreased to $1.0 million for the three months ended March 31, 2025 compared to $4.0 million in 2024.",
            "gross_margin_collapse",
            500,
        )
        self.assertIn("$1.0 million", margin.text)

    def test_ex99_fallback_finds_evidence_missing_from_primary_form(self) -> None:
        index = b"""<table summary="Document Format Files">
        <tr><td>1</td><td>6-K</td><td><a href="/Archives/a/main.htm">main.htm</a></td><td>6-K</td></tr>
        <tr><td>2</td><td>Press release</td><td><a href="/Archives/a/ex991.htm">ex991.htm</a></td><td>EX-99.1</td></tr>
        </table>"""

        class FakeClient:
            def get(self, url: str, cache_key: str) -> bytes:
                payloads = {
                    "https://www.sec.gov/Archives/a/main.htm": b"<html>Form 6-K cover page only.</html>",
                    "https://www.sec.gov/Archives/a/index.htm": index,
                    "https://www.sec.gov/Archives/a/ex991.htm": b"<html>The plan cancels old common shares for no consideration.</html>",
                }
                return payloads[url]

        rows, errors = extractor.extract_rows(
            [
                {
                    "queue_rank": "1",
                    "event_candidate_id": "C1",
                    "ticker_at_event": "TEST",
                    "event_date": "2026-01-01",
                    "event_type": "bankruptcy_liquidation",
                    "filing_date": "2026-01-01",
                    "form": "6-K",
                    "items": "",
                    "accession_number": "0001",
                    "primary_document": "main.htm",
                    "filing_document_url": "https://www.sec.gov/Archives/a/main.htm",
                    "filing_index_url": "https://www.sec.gov/Archives/a/index.htm",
                    "form_item_match_hint": "relevant_form_needs_text_review",
                }
            ],
            client=FakeClient(),
            max_chars=700,
        )
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["filing_document_url"], "https://www.sec.gov/Archives/a/ex991.htm")
        self.assertIn("no consideration", rows[0]["matched_keywords"])

    def test_ex99_can_replace_weak_primary_passage(self) -> None:
        index = b"""<table summary="Document Format Files">
        <tr><td>1</td><td>6-K</td><td><a href="/Archives/a/main.htm">main.htm</a></td><td>6-K</td></tr>
        <tr><td>2</td><td>Press release</td><td><a href="/Archives/a/ex991.htm">ex991.htm</a></td><td>EX-99.1</td></tr>
        </table>"""

        class FakeClient:
            def get(self, url: str, cache_key: str) -> bytes:
                return {
                    "https://www.sec.gov/Archives/a/main.htm": b"<html>The company will delist.</html>",
                    "https://www.sec.gov/Archives/a/index.htm": index,
                    "https://www.sec.gov/Archives/a/ex991.htm": b"<html>The merger closed and each share was converted into the right to receive merger consideration.</html>",
                }[url]

        rows, errors = extractor.extract_rows(
            [{
                "queue_rank": "1", "event_candidate_id": "C1", "ticker_at_event": "TEST",
                "event_date": "2026-01-01", "event_type": "delisted", "filing_date": "2026-01-01",
                "form": "6-K", "items": "", "accession_number": "0001", "primary_document": "main.htm",
                "filing_document_url": "https://www.sec.gov/Archives/a/main.htm",
                "filing_index_url": "https://www.sec.gov/Archives/a/index.htm",
                "form_item_match_hint": "relevant_form_needs_text_review",
            }],
            client=FakeClient(),
            max_chars=700,
        )
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["filing_document_url"], "https://www.sec.gov/Archives/a/ex991.htm")
        self.assertIn("merger consideration", rows[0]["matched_keywords"])

    def test_periodic_report_ex99_can_supply_final_delisting_outcome(self) -> None:
        index = b"""<table summary="Document Format Files">
        <tr><td>1</td><td>10-Q</td><td><a href="/Archives/a/form10-q.htm">form10-q.htm</a></td><td>10-Q</td></tr>
        <tr><td>2</td><td>Press release</td><td><a href="/Archives/a/ex99-1.htm">ex99-1.htm</a></td><td>EX-99.1</td></tr>
        </table>"""

        class FakeClient:
            def get(self, url: str, cache_key: str) -> bytes:
                return {
                    "https://www.sec.gov/Archives/a/form10-q.htm": b"<html>Quarterly financial statements.</html>",
                    "https://www.sec.gov/Archives/a/index.htm": index,
                    "https://www.sec.gov/Archives/a/ex99-1.htm": b"<html>Nasdaq determined to delist the securities for continued non-compliance with the minimum bid price requirement. Trading will be suspended and the company plans to transition to OTC Markets.</html>",
                }[url]

        rows, errors = extractor.extract_rows(
            [{
                "queue_rank": "1", "event_candidate_id": "C1", "ticker_at_event": "TEST",
                "event_date": "2026-01-01", "event_type": "delisted", "filing_date": "2026-01-02",
                "form": "10-Q", "items": "", "accession_number": "0001", "primary_document": "form10-q.htm",
                "filing_document_url": "https://www.sec.gov/Archives/a/form10-q.htm",
                "filing_index_url": "https://www.sec.gov/Archives/a/index.htm",
                "form_item_match_hint": "relevant_form_needs_text_review",
            }],
            client=FakeClient(),
            max_chars=700,
        )
        self.assertEqual(errors, [])
        self.assertEqual(rows[0]["filing_document_url"], "https://www.sec.gov/Archives/a/ex99-1.htm")
        self.assertIn("minimum bid price", rows[0]["matched_keywords"])
        self.assertIn("transition to otc", rows[0]["matched_keywords"])

    def test_interest_coverage_passage_surfaces_covenant_relief_and_compliance(self) -> None:
        relief = extractor.passage_for_event(
            "The lender modified the covenant to defer measurement through July 2027. The company had $22 million available to borrow.",
            "interest_coverage_below_1",
            400,
        )
        self.assertIn("defer measurement", relief.matched_keywords)
        compliance = extractor.passage_for_event(
            "The company was in compliance with all financial covenants and had cash and cash equivalents available.",
            "interest_coverage_below_1",
            400,
        )
        self.assertIn("in compliance with all financial covenants", compliance.matched_keywords)


if __name__ == "__main__":
    unittest.main()
