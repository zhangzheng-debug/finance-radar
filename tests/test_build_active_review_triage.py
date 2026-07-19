from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import build_active_review_triage as triage


def event(event_id: str, event_type: str, family: str, rank: int = 1) -> dict[str, str]:
    return {
        "event_candidate_id": event_id,
        "ticker_at_event": event_id,
        "event_date": "2026-01-01",
        "event_family": family,
        "event_type": event_type,
        "queue_rank": str(rank),
        "stable_id": event_id,
    }


def passage(event_id: str, text: str, form: str = "8-K") -> dict[str, str]:
    return {
        "event_candidate_id": event_id,
        "evidence_passage": text,
        "form_item_match_hint": "relevant_form_needs_text_review",
        "passage_score": "10",
        "filing_date": "2026-01-01",
        "form": form,
        "filing_document_url": "https://www.sec.gov/example",
    }


class ActiveReviewTriageTests(unittest.TestCase):
    def test_fundamental_tie_prefers_timely_post_period_quarterly_filing(self) -> None:
        candidate = event("FUND", "negative_equity", "fundamental_shock")
        pre_event = passage("FUND", "Total stockholders' equity was $21 million.", "8-K")
        pre_event["filing_date"] = "2025-12-01"
        post_period = passage("FUND", "Total stockholders' equity (deficit) was $(9) million.", "10-Q")
        post_period["filing_date"] = "2026-02-10"
        rows = triage.build_rows([candidate], [pre_event, post_period], [])
        self.assertEqual(rows[0]["form"], "10-Q")
        self.assertIn("$(9) million", rows[0]["evidence_passage"])

    def test_equity_death_outranks_bankruptcy_without_equity_outcome(self) -> None:
        rows = triage.build_rows(
            [
                event("S", "bankruptcy_liquidation", "bankruptcy_or_distress", 1),
                event("A", "bankruptcy_liquidation", "bankruptcy_or_distress", 2),
            ],
            [
                passage("S", "Old common interests will receive no distribution and will be canceled for no consideration."),
                passage("A", "The company filed a voluntary petition under Chapter 11."),
            ],
            [],
        )
        self.assertEqual(rows[0]["event_candidate_id"], "S")
        self.assertEqual(rows[0]["grade_ceiling"], "S_deep_review_only")
        self.assertEqual(rows[1]["grade_ceiling"], "A++_review_ceiling")

    def test_merger_delisting_is_prioritized_as_false_positive_control(self) -> None:
        rows = triage.build_rows(
            [event("M", "delisted", "delisting_or_suspension")],
            [passage("M", "Each share was converted into the right to receive merger consideration.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "false_positive_control")
        self.assertEqual(rows[0]["proposed_disposition"], "likely_reject_negative_delisting")

    def test_bankruptcy_driven_delisting_routes_to_cross_family_chain_review(self) -> None:
        rows = triage.build_rows(
            [event("BKDEL", "delisted", "delisting_or_suspension")],
            [passage("BKDEL", "After the company filed a voluntary petition under Chapter 11, Nasdaq determined to delist the common stock.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "bankruptcy_driven_delisting")
        self.assertEqual(rows[0]["grade_ceiling"], "A++_review_ceiling")
        self.assertIn("cross_family_event_chain_dedup_required", rows[0]["reason_codes"])

    def test_multi_rule_forced_delisting_routes_to_a_review(self) -> None:
        rows = triage.build_rows(
            [event("MULTI", "delisted", "delisting_or_suspension")],
            [passage("MULTI", "The issuer missed the minimum bid price and minimum stockholders' equity requirements and is scheduled for delisting.", "6-K")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "delisting_cause_review")
        self.assertEqual(rows[0]["grade_ceiling"], "A_review_ceiling")

    def test_spac_unit_transition_is_a_false_positive_control(self) -> None:
        unit_event = event("UNIT", "delisted", "delisting_or_suspension")
        unit_event["ticker_at_event"] = "TESTU"
        rows = triage.build_rows(
            [unit_event],
            [passage("UNIT", "The SPAC units ceased trading and successor common stock began trading on Nasdaq.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "false_positive_control")
        self.assertIn("spac_unit", rows[0]["reason_codes"])

    def test_stale_spac_history_does_not_override_current_non_unit_delisting(self) -> None:
        current = event("CURRENT", "voluntarydelisting", "delisting_or_suspension")
        current["ticker_at_event"] = "OPER"
        rows = triage.build_rows(
            [current],
            [
                passage("CURRENT", "In 2019 the predecessor units ceased trading and successor common stock began trading on Nasdaq."),
                passage("CURRENT", "The current board cited a lack of an active trading market and reporting costs for the voluntary delisting."),
            ],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "market_access_contraction")

    def test_cost_driven_exit_with_low_volume_gets_a_ceiling(self) -> None:
        rows = triage.build_rows(
            [event("VOL", "voluntarydelisting", "delisting_or_suspension")],
            [passage("VOL", "Low trading volume and reporting requirements created regulatory burdens.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "market_access_contraction")
        self.assertEqual(rows[0]["grade_ceiling"], "A_review_ceiling")

    def test_going_dark_cashout_is_not_misclassified_as_merger(self) -> None:
        rows = triage.build_rows(
            [event("DARK", "voluntarydelisting", "delisting_or_suspension")],
            [passage("DARK", "In a Going Dark Transaction, a reverse stock split converts small holders into the right to receive the Cash-Out Price before the company will delist and deregister.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "going_dark_review")
        self.assertNotEqual(rows[0]["review_bucket"], "false_positive_control")

    def test_compliance_reverse_split_is_not_misclassified_as_going_dark(self) -> None:
        rows = triage.build_rows(
            [event("COMPLIANCE", "delisted", "delisting_or_suspension")],
            [passage("COMPLIANCE", "Nasdaq issued a delisting determination after the company failed the minimum bid price listing rule. The company effected a reverse stock split and appealed the suspension.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "delisting_cause_review")
        self.assertNotEqual(rows[0]["review_bucket"], "going_dark_review")

    def test_later_financing_does_not_backfill_reverse_split_label(self) -> None:
        candidate = event("SPLITTHENFIN", "reverse_split", "equity_dilution")
        later_financing = passage(
            "SPLITTHENFIN",
            "The company is offering units consisting of common stock, pre-funded warrants and common warrants.",
            "424B4",
        )
        later_financing["filing_date"] = "2026-03-15"
        rows = triage.build_rows([candidate], [later_financing], [])
        self.assertEqual(rows[0]["review_bucket"], "separate_financing_event")
        self.assertEqual(rows[0]["grade_ceiling"], "B_review_ceiling")
        self.assertIn("future_leakage_prevention", rows[0]["reason_codes"])

    def test_takeover_control_and_compulsory_redemption_is_delisting_control(self) -> None:
        rows = triage.build_rows(
            [event("TAKEOVER", "voluntarydelisting", "delisting_or_suspension")],
            [passage("TAKEOVER", "The buyer controls more than 90 percent, offered to purchase all of the outstanding shares and will begin compulsory redemption.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "false_positive_control")

    def test_spac_deadline_delisting_is_lifecycle_control(self) -> None:
        rows = triage.build_rows(
            [event("SPACDL", "delisted", "delisting_or_suspension")],
            [passage("SPACDL", "The special purpose acquisition company did not complete its initial business combination within 36 months and is subject to delisting.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "false_positive_control")
        self.assertIn("spac_combination_deadline", rows[0]["reason_codes"])

    def test_weak_market_without_home_exchange_gets_a_ceiling(self) -> None:
        rows = triage.build_rows(
            [event("WEAK", "voluntarydelisting", "delisting_or_suspension")],
            [passage("WEAK", "The board cited a lack of an active trading market and reporting costs, with no assurance that trading would continue after delisting.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "market_access_contraction")
        self.assertEqual(rows[0]["grade_ceiling"], "A_review_ceiling")

    def test_home_exchange_consolidation_stays_b_ceiling(self) -> None:
        rows = triage.build_rows(
            [event("HOME", "voluntarydelisting", "delisting_or_suspension")],
            [passage("HOME", "The issuer will remain listed on Euronext Paris and simplify corporate structure after the ADS delisting.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "voluntary_listing_exit")
        self.assertEqual(rows[0]["grade_ceiling"], "B_review_ceiling")

    def test_ads_exit_with_b3_continuity_stays_b_ceiling(self) -> None:
        rows = triage.build_rows(
            [event("B3", "voluntarydelisting", "delisting_or_suspension")],
            [passage("B3", "The ADSs had very limited trading volume. Common shares will continue to be listed and traded on B3, their principal trading market, and the issuer will maintain its ADS program.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "voluntary_listing_exit")
        self.assertEqual(rows[0]["grade_ceiling"], "B_review_ceiling")
        self.assertIn("home_market_continuity_after_us_delisting", rows[0]["reason_codes"])

    def test_tsx_continuity_prevents_weak_market_a_ceiling(self) -> None:
        rows = triage.build_rows(
            [event("TSX", "voluntarydelisting", "delisting_or_suspension")],
            [passage("TSX", "Due to low trading volume, the issuer will delist from NYSE but continue to trade on the TSX and OTCQX.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "voluntary_listing_exit")
        self.assertEqual(rows[0]["grade_ceiling"], "B_review_ceiling")

    def test_chapter_7_is_not_misclassified_as_chapter_11(self) -> None:
        rows = triage.build_rows(
            [event("CH7", "bankruptcy_liquidation", "bankruptcy_or_distress")],
            [passage("CH7", "The company filed a voluntary petition under chapter 7 of title 11.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "liquidation_boundary")
        self.assertEqual(rows[0]["grade_ceiling"], "S_deep_review_only")
        self.assertIn("chapter_7", rows[0]["reason_codes"])

    def test_hypothetical_chapter_7_analysis_is_not_an_actual_liquidation(self) -> None:
        rows = triage.build_rows(
            [event("HYPCH7", "bankruptcy_liquidation", "bankruptcy_or_distress")],
            [passage("HYPCH7", "If no plan can be confirmed, the cases may be converted to cases under chapter 7. This liquidation analysis estimates creditor recoveries.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "hypothetical_liquidation_control")
        self.assertEqual(rows[0]["proposed_disposition"], "reject_hypothetical_chapter_7_event")
        self.assertEqual(rows[0]["grade_ceiling"], "B_review_ceiling")

    def test_pending_chapter_11_status_is_not_a_new_petition(self) -> None:
        rows = triage.build_rows(
            [event("STALEBK", "bankruptcy_liquidation", "bankruptcy_or_distress")],
            [passage("STALEBK", "The company faces risks while certain chapter 11 cases are pending and continues operating in the ordinary course while the restructuring remains subject to approval.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "stale_bankruptcy_status_control")
        self.assertEqual(rows[0]["proposed_disposition"], "reject_repeated_bankruptcy_status_as_new_event")
        self.assertIn("temporal_anchor_review_required", rows[0]["reason_codes"])

    def test_bankruptcy_driven_listing_notice_is_a_linked_consequence(self) -> None:
        rows = triage.build_rows(
            [event("BKLIST", "bankruptcy_liquidation", "bankruptcy_or_distress")],
            [passage("BKLIST", "After the chapter 11 filing, the company received written notice that its common stock will be delisted as a result of the chapter 11 cases.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "bankruptcy_driven_listing_consequence")
        self.assertEqual(rows[0]["grade_ceiling"], "A_review_ceiling")
        self.assertIn("bankruptcy_driven_listing_is_not_primary_petition", rows[0]["reason_codes"])

    def test_judicial_management_routes_to_a_plus_plus_review(self) -> None:
        rows = triage.build_rows(
            [event("JM", "bankruptcy_liquidation", "bankruptcy_or_distress")],
            [passage("JM", "The High Court appointed interim judicial managers and transferred control from the directors.", "6-K")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "court_insolvency_boundary")
        self.assertEqual(rows[0]["grade_ceiling"], "A++_review_ceiling")

    def test_low_price_delisting_rejects_bankruptcy_label(self) -> None:
        rows = triage.build_rows(
            [event("LIST", "bankruptcy_liquidation", "bankruptcy_or_distress")],
            [passage("LIST", "Nasdaq issued a Delisting Determination under the Low Priced Stocks Rule after failure of the minimum bid price.", "6-K")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "false_positive_control")
        self.assertIn("listing_event_not_bankruptcy", rows[0]["reason_codes"])

    def test_debt_acceleration_reclassifies_instead_of_verifying_bankruptcy(self) -> None:
        rows = triage.build_rows(
            [event("DEFAULT", "bankruptcy_liquidation", "bankruptcy_or_distress")],
            [passage("DEFAULT", "Following Events of Default, an Acceleration Notice made all obligations immediately due and payable.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "debt_default_boundary")
        self.assertEqual(rows[0]["proposed_disposition"], "reject_bankruptcy_reclassify_debt_default")
        self.assertEqual(rows[0]["grade_ceiling"], "A_review_ceiling")

    def test_article_9_collateral_sale_routes_to_secured_creditor_enforcement(self) -> None:
        rows = triage.build_rows(
            [event("UCC", "bankruptcy_liquidation", "bankruptcy_or_distress")],
            [passage("UCC", "The lender issued a UCC Sale Notice for a private disposition of collateral and the buyer agreed to acquire all of the collateral under Article 9 of the Uniform Commercial Code.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "secured_creditor_enforcement_boundary")
        self.assertEqual(rows[0]["grade_ceiling"], "A++_review_ceiling")
        self.assertIn("article_9_collateral_disposition_not_bankruptcy", rows[0]["reason_codes"])

    def test_cash_returning_liquidation_is_not_equity_death(self) -> None:
        rows = triage.build_rows(
            [event("CASHLIQ", "bankruptcy_liquidation", "bankruptcy_or_distress")],
            [passage("CASHLIQ", "Under the Plan of Sale and Dissolution, common holders received a final cash liquidating distribution and beneficial interest units in a liquidating trust.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "cash_returning_liquidation_boundary")
        self.assertEqual(rows[0]["grade_ceiling"], "B_review_ceiling")
        self.assertIn("cash_and_trust_interests_returned_to_common_holders", rows[0]["reason_codes"])

    def test_spac_extension_and_trust_redemption_reject_bankruptcy_label(self) -> None:
        rows = triage.build_rows(
            [event("SPACEXT", "bankruptcy_liquidation", "bankruptcy_or_distress")],
            [passage("SPACEXT", "Shareholders approved the Extension Amendment and Trust Amendment to extend the initial business combination deadline, and holders of public shares elected redemption from the trust account.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "spac_lifecycle_false_bankruptcy_control")
        self.assertEqual(rows[0]["proposed_disposition"], "reject_bankruptcy_reclassify_spac_lifecycle")
        self.assertIn("spac_extension_or_trust_redemption_not_bankruptcy", rows[0]["reason_codes"])

    def test_q_suffix_bankruptcy_requires_event_time_identity_recovery(self) -> None:
        candidate = event("LATEQ", "bankruptcy_liquidation", "bankruptcy_or_distress")
        candidate["ticker_at_event"] = "LATEQ"
        rows = triage.build_rows(
            [candidate],
            [passage("LATEQ", "The company filed a voluntary petition under Chapter 11.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "event_time_identity_control")
        self.assertEqual(rows[0]["grade_ceiling"], "A++_review_ceiling")
        self.assertIn("q_suffix_may_be_later_otc_alias", rows[0]["reason_codes"])

    def test_q_suffix_without_passage_is_prioritized_for_identity_not_accepted(self) -> None:
        candidate = event("NOPASSQ", "bankruptcy_liquidation", "bankruptcy_or_distress")
        candidate["ticker_at_event"] = "NOPASSQ"
        rows = triage.build_rows([candidate], [], [])
        self.assertEqual(rows[0]["review_bucket"], "event_time_identity_control")
        self.assertEqual(rows[0]["proposed_disposition"], "do_not_accept_q_suffix_metadata_without_primary_petition_evidence")
        self.assertEqual(rows[0]["evidence_readiness"], "no_sec_candidate_yet")

    def test_customary_insolvency_default_clause_is_low_priority_bankruptcy_control(self) -> None:
        rows = triage.build_rows(
            [event("BOILER", "bankruptcy_liquidation", "bankruptcy_or_distress")],
            [passage("BOILER", "The notes contain customary covenants and events of default, including hypothetical bankruptcy or insolvency events after which principal may be due.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "contract_boilerplate_control")
        self.assertEqual(rows[0]["grade_ceiling"], "C_review_ceiling")
        self.assertIn("customary_default_clause_not_actual_bankruptcy", rows[0]["reason_codes"])

    def test_reviewed_rows_are_excluded_and_price_only_stays_capped(self) -> None:
        rows = triage.build_rows(
            [
                event("DONE", "delisted", "delisting_or_suspension", 1),
                event("PRICE", "volume_crash", "price_crash", 2),
            ],
            [],
            [{"event_candidate_id": "DONE"}],
        )
        self.assertEqual([row["event_candidate_id"] for row in rows], ["PRICE"])
        self.assertEqual(rows[0]["grade_ceiling"], "C_price_only")

    def test_reviewed_detector_excludes_same_security_date_family_sibling(self) -> None:
        first = event("CRASH1", "volume_crash", "price_crash", 1)
        sibling = event("CRASH2", "one_day_crash", "price_crash", 2)
        first["stable_id"] = sibling["stable_id"] = "permaticker:1"
        rows = triage.build_rows(
            [first, sibling],
            [],
            [{"event_candidate_id": "CRASH1"}],
        )
        self.assertEqual(rows, [])

    def test_unreviewed_sibling_detectors_share_one_review_row(self) -> None:
        first = event("CRASH1", "volume_crash", "price_crash", 1)
        sibling = event("CRASH2", "one_day_crash", "price_crash", 2)
        first["stable_id"] = sibling["stable_id"] = "permaticker:1"
        rows = triage.build_rows([first, sibling], [], [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_candidate_id"], "CRASH1")

    def test_price_crash_detectors_within_thirty_days_share_one_review_row(self) -> None:
        first = event("CRASH1", "one_day_crash", "price_crash", 1)
        later = event("CRASH2", "twenty_one_day_crash", "price_crash", 2)
        first["stable_id"] = later["stable_id"] = "permaticker:1"
        first["event_date"] = "2026-01-01"
        later["event_date"] = "2026-01-29"
        rows = triage.build_rows(
            [first, later],
            [passage("CRASH2", "The warrant exchange issued 240 million Exchange Shares.")],
            [],
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_candidate_id"], "CRASH2")

    def test_price_crash_detectors_beyond_thirty_days_are_separate_threads(self) -> None:
        first = event("CRASH1", "one_day_crash", "price_crash", 1)
        later = event("CRASH2", "twenty_one_day_crash", "price_crash", 2)
        first["stable_id"] = later["stable_id"] = "permaticker:1"
        first["event_date"] = "2026-01-01"
        later["event_date"] = "2026-02-01"
        self.assertEqual(len(triage.build_rows([first, later], [], [])), 2)

    def test_reviewed_price_crash_excludes_later_detector_in_same_episode(self) -> None:
        first = event("CRASH1", "one_day_crash", "price_crash", 1)
        later = event("CRASH2", "twenty_one_day_crash", "price_crash", 2)
        first["stable_id"] = later["stable_id"] = "permaticker:1"
        first["event_date"] = "2026-01-01"
        later["event_date"] = "2026-01-20"
        rows = triage.build_rows(
            [first, later],
            [],
            [{"event_candidate_id": "CRASH1"}],
        )
        self.assertEqual(rows, [])

    def test_prior_queue_adjudication_excludes_cross_batch_price_sibling(self) -> None:
        later = event("CRASH2", "one_day_crash", "price_crash", 2)
        later["stable_id"] = "permaticker:1"
        later["event_date"] = "2026-01-20"
        rows = triage.build_rows(
            [later],
            [],
            [{
                "event_candidate_id": "OLD-CRASH",
                "stable_id": "permaticker:1",
                "event_date": "2026-01-01",
                "detected_event_type": "volume_crash",
            }],
        )
        self.assertEqual(rows, [])

    def test_price_crash_with_dilution_evidence_routes_to_a_review(self) -> None:
        rows = triage.build_rows(
            [event("DIL", "volume_crash", "price_crash")],
            [passage("DIL", "The warrant exchange issued 240 million Exchange Shares.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "dilution_cause_review")
        self.assertEqual(rows[0]["grade_ceiling"], "A_review_ceiling")

    def test_price_crash_with_prefunded_warrant_resale_routes_to_a_review(self) -> None:
        rows = triage.build_rows(
            [event("RESALE", "volume_crash", "price_crash")],
            [passage("RESALE", "The registration statement covers resale by selling shareholders of common shares issuable upon exercise of pre-funded warrants.", "6-K")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "dilution_cause_review")
        self.assertEqual(rows[0]["grade_ceiling"], "A_review_ceiling")

    def test_price_crash_with_priced_ordinary_share_offering_routes_to_dilution_review(self) -> None:
        rows = triage.build_rows(
            [event("OFFER", "volume_crash", "price_crash")],
            [passage("OFFER", "The company announces pricing of a $10.8 million public offering of ordinary shares.", "6-K")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "dilution_cause_review")

    def test_price_crash_with_split_and_authorized_share_expansion_routes_to_structural_dilution(self) -> None:
        rows = triage.build_rows(
            [event("STRUCT", "volume_crash", "price_crash")],
            [passage("STRUCT", "Following the share consolidation, shareholders approved the increase of authorized shares and creation of an additional 9 billion ordinary shares.", "6-K")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "structural_dilution_cause_review")

    def test_price_crash_with_customary_default_clause_stays_control(self) -> None:
        rows = triage.build_rows(
            [event("COV", "volume_crash", "price_crash")],
            [passage("COV", "The loan documents contain customary events of default, including bankruptcy or insolvency events.", "20-F")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "contract_boilerplate_control")
        self.assertEqual(rows[0]["grade_ceiling"], "C_price_only")

    def test_price_crash_with_official_resolution_stays_price_only(self) -> None:
        rows = triage.build_rows(
            [event("FIXED", "volume_crash", "price_crash")],
            [passage("FIXED", "The company made full repayment and the matter regarding fees is closed.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "false_positive_control")
        self.assertEqual(rows[0]["grade_ceiling"], "C_price_only")

    def test_price_crash_with_pre_event_liquidity_distress_routes_to_a_review(self) -> None:
        rows = triage.build_rows(
            [event("CASH", "one_day_crash", "price_crash")],
            [passage("CASH", "The company has substantial doubt about its ability to continue as a going concern and does not have sufficient cash resources for twelve months.", "NT 10-Q")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "liquidity_distress_cause_review")
        self.assertEqual(rows[0]["grade_ceiling"], "A_review_ceiling")

    def test_price_crash_with_regulatory_suspension_routes_to_a_plus_plus_review(self) -> None:
        rows = triage.build_rows(
            [event("OFAC", "one_day_crash", "price_crash")],
            [passage("OFAC", "The Office of Foreign Assets Control designated the company as a Specially Designated National and trading in its stock has been suspended.", "6-K")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "regulatory_trading_suspension")
        self.assertEqual(rows[0]["grade_ceiling"], "A++_review_ceiling")

    def test_price_crash_with_liquidating_bankruptcy_plan_routes_to_a_plus_plus_review(self) -> None:
        rows = triage.build_rows(
            [event("PLAN", "one_day_crash", "price_crash")],
            [passage("PLAN", "The company will file first day motions with the Bankruptcy Court on the Petition Date together with a Liquidating Plan.", "8-K")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "price_cause_review")
        self.assertEqual(rows[0]["grade_ceiling"], "A++_review_ceiling")

    def test_reverse_split_with_contemporaneous_offering_routes_to_dilution_review(self) -> None:
        rows = triage.build_rows(
            [event("DILRS", "reverse_split", "corporate_action")],
            [passage("DILRS", "After the reverse stock split, the company entered a registered direct offering and agreed to sell 506,803 shares.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "recapitalization_dilution_review")
        self.assertEqual(rows[0]["grade_ceiling"], "A_review_ceiling")

    def test_reverse_split_for_listing_compliance_stays_b_ceiling(self) -> None:
        rows = triage.build_rows(
            [event("COMPRS", "reverse_split", "corporate_action")],
            [passage("COMPRS", "The proportionate reverse stock split is intended to regain compliance with the Nasdaq minimum bid price requirement.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "listing_compliance_reverse_split")
        self.assertEqual(rows[0]["grade_ceiling"], "B_review_ceiling")

    def test_expected_reverse_split_date_requires_later_realized_confirmation(self) -> None:
        rows = triage.build_rows(
            [event("EXPECTRS", "reverse_split", "corporate_action")],
            [passage("EXPECTRS", "The company will implement a one-for-75 reverse stock split for minimum bid price compliance and the shares are expected to begin trading on a split-adjusted basis on January 6.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "expected_effective_date_control")
        self.assertEqual(rows[0]["proposed_disposition"], "verify_split_but_do_not_accept_expected_date_as_realized")
        self.assertIn("expected_date_is_not_realized_event_date", rows[0]["reason_codes"])

    def test_filed_amendment_with_exact_effective_time_is_realized_split_evidence(self) -> None:
        rows = triage.build_rows(
            [event("FILEDRS", "reverse_split", "corporate_action")],
            [passage("FILEDRS", "The company filed the amendment with the Secretary of State, and the reverse stock split will become effective at 12:01 a.m. on the event date to regain compliance with the minimum bid price rule.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "listing_compliance_reverse_split")
        self.assertNotIn("expected_date_is_not_realized_event_date", rows[0]["reason_codes"])

    def test_rescinded_unfunded_financing_does_not_upgrade_reverse_split_to_dilution(self) -> None:
        rows = triage.build_rows(
            [event("RESCINDRS", "reverse_split", "corporate_action")],
            [
                passage("RESCINDRS", "The reverse stock split was implemented to regain compliance with the Nasdaq minimum bid price requirement."),
                passage("RESCINDRS", "The securities purchase agreement described notes and warrants, but the company rescinded the issuance because of non-payment of the proceeds."),
            ],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "listing_compliance_reverse_split")
        self.assertEqual(rows[0]["grade_ceiling"], "B_review_ceiling")

    def test_reverse_split_immediately_before_merger_is_transaction_mechanic(self) -> None:
        rows = triage.build_rows(
            [event("MERGERS", "reverse_split", "corporate_action")],
            [passage("MERGERS", "The reverse stock split became effective immediately prior to the merger and name change.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "transaction_mechanic_reverse_split")
        self.assertEqual(rows[0]["grade_ceiling"], "B_review_ceiling")

    def test_reverse_split_condition_to_public_offering_routes_to_dilution_review(self) -> None:
        rows = triage.build_rows(
            [event("OFFERINGRS", "reverse_split", "corporate_action")],
            [passage("OFFERINGRS", "The reverse stock split is a condition to the closing of the offering, a best efforts public offering of 1,454,546 new shares.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "recapitalization_dilution_review")
        self.assertEqual(rows[0]["grade_ceiling"], "A_review_ceiling")

    def test_reverse_split_with_cashless_warrant_financing_routes_to_dilution_review(self) -> None:
        rows = triage.build_rows(
            [event("WARRANTSRS", "reverse_split", "corporate_action")],
            [passage("WARRANTSRS", "The securities purchase agreement included Series A warrants exercisable on an alternate cashless basis.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "recapitalization_dilution_review")

    def test_reverse_split_with_atm_or_prefunded_warrants_routes_to_dilution_review(self) -> None:
        rows = triage.build_rows(
            [event("ATMRS", "reverse_split", "corporate_action")],
            [passage("ATMRS", "The sales agreement permits an at-the-market offering with a $100 million aggregate offering price and pre-funded warrants.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "recapitalization_dilution_review")

    def test_hypothetical_delisting_risk_does_not_turn_prefunded_warrants_into_financing(self) -> None:
        rows = triage.build_rows(
            [event("RISK", "reverse_split", "equity_dilution")],
            [passage("RISK", "If our common stock is delisted, investors may not be able to sell their shares and the value and liquidity of our pre-funded warrants would likely be significantly adversely affected.", "10-Q")],
            [],
        )
        self.assertNotEqual(rows[0]["review_bucket"], "recapitalization_dilution_review")
        self.assertEqual(rows[0]["review_bucket"], "source_mismatch_review")

    def test_court_restructuring_with_new_shares_routes_to_a_plus_plus_review(self) -> None:
        rows = triage.build_rows(
            [event("COURTRS", "reverse_split", "corporate_action")],
            [passage("COURTRS", "The court agreed to sanction the restructuring plan, which issues new ordinary shares to bondholders and converts the loan facility.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "court_restructuring_review")
        self.assertEqual(rows[0]["grade_ceiling"], "A++_review_ceiling")

    def test_no_change_in_control_does_not_trigger_reverse_split_dilution(self) -> None:
        rows = triage.build_rows(
            [event("NOCTRLRS", "reverse_split", "corporate_action")],
            [passage("NOCTRLRS", "The reverse stock split was proportional and the common-control transaction resulted in no change in control.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "ordinary_corporate_action")

    def test_second_reverse_split_for_same_security_routes_to_repeat_review(self) -> None:
        first = event("RS1", "reverse_split", "corporate_action", 1)
        second = event("RS2", "reverse_split", "corporate_action", 2)
        first["stable_id"] = second["stable_id"] = "permaticker:repeat"
        first["event_date"] = "2024-01-01"
        second["event_date"] = "2025-01-01"
        rows = triage.build_rows(
            [first, second],
            [
                passage("RS1", "The reverse stock split was intended to regain compliance with the minimum bid price."),
                passage("RS2", "A second reverse stock split was intended to regain compliance with the minimum bid price."),
            ],
            [],
        )
        by_id = {row["event_candidate_id"]: row for row in rows}
        self.assertEqual(by_id["RS1"]["review_bucket"], "listing_compliance_reverse_split")
        self.assertEqual(by_id["RS2"]["review_bucket"], "repeat_reverse_split_review")
        self.assertEqual(by_id["RS2"]["grade_ceiling"], "A_review_ceiling")

    def test_profitable_buyback_driven_negative_equity_stays_b_ceiling(self) -> None:
        rows = triage.build_rows(
            [event("BUYBACK", "negative_equity", "fundamental_shock")],
            [passage("BUYBACK", "The stockholders' deficit includes treasury stock after a share repurchase. The company reported net income and net cash provided by operating activities.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "fundamental_context")
        self.assertEqual(rows[0]["grade_ceiling"], "B_review_ceiling")
        self.assertIn("capital_return_negative_equity_boundary", rows[0]["reason_codes"])

    def test_negative_equity_with_troubled_debt_and_cash_burn_routes_to_a_review(self) -> None:
        rows = triage.build_rows(
            [event("DISTRESS", "negative_equity", "fundamental_shock")],
            [passage("DISTRESS", "The company has an accumulated deficit and a troubled debt restructuring. Net cash used in operating activities and long-term debt constrain liquidity.")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "negative_equity_distress_review")
        self.assertEqual(rows[0]["grade_ceiling"], "A_review_ceiling")

    def test_revenue_yoy_below_minus_one_is_ratio_artifact(self) -> None:
        candidate = event("REV", "revenue_collapse_yoy", "fundamental_shock")
        candidate["detection_value"] = "-6.25"
        rows = triage.build_rows([candidate], [], [])
        self.assertEqual(rows[0]["review_bucket"], "invalid_revenue_yoy_denominator")
        self.assertEqual(rows[0]["grade_ceiling"], "C_metric_only")

    def test_spac_cash_ratio_is_not_operating_liquidity(self) -> None:
        candidate = event("SPAC", "cash_short_debt_stress", "fundamental_shock")
        candidate["industry"] = "Shell Companies"
        candidate["detection_value"] = "0.0"
        rows = triage.build_rows(
            [candidate],
            [passage("SPAC", "The combination deadline raises substantial doubt about the company's ability to continue as a going concern.", "10-K")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "spac_balance_sheet_boundary")
        self.assertEqual(rows[0]["grade_ceiling"], "B_review_ceiling")

    def test_unstable_margin_with_going_concern_is_reclassified(self) -> None:
        candidate = event("MARGIN", "gross_margin_collapse", "fundamental_shock")
        candidate["detection_value"] = "-5.0"
        rows = triage.build_rows(
            [candidate],
            [passage("MARGIN", "Recurring losses raise substantial doubt about the company's ability to continue as a going concern.", "10-K")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "going_concern_reclassification")
        self.assertEqual(rows[0]["grade_ceiling"], "A_review_ceiling")

    def test_quarter_over_quarter_fcf_turn_is_seasonal_boundary(self) -> None:
        candidate = event("FCF", "free_cash_flow_turn_negative", "fundamental_shock")
        candidate["detection_value"] = "fcf=-100;prev=500"
        rows = triage.build_rows([candidate], [], [])
        self.assertEqual(rows[0]["review_bucket"], "seasonal_cash_flow_boundary")
        self.assertEqual(rows[0]["grade_ceiling"], "B_review_ceiling")

    def test_customary_events_of_default_clause_is_not_actual_default(self) -> None:
        candidate = event("COV", "interest_coverage_below_1", "fundamental_shock")
        rows = triage.build_rows(
            [candidate],
            [passage("COV", "The credit agreement contains customary affirmative and negative covenants and events of default.", "10-Q")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "single_quarter_interest_coverage_boundary")

    def test_explicit_no_default_overrides_hypothetical_immediate_due_language(self) -> None:
        candidate = event("NODEFAULT", "interest_coverage_below_1", "fundamental_shock")
        rows = triage.build_rows(
            [candidate],
            [passage("NODEFAULT", "The notes include customary covenants and events of default after which they may be declared immediately due and payable. No such events have occurred.", "10-Q")],
            [],
        )
        self.assertEqual(rows[0]["review_score"], 42)
        self.assertEqual(rows[0]["proposed_disposition"], "likely_reject_metric_only_default_language")
        self.assertIn("actual_default_explicitly_negated", rows[0]["reason_codes"])
        self.assertEqual(rows[0]["grade_ceiling"], "B_review_ceiling")

    def test_deferred_covenant_measurement_routes_to_relief_boundary(self) -> None:
        rows = triage.build_rows(
            [event("RELIEF", "interest_coverage_below_1", "fundamental_shock")],
            [passage("RELIEF", "The lender modified the debt service covenant to defer measurement through 2027. The company remained in compliance with the replacement covenant.", "10-Q")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "covenant_relief_boundary")
        self.assertEqual(rows[0]["grade_ceiling"], "A_review_ceiling")

    def test_covenant_compliance_and_liquidity_reject_single_quarter_ratio(self) -> None:
        rows = triage.build_rows(
            [event("COMPLY", "interest_coverage_below_1", "fundamental_shock")],
            [passage("COMPLY", "The company was in compliance with all financial covenants and had $78 million available to borrow plus cash and cash equivalents.", "10-Q")],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "interest_coverage_false_positive_control")
        self.assertEqual(rows[0]["proposed_disposition"], "reject_single_quarter_ratio_with_covenant_and_liquidity_support")

    def test_resolving_compliance_passage_beats_higher_scored_ambiguous_passage(self) -> None:
        candidate = event("MULTI", "interest_coverage_below_1", "fundamental_shock")
        rows = triage.build_rows(
            [candidate],
            [
                passage("MULTI", "Net cash provided by operating activities was positive.", "8-K"),
                passage("MULTI", "The company was in compliance with all financial covenants and had substantial available borrowing capacity plus cash and cash equivalents.", "10-Q"),
            ],
            [],
        )
        self.assertEqual(rows[0]["review_bucket"], "interest_coverage_false_positive_control")
        self.assertEqual(rows[0]["form"], "10-Q")


if __name__ == "__main__":
    unittest.main()
