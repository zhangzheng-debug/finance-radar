#!/usr/bin/env python3
"""Rank unreviewed Sharadar research candidates by primary-evidence value."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from review_threads import EVENT_FAMILY_BY_TYPE, review_thread_assignments


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUEUE = ROOT / "data" / "research" / "active_event_research_queue.csv"
DEFAULT_PASSAGES = ROOT / "data" / "research" / "active_event_sec_evidence_passages.csv"
DEFAULT_ADJUDICATIONS = ROOT / "reports" / "active_event_adjudications.csv"
DEFAULT_CSV = ROOT / "data" / "research" / "active_event_review_triage.csv"
DEFAULT_REPORT = ROOT / "reports" / "active_event_review_triage_latest.md"


FIELDS = [
    "review_rank",
    "event_candidate_id",
    "ticker_at_event",
    "event_date",
    "event_family",
    "event_type",
    "queue_rank",
    "review_score",
    "review_bucket",
    "proposed_disposition",
    "grade_ceiling",
    "evidence_readiness",
    "next_action",
    "reason_codes",
    "filing_date",
    "form",
    "filing_document_url",
    "evidence_passage",
    "allowed_use",
]


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def evidence_semantics(event: dict[str, str], passage: dict[str, str] | None) -> dict[str, Any]:
    event_type = event["event_type"]
    text = " ".join(
        [
            str((passage or {}).get("evidence_passage") or ""),
            str((passage or {}).get("matched_keywords") or ""),
        ]
    ).casefold()
    hint = str((passage or {}).get("form_item_match_hint") or "")
    has_passage = bool(text)
    reasons = [f"event_type:{event_type}"]

    if event_type == "bankruptcy_liquidation":
        ticker = event.get("ticker_at_event", "").upper()
        equity_death = any(
            phrase in text
            for phrase in (
                "no consideration",
                "no distribution",
                "will be canceled",
                "will be cancelled",
                "all such interests will be canceled",
                "all such interests will be cancelled",
            )
        )
        redemption = "redemption amount" in text or "right to receive the redemption" in text
        spac_extension_or_trust_redemption = (
            any(
                phrase in text
                for phrase in (
                    "extension amendment",
                    "trust amendment",
                    "initial business combination",
                    "combination deadline",
                    "liquidate the trust account",
                )
            )
            and any(
                phrase in text
                for phrase in (
                    "redeem",
                    "redemption",
                    "public shares",
                    "trust account",
                    "extend the date",
                )
            )
        )
        chapter_7 = "chapter 7" in text
        chapter_11 = "chapter 11" in text or "voluntary petition" in text
        hypothetical_chapter_7 = chapter_7 and any(
            phrase in text
            for phrase in (
                "if no plan can be confirmed",
                "may be converted to cases under chapter 7",
                "may be converted to a case under chapter 7",
                "could be converted to cases under chapter 7",
                "could be converted to a case under chapter 7",
                "hypothetical liquidation",
                "liquidation analysis",
            )
        )
        judicial_management = any(
            phrase in text
            for phrase in (
                "judicial management",
                "judicial manager",
                "interim judicial manager",
            )
        )
        listing_only = any(
            phrase in text
            for phrase in (
                "delisting determination",
                "delisting notice",
                "will be delisted",
                "common stock will be delisted",
                "qualification halt",
                "minimum bid price",
                "low priced stocks rule",
                "trades on otcqb",
                "securities would be suspended",
                "closing bid price",
            )
        )
        bankruptcy_driven_listing_consequence = (
            chapter_11
            and listing_only
            and any(
                phrase in text
                for phrase in (
                    "as a result of the chapter 11",
                    "result of the chapter 11",
                    "after the chapter 11",
                    "following the chapter 11",
                    "received written notice",
                )
            )
        )
        stale_chapter_11_status = (
            chapter_11
            and any(
                phrase in text
                for phrase in (
                    "cases are pending",
                    "case is pending",
                    "pendency of the chapter 11",
                    "while certain chapter 11",
                    "continue operating in the ordinary course while",
                )
            )
            and not any(
                phrase in text
                for phrase in (
                    "filed a voluntary petition",
                    "filed voluntary petitions",
                    "commenced voluntary chapter 11",
                    "commenced the chapter 11 cases",
                )
            )
        )
        later_otc_alias_risk = ticker.endswith("Q")
        actual_debt_default = any(
            phrase in text
            for phrase in (
                "event of default has occurred",
                "currently in default",
                "acceleration notice",
                "limited forbearance",
                "forbearance period",
                "declared immediately due and payable",
                "became immediately due and payable",
            )
        )
        secured_creditor_enforcement = any(
            phrase in text
            for phrase in (
                "private disposition of collateral",
                "ucc sale notice",
                "article 9 of the uniform commercial code",
                "sell all of the collateral",
                "acquire all of the collateral",
            )
        )
        cash_returning_liquidation = (
            any(
                phrase in text
                for phrase in (
                    "final cash liquidating distribution",
                    "cash liquidating distribution",
                    "aggregate cash liquidating distributions",
                )
            )
            and any(
                phrase in text
                for phrase in (
                    "plan of sale and dissolution",
                    "complete liquidation and dissolution",
                    "liquidating trust",
                    "beneficial interest units",
                    "converted into beneficial interests",
                )
            )
        )
        contractual_default_boilerplate = (
            "customary" in text
            and any(phrase in text for phrase in ("events of default", "covenants"))
            and not actual_debt_default
        )
        if equity_death:
            return _result(100, "equity_death_boundary", "verify_if_exact_old_common_treatment", "S_deep_review_only", "confirm_plan_effective_and_old_common_distribution", reasons + ["explicit_equity_death_language"], passage)
        if spac_extension_or_trust_redemption:
            return _result(98, "spac_lifecycle_false_bankruptcy_control", "reject_bankruptcy_reclassify_spac_lifecycle", "B_review_ceiling", "confirm_extension_vote_trust_redemption_cash_consideration_and_absence_of_insolvency_petition", reasons + ["spac_extension_or_trust_redemption_not_bankruptcy"], passage)
        if redemption:
            return _result(94, "false_positive_control", "likely_reject_bankruptcy_label", "B_review_ceiling", "confirm_redemption_consideration_and_spac_liquidation_cause", reasons + ["redemption_consideration_not_zero"], passage)
        if hypothetical_chapter_7:
            return _result(94, "hypothetical_liquidation_control", "reject_hypothetical_chapter_7_event", "B_review_ceiling", "find_actual_petition_conversion_or_plan_effective_event_on_its_true_date", reasons + ["hypothetical_chapter_7_not_actual_conversion"], passage)
        if cash_returning_liquidation:
            return _result(86, "cash_returning_liquidation_boundary", "verify_liquidation_without_equity_death_label", "B_review_ceiling", "confirm_aggregate_common_distribution_trust_unit_conversion_remaining_assets_and_debt", reasons + ["cash_and_trust_interests_returned_to_common_holders"], passage)
        if bankruptcy_driven_listing_consequence:
            return _result(94, "bankruptcy_driven_listing_consequence", "reject_primary_bankruptcy_anchor_reclassify_linked_listing_event", "A_review_ceiling", "recover_actual_petition_date_and_keep_delisting_as_linked_consequence", reasons + ["bankruptcy_driven_listing_is_not_primary_petition", "temporal_anchor_review_required"], passage)
        if later_otc_alias_risk:
            has_petition_evidence = chapter_7 or chapter_11
            return _result(
                97 if has_petition_evidence else 83,
                "event_time_identity_control",
                "verify_bankruptcy_after_recovering_event_time_ticker_and_petition_date" if has_petition_evidence else "do_not_accept_q_suffix_metadata_without_primary_petition_evidence",
                "A++_review_ceiling",
                "resolve_pre_petition_ticker_exact_petition_date_and_later_otc_q_alias_before_labeling",
                reasons + ["q_suffix_may_be_later_otc_alias", "event_time_identity_required", "temporal_anchor_review_required"],
                passage,
            )
        if chapter_7:
            return _result(96, "liquidation_boundary", "verify_chapter_7_and_common_equity_outcome", "S_deep_review_only", "confirm_company_scope_trustee_control_liquidation_and_common_recovery", reasons + ["chapter_7_liquidation_without_full_equity_outcome"], passage)
        if stale_chapter_11_status:
            return _result(91, "stale_bankruptcy_status_control", "reject_repeated_bankruptcy_status_as_new_event", "B_review_ceiling", "recover_actual_petition_date_and_separate_any_new_milestone", reasons + ["pending_case_status_not_new_petition", "temporal_anchor_review_required"], passage)
        if chapter_11:
            return _result(92, "bankruptcy_boundary", "verify_bankruptcy_not_equity_death", "A++_review_ceiling", "find_plan_disclosure_statement_and_old_common_treatment", reasons + ["chapter_11_without_equity_outcome"], passage)
        if judicial_management:
            return _result(95, "court_insolvency_boundary", "verify_judicial_management_and_equity_outcome", "A++_review_ceiling", "confirm_court_order_director_power_transfer_restructuring_plan_and_old_common_treatment", reasons + ["court_supervised_judicial_management_without_equity_outcome"], passage)
        if secured_creditor_enforcement:
            return _result(90, "secured_creditor_enforcement_boundary", "reject_bankruptcy_reclassify_secured_creditor_enforcement", "A++_review_ceiling", "confirm_collateral_scope_sale_closing_debt_release_and_remaining_operating_assets", reasons + ["article_9_collateral_disposition_not_bankruptcy"], passage)
        if actual_debt_default:
            return _result(88, "debt_default_boundary", "reject_bankruptcy_reclassify_debt_default", "A_review_ceiling", "confirm_no_insolvency_petition_in_event_window_and_record_acceleration_forbearance_or_foreclosure", reasons + ["debt_default_or_forbearance_not_bankruptcy"], passage)
        if listing_only:
            return _result(90, "false_positive_control", "reject_bankruptcy_reclassify_listing_event", "A_review_ceiling", "confirm_no_insolvency_filing_and_record_actual_listing_cause", reasons + ["listing_event_not_bankruptcy"], passage)
        if contractual_default_boilerplate:
            return _result(44, "contract_boilerplate_control", "likely_reject_bankruptcy_keyword_match", "C_review_ceiling", "confirm_no_actual_default_or_insolvency_petition_in_event_window", reasons + ["customary_default_clause_not_actual_bankruptcy"], passage)
        if "1.03" in hint:
            return _result(82, "bankruptcy_boundary", "read_strong_form_match", "A++_review_ceiling", "open_item_1_03_and_find_equity_outcome", reasons + ["strong_item_1_03_match"], passage)
        return _result(58, "source_mismatch_review", "possible_false_positive", "B_review_ceiling", "find_court_or_company_evidence_or_reject_metadata_candidate", reasons + ["no_bankruptcy_evidence_in_selected_filings"], passage)

    if event_type in {"delisted", "voluntarydelisting"}:
        bankruptcy_cause = any(
            phrase in text
            for phrase in ("chapter 11", "voluntary petition", "bankruptcy court")
        )
        merger = (
            "merger consideration" in text
            or "cash consideration" in text
            or (
                "converted into the right" in text
                and any(phrase in text for phrase in ("merger", "business combination"))
            )
            or any(
                phrase in text
                for phrase in (
                    "controls more than 90 percent",
                    "offers to purchase all of the outstanding",
                    "compulsory redemption",
                    "compulsory buy-out",
                )
            )
        )
        cashout_mechanics = any(
            phrase in text
            for phrase in (
                "going dark transaction",
                "cash-out price",
                "cash out price",
                "cashed out",
                "cash in lieu of fractional shares",
            )
        ) or (
            "reverse stock split" in text
            and "fractional shares" in text
            and any(phrase in text for phrase in ("cash", "payment", "receive"))
        )
        going_dark = cashout_mechanics and any(
            phrase in text
            for phrase in ("delist", "deregister", "reporting obligations")
        )
        unit_transition = event.get("ticker_at_event", "").upper().endswith("U") and any(
            phrase in text
            for phrase in (
                "units ceased trading",
                "common stock began trading",
                "began trading on nasdaq",
                "new trading symbol",
                "reclassified into",
            )
        )
        voluntary_cost = any(
            phrase in text
            for phrase in (
                "low trading volume",
                "limited public shareholder base",
                "costs and expenses associated",
                "reporting requirements",
                "reporting obligations",
                "regulatory burdens",
                "dual listing",
                "concentrate trading",
                "substantial majority of trading volume",
                "cost and regulatory",
                "administrative burden",
                "lack of an active trading market",
                "quoted on the otcqx",
                "trade over-the-counter",
                "begin to trade over-the-counter",
                "reduce our expenses",
                "costs of maintaining",
                "demands on management",
                "simplify corporate structure",
                "operational efficiency",
                "streamline regulatory reporting",
                "reduced free float",
                "concentrating its float",
                "natural market",
                "reduce its cost base",
                "very limited trading volume",
                "considerable costs associated with maintaining the listing",
            )
        )
        home_market_continuity = any(
            phrase in text
            for phrase in (
                "remain listed",
                "sole primary listing",
                "spanish stock exchange",
                "euronext paris",
                "australian securities exchange",
                "six swiss exchange",
                "toronto stock exchange",
                "trade on the tsx",
                "consolidate trading liquidity",
                "continue to be listed and traded",
                "principal trading market",
                "maintain its ads program",
            )
        )
        weak_market_access = any(
            phrase in text
            for phrase in (
                "low trading volume",
                "limited public shareholder base",
                "lack of an active trading market",
                "likely future non-compliance",
                "very limited trading volume",
            )
        )
        noncompliance = any(
            phrase in text
            for phrase in (
                "listing rule",
                "determination letter",
                "delisting determination",
                "non-compliance",
                "noncompliance",
                "suspend trading",
                "minimum bid price",
                "minimum stockholders' equity",
                "minimum stockholders’ equity",
                "scheduled for delisting",
                "suspended at the opening",
            )
        )
        spac_deadline = (
            any(
                phrase in text
                for phrase in ("special purpose acquisition company", "initial business combination")
            )
            and any(
                phrase in text
                for phrase in ("within 36 months", "deadline", "did not complete")
            )
        )
        form_25 = str((passage or {}).get("form") or "") in {"25", "25-NSE", "15-12B", "15-12G"}
        if bankruptcy_cause:
            return _result(96, "bankruptcy_driven_delisting", "verify_delisting_link_to_bankruptcy_chain", "A++_review_ceiling", "link_to_primary_bankruptcy_event_and_avoid_duplicate_hard_label", reasons + ["bankruptcy_is_direct_delisting_cause", "cross_family_event_chain_dedup_required"], passage)
        if merger:
            return _result(98, "false_positive_control", "likely_reject_negative_delisting", "B_review_ceiling", "confirm_per_share_consideration_and_completed_merger", reasons + ["merger_consideration_delisting"], passage)
        if going_dark:
            return _result(86, "going_dark_review", "verify_cashout_and_remaining_holder_treatment", "B_review_ceiling", "confirm_reverse_forward_split_ratio_cashout_price_and_post_delisting_market", reasons + ["going_dark_reverse_split_cashout"], passage)
        if unit_transition:
            return _result(99, "false_positive_control", "likely_reject_negative_delisting", "B_review_ceiling", "confirm_spac_unit_termination_and_successor_common_listing", reasons + ["spac_unit_or_ticker_transition"], passage)
        if spac_deadline:
            return _result(93, "false_positive_control", "reject_negative_delisting_reclassify_spac_lifecycle", "B_review_ceiling", "confirm_trust_redemption_and_final_spac_winding_up_terms", reasons + ["spac_combination_deadline_delisting"], passage)
        if noncompliance:
            return _result(88, "delisting_cause_review", "negative_cause_needs_outcome", "A_review_ceiling", "confirm_final_delisting_suspension_and_remediation_status", reasons + ["listing_noncompliance_language"], passage)
        if weak_market_access and not home_market_continuity:
            return _result(87, "market_access_contraction", "verify_material_voluntary_delisting", "A_review_ceiling", "confirm_replacement_market_certainty_liquidity_and_reporting_continuity", reasons + ["weak_liquidity_or_listing_access_without_home_market_continuity"], passage)
        if home_market_continuity:
            return _result(84, "voluntary_listing_exit", "verify_home_market_continuity", "B_review_ceiling", "confirm_home_market_or_otc_continuity_and_ads_holder_treatment", reasons + ["home_market_continuity_after_us_delisting"], passage)
        if voluntary_cost:
            return _result(82, "voluntary_listing_exit", "verify_ordinary_voluntary_delisting", "B_review_ceiling", "confirm_continued_otc_or_other_exchange_access_and_no_forced_cause", reasons + ["voluntary_cost_liquidity_or_listing_consolidation"], passage)
        if form_25 or "form 25" in text:
            return _result(66, "delisting_cause_review", "cause_unresolved", "A_review_ceiling", "find_merger_voluntary_noncompliance_or_bankruptcy_cause", reasons + ["form_25_only"], passage)
        return _result(54, "source_mismatch_review", "possible_false_positive", "B_review_ceiling", "find_exchange_or_company_delisting_cause", reasons + ["no_delisting_cause_in_selected_filings"], passage)

    if event["event_family"] == "price_crash":
        resolved_compliance = any(
            phrase in text
            for phrase in ("full repayment", "matter regarding", "matter is closed", "regained compliance")
        )
        dilution = any(
            phrase in text
            for phrase in ("warrant exchange", "exchange shares", "extreme issuance", "highly dilutive")
        ) or (
            any(phrase in text for phrase in ("pre-funded warrants", "resale by selling shareholders"))
            and any(phrase in text for phrase in ("issuable upon exercise", "common shares", "common stock"))
        )
        executed_equity_offering = (
            any(phrase in text for phrase in ("announces pricing", "pricing of", "offering closed", "closed on"))
            and any(phrase in text for phrase in ("public offering", "registered direct offering"))
            and any(phrase in text for phrase in ("ordinary shares", "common shares", "common stock"))
        )
        structural_dilution = (
            any(phrase in text for phrase in ("share consolidation", "reverse stock split", "reverse split"))
            and any(phrase in text for phrase in ("increase in the company’s authorized share capital", "increase of authorized shares", "creation of an additional"))
        )
        contractual_default_boilerplate = (
            "customary" in text
            and any(phrase in text for phrase in ("events of default", "bankruptcy or insolvency events"))
            and not any(
                phrase in text
                for phrase in (
                    "default has occurred",
                    "event of default occurred",
                    "failed to pay",
                    "payment default",
                    "accelerated the",
                    "voluntary petition",
                    "filed for bankruptcy",
                )
            )
        )
        if resolved_compliance:
            return _result(84, "false_positive_control", "retain_price_only_reject_negative_cause", "C_price_only", "confirm_resolution_precedes_or_matches_price_event_and_reject_stale_delisting_cause", reasons + ["official_resolution_or_compliance_restoration", "temporal_alignment_required"], passage)
        if structural_dilution:
            return _result(88, "structural_dilution_cause_review", "possible_structural_dilution_cause", "A_review_ceiling", "confirm_effective_split_ratio_authorized_share_expansion_and_shareholder_approval", reasons + ["primary_split_and_authorized_share_expansion_language", "temporal_alignment_required"], passage)
        if dilution or executed_equity_offering:
            return _result(86, "dilution_cause_review", "possible_extreme_dilution_cause", "A_review_ceiling", "calculate_new_shares_post_money_share_count_and_effective_date", reasons + ["primary_dilution_language", "temporal_alignment_required"], passage)
        regulatory_suspension = any(
            phrase in text
            for phrase in (
                "specially designated nationals",
                "office of foreign assets control",
                "ofac",
            )
        ) and any(phrase in text for phrase in ("trading in", "trading has been suspended", "suspend"))
        if regulatory_suspension:
            return _result(92, "regulatory_trading_suspension", "possible_regulatory_suspension_cause", "A++_review_ceiling", "confirm_official_designation_suspension_date_and_entity_scope", reasons + ["primary_regulatory_suspension_language", "temporal_alignment_required"], passage)
        hard_cause = any(phrase in text for phrase in ("chapter 11", "voluntary petition", "will be delisted", "appointed receiver", "receivership")) or sum(
            phrase in text for phrase in ("bankruptcy court", "petition date", "liquidating plan")
        ) >= 2
        if hard_cause:
            return _result(90, "price_cause_review", "possible_hard_event_cause", "A++_review_ceiling", "confirm_cause_effective_time_precedes_price_event_and_separate_subsidiary_scope", reasons + ["primary_hard_event_language", "temporal_alignment_required"], passage)
        liquidity_distress = any(phrase in text for phrase in ("going concern", "substantial doubt")) and any(
            phrase in text
            for phrase in ("insufficient cash", "cash resources", "cash and cash equivalents", "financial resources")
        )
        if liquidity_distress:
            return _result(82, "liquidity_distress_cause_review", "possible_liquidity_distress_cause", "A_review_ceiling", "confirm_filing_precedes_price_episode_and_quantify_cash_runway_without_using_returns", reasons + ["primary_liquidity_distress_language", "temporal_alignment_required"], passage)
        if contractual_default_boilerplate:
            return _result(26, "contract_boilerplate_control", "retain_price_only_reject_negative_cause", "C_price_only", "confirm_no_actual_default_or_insolvency_event_in_event_window", reasons + ["customary_default_clause_not_actual_default", "temporal_alignment_required"], passage)
        if any(phrase in text for phrase in ("default", "investigation", "delist")):
            return _result(72, "price_cause_review", "possible_material_cause", "A_review_ceiling", "confirm_same_entity_current_event_and_effective_time", reasons + ["possible_cause_language", "temporal_alignment_required"], passage)
        return _result(28, "price_only_control", "retain_price_only_or_reject", "C_price_only", "search_non_price_primary_evidence_without_using_returns", reasons + ["no_primary_cause_found"], passage)

    if event_type == "reverse_split":
        prior_reverse_split_count = int(event.get("prior_reverse_split_count") or 0)
        try:
            split_event_date = datetime.fromisoformat(event.get("event_date", "")).date()
            evidence_filing_date = datetime.fromisoformat((passage or {}).get("filing_date", "")).date()
            passage_delta_days: int | None = (evidence_filing_date - split_event_date).days
        except ValueError:
            passage_delta_days = None
        split_confirmed = "reverse stock split" in text or "reverse split" in text or "share consolidation" in text
        filed_legal_effectiveness_instrument = (
            any(
                phrase in text
                for phrase in (
                    "filed the amendment",
                    "filed a certificate of amendment",
                    "filed the certificate of amendment",
                )
            )
            and "secretary of state" in text
            and any(phrase in text for phrase in ("effective time", "will become effective", "became effective"))
        )
        expected_effective_date_only = (
            split_confirmed
            and any(
                phrase in text
                for phrase in (
                    "expected to begin trading",
                    "expects to begin trading",
                    "will begin trading",
                    "anticipated to begin trading",
                    "expected to become effective",
                    "will become effective",
                    "intends to be effective",
                )
            )
            and not any(
                phrase in text
                for phrase in (
                    "began trading",
                    "commenced trading",
                    "became effective",
                    "implemented a",
                    "effected the",
                    "completed a",
                )
            )
            and not filed_legal_effectiveness_instrument
        )
        positive_change_in_control = "change in control" in text and not any(
            phrase in text
            for phrase in ("no change in control", "does not constitute a change in control", "does not result in a change in control")
        )
        hypothetical_market_risk_only = any(
            phrase in text
            for phrase in (
                "if our common stock is delisted",
                "would likely be significantly adversely affected",
                "may not be able to sell their shares",
            )
        ) and not any(
            phrase in text
            for phrase in (
                "entered into a securities purchase agreement",
                "agreed to issue and sell",
                "we are offering",
                "offering closed",
                "fully exercised",
                "issuable upon exercise",
            )
        )
        financing_language = any(
            phrase in text
            for phrase in (
                "registered direct offering",
                "at-the-market offering",
                "underwritten public offering",
                "best efforts public offering",
                "pre-funded warrants",
                "aggregate offering price",
                "agreed to sell",
                "converted notes",
                "notes converted",
                "potential issuance of an excess of 19.99%",
                "in excess of 19.99%",
                "alternate cashless basis",
                "cashless warrants",
                "condition to the closing of the offering",
            )
        ) or positive_change_in_control or (
            "securities purchase agreement" in text
            and any(phrase in text for phrase in ("warrant", "offering", "issuance of securities"))
        )
        financing_rescinded = any(
            phrase in text
            for phrase in (
                "rescinded the issuance",
                "issuance has been rescinded",
                "non-payment of the proceeds",
                "proceeds were not paid",
                "did not receive the proceeds",
            )
        )
        distant_financing_context = (
            financing_language
            and passage_delta_days is not None
            and abs(passage_delta_days) > 30
        )
        dilutive_recapitalization = (
            not hypothetical_market_risk_only
            and financing_language
            and not financing_rescinded
            and not distant_financing_context
        )
        court_restructuring = "restructuring plan" in text and any(
            phrase in text for phrase in ("court", "sanction")
        ) and any(
            phrase in text
            for phrase in ("new ordinary shares", "bondholders", "conversion of its loan facility")
        )
        transaction_mechanic = any(
            phrase in text
            for phrase in ("merger agreement", "immediately prior to the merger", "name change")
        )
        listing_compliance = any(
            phrase in text
            for phrase in (
                "minimum bid price",
                "regain compliance",
                "maintaining its listing",
                "continued listing",
            )
        )
        proportional_action = any(
            phrase in text
            for phrase in (
                "relative interest",
                "proportionate reduction",
                "affected all stockholders uniformly",
                "proportionately adjusted",
            )
        )
        if court_restructuring:
            return _result(94, "court_restructuring_review", "verify_old_common_dilution_and_rescue_terms", "A++_review_ceiling", "calculate_old_common_post_plan_ownership_debt_conversion_new_money_and_ads_ratio_change", reasons + ["court_sanctioned_restructuring", "temporal_alignment_required"], passage)
        if distant_financing_context:
            return _result(74, "separate_financing_event", "verify_split_only_and_create_separate_financing_event", "B_review_ceiling", "adjudicate_reverse_split_at_its_effective_date_and_financing_at_its_own_agreement_or_closing_date", reasons + ["financing_context_outside_30_day_split_window", f"filing_delta_days:{passage_delta_days}", "future_leakage_prevention"], passage)
        if dilutive_recapitalization:
            return _result(88, "recapitalization_dilution_review", "verify_financing_and_post_split_dilution", "A_review_ceiling", "calculate_post_split_old_common_new_issuance_fully_diluted_ownership_and_control_change", reasons + ["contemporaneous_financing_or_conversion", "temporal_alignment_required"], passage)
        if transaction_mechanic:
            return _result(76, "transaction_mechanic_reverse_split", "verify_transaction_mechanics", "B_review_ceiling", "record_split_ratio_merger_exchange_ratio_name_change_and_holder_continuity", reasons + ["reverse_split_transaction_mechanic"], passage)
        if prior_reverse_split_count:
            return _result(84, "repeat_reverse_split_review", "verify_failed_prior_remediation_and_new_capital_structure", "A_review_ceiling", "compare_prior_and_current_ratios_listing_deficiencies_authorized_share_headroom_and_intervening_issuance", reasons + ["prior_reverse_split_same_security", f"prior_reverse_split_count:{prior_reverse_split_count}"], passage)
        if expected_effective_date_only:
            return _result(86, "expected_effective_date_control", "verify_split_but_do_not_accept_expected_date_as_realized", "B_review_ceiling", "find_later_issuer_exchange_or_transfer_agent_confirmation_of_actual_effective_and_split_adjusted_trading_dates", reasons + ["expected_date_is_not_realized_event_date", "temporal_anchor_review_required"], passage)
        if listing_compliance:
            return _result(72, "listing_compliance_reverse_split", "verify_listing_remediation", "B_review_ceiling", "record_ratio_effective_time_listing_rule_and_subsequent_compliance_without_treating_split_as_dilution", reasons + ["listing_compliance_remediation"], passage)
        if split_confirmed:
            reason = "proportional_reverse_split" if proportional_action else "reverse_split_confirmed"
            return _result(52, "ordinary_corporate_action", "verify_action_only", "B_review_ceiling", "record_ratio_effective_time_authorized_share_treatment_and_any_separate_financing", reasons + [reason], passage)
        return _result(30, "source_mismatch_review", "possible_false_positive", "C_review_ceiling", "find_charter_or_exchange_confirmation", reasons + ["no_reverse_split_passage"], passage)

    if event["event_family"] == "fundamental_shock":
        if event_type == "negative_equity" and any(
            phrase in text
            for phrase in ("stockholders' deficit", "shareholders' deficit", "negative equity", "accumulated deficit")
        ):
            debt_distress = "troubled debt restructuring" in text or "substantial doubt" in text or (
                "net cash used in operating activities" in text
                and any(phrase in text for phrase in ("long-term debt", "liquidity", "covenant"))
            )
            if debt_distress:
                return _result(82, "negative_equity_distress_review", "verify_debt_restructuring_cash_burn_and_dilution", "A_review_ceiling", "quantify_cash_debt_operating_burn_debt_exchange_atm_capacity_and_twelve_month_liquidity_statement", reasons + ["negative_equity_with_debt_or_liquidity_stress"], passage)
            capital_return = any(
                phrase in text for phrase in ("treasury stock", "share repurchase", "repurchased")
            ) and any(
                phrase in text for phrase in ("net income", "net cash provided by operating activities")
            )
            return _result(52 if capital_return else 48, "fundamental_context", "verify_metric_not_severity", "B_review_ceiling", "confirm_point_in_time_value_and_distinguish_buybacks_from_distress", reasons + (["capital_return_negative_equity_boundary"] if capital_return else ["accounting_deficit_language"]), passage)
        try:
            metric_value = float(str(event.get("detection_value") or "").split(";", 1)[0].replace("fcf=", ""))
        except ValueError:
            metric_value = None
        sector = str(event.get("sector") or "")
        industry = str(event.get("industry") or "")
        going_concern = "substantial doubt" in text and "going concern" in text
        merger_consideration = "merger consideration" in text or (
            "converted into the right to receive" in text and "cash" in text
        )
        no_actual_default = any(
            phrase in text
            for phrase in (
                "no such events have occurred",
                "no event of default has occurred",
                "was not in default",
                "were not in default",
            )
        )
        contractual_default_boilerplate = (
            "customary" in text
            and any(phrase in text for phrase in ("events of default", "covenants"))
        )
        explicit_covenant_compliance = any(
            phrase in text
            for phrase in (
                "in compliance with all financial covenants",
                "was in compliance with all financial covenants",
                "in compliance with this covenant",
                "in compliance with the financial covenant",
                "were in compliance with all covenants",
            )
        )
        covenant_relief = any(
            phrase in text
            for phrase in (
                "defer measurement",
                "covenant relief period",
                "waiver of the financial covenant",
                "waived compliance with",
            )
        )
        covenant_stress = not no_actual_default and any(
            phrase in text
            for phrase in (
                "event of default has occurred",
                "currently in default",
                "covenant violation",
                "acceleration notice",
                "declared immediately due and payable",
                "became immediately due and payable",
            )
        )
        sufficient_liquidity = any(
            phrase in text
            for phrase in ("capital resources are sufficient", "robust capital, liquidity", "sufficient liquidity")
        )
        if event_type == "revenue_collapse_yoy":
            if merger_consideration:
                return _result(78, "corporate_transaction_metric_break", "reject_revenue_collapse_reclassify_merger", "C_metric_only", "record_merger_consideration_and exclude_post_transaction_revenue_ratio", reasons + ["merger_breaks_revenue_comparability"], passage)
            if metric_value is not None and metric_value < -1:
                return _result(74, "invalid_revenue_yoy_denominator", "likely_reject_ratio_artifact", "C_metric_only", "verify_prior_period_revenue_sign_restatement_and_business_model_before_any_label", reasons + ["revenue_yoy_below_minus_100_implies_negative_or_incomparable_prior_denominator"], passage)
            if sector == "Financial Services" or industry == "REIT - Mortgage":
                return _result(62, "sector_metric_mismatch", "likely_reject_generic_revenue_collapse", "B_review_ceiling", "use_net_interest_income_book_value_and_credit_metrics_instead_of_generic_revenue", reasons + ["financial_business_model_requires_sector_metrics"], passage)
        if event_type == "gross_margin_collapse":
            if going_concern:
                return _result(82, "going_concern_reclassification", "reject_margin_metric_reclassify_fundamental_distress", "A_review_ceiling", "verify_cash_runway_recurring_losses_financing_plan_and_auditor_language", reasons + ["going_concern_is_stronger_than_unstable_margin_ratio"], passage)
            if metric_value is not None and metric_value < -2:
                return _result(70, "unstable_gross_margin_denominator", "likely_reject_ratio_artifact", "C_metric_only", "verify_positive_material_revenue_in_both_year_ago_and_current_quarters", reasons + ["gross_margin_delta_below_minus_200pp_is_pre_revenue_or_denominator_instability"], passage)
        if event_type == "cash_short_debt_stress":
            if industry == "Shell Companies":
                return _result(68, "spac_balance_sheet_boundary", "reject_operating_cash_stress_or_reclassify_spac_deadline", "B_review_ceiling", "separate_trust_account_restricted_cash_founder_working_capital_and_combination_deadline", reasons + ["spac_cash_to_short_debt_ratio_not_operating_liquidity"], passage)
            if sector in {"Financial Services", "Utilities"}:
                return _result(62, "sector_balance_sheet_boundary", "likely_reject_generic_cash_short_debt_stress", "B_review_ceiling", "use_sector_specific_regulatory_capital_deposit_funding_or_rate_base_metrics", reasons + ["sector_balance_sheet_ratio_mismatch"], passage)
            if going_concern:
                return _result(82, "liquidity_distress_review", "possible_fundamental_distress", "A_review_ceiling", "verify_unrestricted_cash_short_term_obligations_operating_burn_and_financing_plan", reasons + ["primary_going_concern_language"], passage)
            if sufficient_liquidity:
                return _result(58, "liquidity_false_positive_control", "likely_reject_cash_ratio_stress", "B_review_ceiling", "confirm_revolver_access_cash_generation_and_no_covenant_default", reasons + ["management_states_sufficient_liquidity"], passage)
        if event_type == "interest_coverage_below_1":
            if going_concern or covenant_stress:
                return _result(82, "debt_service_distress_review", "possible_fundamental_distress", "A_review_ceiling", "verify_ltm_ebitda_cash_interest_covenant_headroom_and_default_status", reasons + ["primary_debt_or_going_concern_stress"], passage)
            if covenant_relief:
                return _result(78, "covenant_relief_boundary", "verify_financing_dependency_not_default", "A_review_ceiling", "quantify_deferred_covenant_tests_ltm_debt_service_operating_cash_use_and_remaining_revolver_capacity", reasons + ["lender_deferred_financial_covenant_measurement_without_default"], passage)
            if explicit_covenant_compliance and any(
                phrase in text
                for phrase in (
                    "available to borrow",
                    "available borrowing capacity",
                    "net cash provided by operating activities",
                    "sufficient to fund our operating and capital needs",
                    "cash and cash equivalents",
                )
            ):
                return _result(40, "interest_coverage_false_positive_control", "reject_single_quarter_ratio_with_covenant_and_liquidity_support", "B_review_ceiling", "retain_ltm_coverage_calculation_as_audit_only_and_record_explicit_covenant_compliance", reasons + ["explicit_covenant_compliance_and_liquidity_support"], passage)
            if no_actual_default or contractual_default_boilerplate:
                reason = "actual_default_explicitly_negated" if no_actual_default else "customary_default_clause_not_actual_default"
                return _result(42, "single_quarter_interest_coverage_boundary", "likely_reject_metric_only_default_language", "B_review_ceiling", "recalculate_ltm_cash_interest_coverage_and_confirm_no_payment_or_covenant_default", reasons + [reason, "single_quarter_negative_ebit_ratio_is_not_default"], passage)
            if "adjusted ebitda" in text and "free cash flow" in text:
                return _result(58, "non_gaap_coverage_boundary", "likely_reject_single_quarter_coverage_signal", "B_review_ceiling", "recalculate_ltm_cash_interest_coverage_and_reconcile_adjusted_ebitda", reasons + ["positive_or_mitigating_non_gaap_operating_context"], passage)
            return _result(50, "single_quarter_interest_coverage_boundary", "retain_only_for_ltm_debt_review", "B_review_ceiling", "require_ltm_cash_interest_covenant_and_liquidity_evidence_before_negative_label", reasons + ["single_quarter_negative_ebit_ratio_is_not_default"], passage)
        if event_type == "free_cash_flow_turn_negative":
            if going_concern or covenant_stress:
                return _result(80, "cash_flow_distress_review", "possible_fundamental_distress", "A_review_ceiling", "verify_ltm_free_cash_flow_liquidity_debt_service_and_management_remediation", reasons + ["cash_flow_change_with_primary_distress_language"], passage)
            return _result(56, "seasonal_cash_flow_boundary", "likely_reject_quarter_over_quarter_fcf_signal", "B_review_ceiling", "compare_same_quarter_year_over_year_and_ltm_free_cash_flow_before_any_label", reasons + ["detector_uses_previous_quarter_not_seasonally_comparable_period"], passage)
        return _result(24, "low_evidence_fundamental", "retain_candidate", "C_review_ceiling", "open_point_in_time_financial_statements", reasons + ["no_metric_passage"], passage)

    return _result(35 if has_passage else 20, "manual_context", "retain_candidate", "B_review_ceiling", "manual_primary_evidence_review", reasons, passage)


def _result(
    score: int,
    bucket: str,
    disposition: str,
    ceiling: str,
    action: str,
    reasons: list[str],
    passage: dict[str, str] | None,
) -> dict[str, Any]:
    return {
        "score": score,
        "bucket": bucket,
        "disposition": disposition,
        "ceiling": ceiling,
        "action": action,
        "reasons": reasons,
        "passage": passage,
    }


UNRESOLVED_REVIEW_BUCKETS = {
    "delisting_cause_review",
    "low_evidence_fundamental",
    "manual_context",
    "single_quarter_interest_coverage_boundary",
    "source_mismatch_review",
}


def decision_resolution_rank(decision: dict[str, Any]) -> int:
    """Prefer evidence that resolves the detected event over higher-severity ambiguity."""
    if decision["bucket"] in UNRESOLVED_REVIEW_BUCKETS:
        return 0
    return 1


def evidence_alignment_rank(event: dict[str, str], passage: dict[str, str] | None) -> tuple[int, int, int]:
    """Prefer the point-in-time statement that actually covers a fundamental period."""
    if not passage:
        return (0, -1, -10**9)
    try:
        filing_date = datetime.fromisoformat(passage.get("filing_date", "")).date()
        event_date = datetime.fromisoformat(event.get("event_date", "")).date()
        delta = (filing_date - event_date).days
    except ValueError:
        delta = 10**6
    if event.get("event_family") != "fundamental_shock":
        return (0, 0, -abs(delta))
    form = passage.get("form", "").upper()
    periodic = int(form in {"10-Q", "10-K", "20-F", "40-F"})
    timely_post_period = int(0 <= delta <= 90)
    return (periodic, timely_post_period, -abs(delta))


def build_rows(
    queue_rows: Iterable[dict[str, str]],
    passage_rows: Iterable[dict[str, str]],
    adjudication_rows: Iterable[dict[str, str]],
) -> list[dict[str, Any]]:
    queue_rows = list(queue_rows)
    adjudication_rows = list(adjudication_rows)
    reviewed = {row["event_candidate_id"] for row in adjudication_rows}
    queue_by_id = {row["event_candidate_id"]: row for row in queue_rows}
    thread_input = list(queue_rows)
    for row in adjudication_rows:
        event_id = row.get("event_candidate_id", "")
        family = EVENT_FAMILY_BY_TYPE.get(row.get("detected_event_type", ""), "")
        if (
            event_id
            and event_id not in queue_by_id
            and row.get("stable_id")
            and row.get("event_date")
            and family
        ):
            thread_input.append(
                {
                    "event_candidate_id": event_id,
                    "stable_id": row["stable_id"],
                    "event_date": row["event_date"],
                    "event_family": family,
                    "queue_rank": "0",
                }
            )
    thread_by_id = review_thread_assignments(thread_input)
    reviewed_groups: set[tuple[str, str, str]] = set()
    for row in adjudication_rows:
        event_id = row.get("event_candidate_id", "")
        if event_id in thread_by_id:
            reviewed_groups.add(thread_by_id[event_id])
    by_event: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in passage_rows:
        by_event[row["event_candidate_id"]].append(row)

    reverse_dates_by_security: dict[str, list[str]] = defaultdict(list)
    for queued in queue_rows:
        if queued.get("event_type") == "reverse_split":
            security = queued.get("stable_id") or queued["event_candidate_id"]
            reverse_dates_by_security[security].append(queued.get("event_date", ""))

    grouped_output: dict[tuple[str, str, str], dict[str, Any]] = {}
    for event in queue_rows:
        group_key = thread_by_id[event["event_candidate_id"]]
        if event["event_candidate_id"] in reviewed or group_key in reviewed_groups:
            continue
        event_for_review = dict(event)
        if event.get("event_type") == "reverse_split":
            security = event.get("stable_id") or event["event_candidate_id"]
            event_for_review["prior_reverse_split_count"] = str(
                sum(1 for value in reverse_dates_by_security[security] if value < event.get("event_date", ""))
            )
        candidates = by_event.get(event["event_candidate_id"], []) or [None]
        decisions = [evidence_semantics(event_for_review, passage) for passage in candidates]
        decision = max(
            decisions,
            key=lambda item: (
                decision_resolution_rank(item),
                item["score"],
                evidence_alignment_rank(event_for_review, item["passage"]),
                int((item["passage"] or {}).get("passage_score") or 0),
            ),
        )
        passage = decision["passage"] or {}
        record = {
                "event_candidate_id": event["event_candidate_id"],
                "ticker_at_event": event["ticker_at_event"],
                "event_date": event["event_date"],
                "event_family": event["event_family"],
                "event_type": event["event_type"],
                "queue_rank": int(event["queue_rank"]),
                "review_score": decision["score"],
                "review_bucket": decision["bucket"],
                "proposed_disposition": decision["disposition"],
                "grade_ceiling": decision["ceiling"],
                "evidence_readiness": "primary_passage_ready" if passage.get("evidence_passage") else "filing_link_only" if passage else "no_sec_candidate_yet",
                "next_action": decision["action"],
                "reason_codes": ";".join(decision["reasons"]),
                "filing_date": passage.get("filing_date", ""),
                "form": passage.get("form", ""),
                "filing_document_url": passage.get("filing_document_url", ""),
                "evidence_passage": passage.get("evidence_passage", ""),
                "allowed_use": "manual_review_priority_only_no_trading_no_auto_label",
            }
        existing = grouped_output.get(group_key)
        if existing is None or (record["review_score"], -record["queue_rank"]) > (
            existing["review_score"],
            -existing["queue_rank"],
        ):
            grouped_output[group_key] = record
    output = list(grouped_output.values())
    output.sort(key=lambda row: (-row["review_score"], row["queue_rank"], row["event_candidate_id"]))
    for rank, row in enumerate(output, 1):
        row["review_rank"] = rank
    return output


def write_outputs(rows: list[dict[str, Any]], csv_path: Path, report_path: Path) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    counts = Counter(row["review_bucket"] for row in rows)
    generated_at = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Active Historical Review Triage",
        "",
        f"Generated: `{generated_at}`",
        "",
        f"- Unreviewed queue rows ranked: `{len(rows)}`",
        "- Inputs: Sharadar candidate metadata plus contemporaneous SEC evidence passages.",
        "- Forbidden inputs: post-event return, drawdown, recovery, and future delisting outcome.",
        "- Review score is workload priority only; it cannot mutate labels or enable trading.",
        "",
        "## Buckets",
        "",
    ]
    lines.extend(f"- {key}: `{value}`" for key, value in sorted(counts.items()))
    lines.extend(
        [
            "",
            "## Top 20",
            "",
            "| rank | score | ticker | date | detected | bucket | proposed disposition | ceiling | evidence |",
            "|---:|---:|---|---|---|---|---|---|---|",
        ]
    )
    for row in rows[:20]:
        evidence = f"[SEC]({row['filing_document_url']})" if row["filing_document_url"] else row["evidence_readiness"]
        lines.append(
            f"| {row['review_rank']} | {row['review_score']} | {row['ticker_at_event']} | "
            f"{row['event_date']} | {row['event_type']} | {row['review_bucket']} | "
            f"{row['proposed_disposition']} | {row['grade_ceiling']} | {evidence} |"
        )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--passages", type=Path, default=DEFAULT_PASSAGES)
    parser.add_argument("--adjudications", type=Path, default=DEFAULT_ADJUDICATIONS)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    rows = build_rows(read_csv(args.queue), read_csv(args.passages), read_csv(args.adjudications))
    write_outputs(rows, args.csv, args.report)
    print(json.dumps({"unreviewed": len(rows), "top_score": rows[0]["review_score"] if rows else None}, sort_keys=True))
    print(f"CSV={args.csv}")
    print(f"REPORT={args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
