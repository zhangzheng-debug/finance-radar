#!/usr/bin/env python3
"""Rank pending live evidence reviews without assigning or verifying severity."""

from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from event_ledger import open_ledger, stable_json, utc_now


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "finance_radar.sqlite3"
DEFAULT_CSV = ROOT / "data" / "research" / "live_review_triage.csv"
DEFAULT_REPORT = ROOT / "reports" / "live_review_triage_latest.md"


@dataclass(frozen=True)
class Triage:
    score: int
    bucket: str
    direction: str
    ceiling: str
    reversible: str
    action: str
    reasons: tuple[str, ...]


POLICY = {
    "bankruptcy": (98, "hard_negative_first", "negative_likely_needs_equity_outcome", "S_deep_review_only", "external_rescue_possible", "confirm_plan_effective_and_common_equity_distribution"),
    "debt_default": (94, "hard_negative_first", "negative_likely_needs_scope", "A++_review_ceiling", "cure_or_waiver_possible", "confirm_default_amount_acceleration_and_cure_status"),
    "delisting": (90, "hard_negative_first", "negative_likely_cause_unresolved", "A++_review_ceiling", "merger_or_relisting_possible", "separate_merger_voluntary_noncompliance_and_forced_delisting"),
    "restructuring": (82, "material_negative", "negative_likely", "A_review_ceiling", "repairable", "confirm_cost_scope_cash_charge_and_operating_effect"),
    "enforcement_action": (78, "material_negative", "negative_likely", "A_review_ceiling", "settlement_or_remediation_possible", "confirm_penalty_restrictions_and_license_effect"),
    "offering_or_dilution": (74, "material_negative", "dilution_likely_terms_needed", "A_review_ceiling", "financing_may_extend_runway", "confirm_size_discount_warrants_and_post_money_dilution"),
    "going_concern_financing_dependency": (82, "material_negative", "liquidity_distress_likely", "A_review_ceiling", "new_capital_or_operating_recovery_possible", "confirm_cash_burn_working_capital_debt_maturities_funding_need_and_realized_dilution"),
    "convertible_debt_financing": (58, "direction_needed", "debt_and_dilution_terms_needed", "B_review_ceiling", "repayment_or_conversion_path_varies", "confirm_principal_coupon_conversion_premium_caps_and_use_of_proceeds"),
    "senior_unsecured_debt_financing": (50, "direction_needed", "leverage_terms_needed", "B_review_ceiling", "proceeds_may_refinance_nearer_debt", "confirm_principal_yield_maturity_ranking_and_use_of_proceeds"),
    "credit_facility_amendment": (48, "direction_needed", "liquidity_change_needs_terms", "B_review_ceiling", "amendment_may_improve_or_reduce_capacity", "confirm_commitment_change_maturity_pricing_covenants_and_borrowing_availability"),
    "debt_refinancing": (42, "direction_needed", "refinancing_terms_needed", "B_review_ceiling", "maturity_extension_may_offset_cost", "compare_old_and_new_cost_maturity_covenants_and_capacity"),
    "spac_sponsor_working_capital_note": (20, "low_value_official_noise", "ordinary_sponsor_working_capital", "C_review_ceiling", "conversion_only_if_business_combination", "confirm_principal_interest_conversion_trigger_and_no_current_merger"),
    "spac_ipo_closing": (15, "low_value_official_noise", "capital_formation_not_merger", "C_review_ceiling", "trust_redemption_structure", "confirm_units_warrants_trust_amount_and_no_completed_target_acquisition"),
    "product_recall": (76, "material_negative", "negative_likely", "A++_review_ceiling", "remediation_possible", "confirm_recall_scope_injury_and_core_product_dependence"),
    "management_change": (58, "direction_needed", "direction_unresolved", "A_review_ceiling", "replacement_may_mitigate", "determine_forced_departure_cause_and_transition"),
    "earnings_or_guidance": (45, "direction_needed", "direction_unresolved", "A_review_ceiling", "ordinary_operating_recovery_possible", "establish_miss_cut_or_deterioration_before_negative_label"),
    "material_corporate_transaction": (52, "direction_needed", "direction_unresolved", "A_review_ceiling", "transaction_may_be_positive", "determine_consideration_financing_and_control_change"),
    "merger_or_acquisition": (48, "direction_needed", "direction_unresolved", "B_review_ceiling", "transaction_may_be_positive", "determine_target_buyer_and_equity_consideration"),
    "conflict_or_blockade": (68, "macro_materiality", "asset_direction_dependent", "A_review_ceiling", "ceasefire_or_rerouting_possible", "confirm_current_operational_scope_and_effective_time"),
    "sanctions_or_tariffs": (66, "macro_materiality", "asset_direction_dependent", "A_review_ceiling", "policy_reversal_possible", "confirm_named_targets_scope_and_effective_date"),
    "monetary_policy": (42, "macro_digest", "asset_direction_dependent", "B_review_ceiling", "policy_path_changes", "separate_decision_minutes_and_commentary"),
    "inflation_release": (44, "macro_digest", "asset_direction_dependent", "B_review_ceiling", "data_revisions_expected", "confirm_value_previous_revision_and_no_consensus_if_missing"),
    "employment_release": (42, "macro_digest", "asset_direction_dependent", "B_review_ceiling", "data_revisions_expected", "confirm_value_previous_revision_and_release_period"),
    "bank_regulatory_update": (38, "regulatory_digest", "direction_unresolved", "B_review_ceiling", "proposal_or_comment_stage", "separate_proposal_final_rule_result_and_schedule"),
    "sec_material_filing": (24, "low_value_official_noise", "direction_unresolved", "B_review_ceiling", "meaning_not_yet_classified", "read_primary_excerpt_and_assign_event_type_or_reject"),
    "auditor_change_without_disagreement": (25, "low_value_official_noise", "routine_auditor_rotation", "C_review_ceiling", "no_disagreement_found", "confirm_unmodified_opinion_and_no_reportable_event"),
    "share_repurchase_authorization_expansion": (28, "low_value_official_noise", "positive_authorization_not_execution", "C_review_ceiling", "authorization_may_never_be_used", "separate_authorization_from_realized_repurchases"),
    "employee_warrant_grant": (20, "low_value_official_noise", "compensation_grant", "C_review_ceiling", "vesting_and_exercise_uncertain", "compare_grant_size_with_shares_outstanding"),
    "business_combination_shareholder_approval": (45, "direction_needed", "transaction_approved_not_closed", "B_review_ceiling", "closing_conditions_remain", "confirm_redemptions_final_ownership_and_closing"),
    "routine_nt_10q_extension_request": (25, "low_value_official_noise", "late_filing_reason_needed", "C_review_ceiling", "standard_extension_available", "confirm_no_restatement_auditor_dispute_or_control_failure"),
    "precautionary_wildfire_evacuation_and_exploration_suspension": (52, "direction_needed", "temporary_operational_disruption", "B_review_ceiling", "operations_may_resume", "confirm_damage_duration_and_production_effect"),
    "routine_nav_and_leverage_update": (20, "low_value_official_noise", "routine_fund_disclosure", "C_review_ceiling", "monthly_values_change", "confirm_no_default_write_down_or_covenant_breach"),
    "chief_financial_officer_appointment": (45, "direction_needed", "named_c_suite_appointment", "B_review_ceiling", "transition_may_be_orderly", "confirm_predecessor_status_and_effective_date"),
    "minimum_bid_price_deficiency_notice": (58, "direction_needed", "listing_risk_with_cure_period", "B_review_ceiling", "compliance_can_be_regained", "confirm_cure_deadline_no_immediate_delisting_and_actual_compliance"),
    "positive_preliminary_healthcare_operating_kpis": (38, "direction_needed", "positive_preliminary_operating_context", "B_review_ceiling", "figures_subject_to_revision", "compare_prior_period_and await_final_financials"),
    "credit_facility_expansion_extension_and_margin_reduction": (46, "direction_needed", "liquidity_terms_improved", "B_review_ceiling", "future_draws_uncertain", "confirm_capacity_maturity_margin_and_current_draw"),
    "nda_resubmission_regulatory_process_update": (52, "direction_needed", "regulatory_path_unresolved", "B_review_ceiling", "future_meeting_or_resubmission", "separate_meeting_minutes_from_resubmission_acceptance_or_approval"),
    "annual_meeting_voting_report": (15, "low_value_official_noise", "routine_shareholder_vote", "C_review_ceiling", "annual_cycle", "confirm_no_contested_control_or_special_transaction"),
}
DEFAULT_POLICY = (35, "direction_needed", "direction_unresolved", "B_review_ceiling", "unknown", "manual_event_meaning_review")


def triage_row(row: Any) -> Triage:
    base, bucket, direction, ceiling, reversible, action = POLICY.get(row["event_type"], DEFAULT_POLICY)
    reasons = [f"event_type:{row['event_type']}"]
    text = " ".join(
        str(value or "").casefold()
        for value in (row["title"], row["observation_summary"], row["evidence_excerpt"])
    )

    # Source semantics can reverse or neutralize a broad event-type keyword.
    # These are review-routing overrides only; they never adjudicate the event.
    if row["event_type"] == "delisting" and (
        "granted" in text and "exception" in text or "trading resumed" in text
    ):
        base, bucket, direction, ceiling, reversible, action = (
            65,
            "direction_needed",
            "compliance_relief_with_remaining_risk",
            "A_review_ceiling",
            "deadline_and_recurrence_risk",
            "confirm_exception_deadline_current_bid_price_and_actual_compliance",
        )
        reasons.append("listing_exception_or_resumed_trading")
    elif row["event_type"] == "enforcement_action":
        termination = (
            "termination of enforcement action" in text
            or "termination enforcement action" in text
            or "terminates enforcement action" in text
        )
        issuance = "issues enforcement action" in text
        individual = "employee of" in text or "former employee" in text
        if termination and issuance:
            base, bucket, direction, ceiling, reversible, action = (
                48,
                "direction_needed",
                "mixed_new_and_terminated_actions",
                "B_review_ceiling",
                "named_actions_have_different_effects",
                "separate_each_new_action_from_each_termination",
            )
            reasons.append("mixed_enforcement_release")
        elif termination:
            base, bucket, direction, ceiling, reversible, action = (
                35,
                "regulatory_digest",
                "resolution_likely_not_new_negative",
                "B_review_ceiling",
                "action_already_terminated",
                "confirm_termination_scope_and_original_subject",
            )
            reasons.append("enforcement_termination")
        elif individual:
            base, bucket, direction, ceiling, reversible, action = (
                45,
                "regulatory_digest",
                "individual_action_not_issuer_loss",
                "B_review_ceiling",
                "issuer_effect_not_established",
                "confirm_subject_is_individual_and_check_separate_bank_restrictions",
            )
            reasons.append("individual_enforcement_subject")
    elif row["event_type"] == "management_change":
        forced = any(value in text for value in ("resigned effective immediately", "terminated", "removed for cause"))
        routine = (
            any(value in text for value in ("will retire", "elected by the board", "appointed to the board"))
            or "appointed" in text and "board" in text
        )
        compensation_only = any(
            value in text for value in ("inducement stock plan", "compensation committee", "compensatory arrangement")
        ) and not any(
            value in text for value in ("chief executive", "chief financial", "resigned", "will retire")
        )
        if compensation_only:
            base, bucket, direction, ceiling, reversible, action = (
                25,
                "low_value_official_noise",
                "compensation_plan_not_management_departure",
                "B_review_ceiling",
                "not_a_management_transition",
                "reject_management_change_unless_a_named_officer_transition_is_present",
            )
            reasons.append("item_5_02_compensation_only")
        elif routine and not forced:
            base, bucket, direction, ceiling, reversible, action = (
                35,
                "direction_needed",
                "routine_succession_or_board_change",
                "B_review_ceiling",
                "planned_transition",
                "confirm_no_forced_departure_or_control_dispute",
            )
            reasons.append("routine_governance_language")
    elif row["event_type"] == "offering_or_dilution":
        debt_terms = "senior unsecured notes" in text or "aggregate principal amount" in text
        equity_terms = any(value in text for value in ("ordinary shares", "common stock", "warrant", "convertible"))
        if debt_terms and not equity_terms:
            base, bucket, direction, ceiling, reversible, action = (
                40,
                "direction_needed",
                "debt_financing_not_equity_dilution",
                "B_review_ceiling",
                "refinancing_use_may_be_neutral",
                "confirm_leverage_yield_maturity_and_use_of_proceeds",
            )
            reasons.append("debt_offering_not_equity")
    elif row["event_type"] == "earnings_or_guidance":
        periodic_form = str(row["title"] or "").upper().startswith(("10-K", "10-Q", "20-F"))
        negative_terms = any(
            value in text
            for value in (
                "profit warning",
                "guidance cut",
                "lowered guidance",
                "substantial doubt about our ability to continue as a going concern",
                "substantial doubt regarding our ability to continue as a going concern",
            )
        )
        resolved_doubt = "substantial doubt" in text and "alleviated" in text
        amendment_only = (
            "explanatory note" in text and "pro forma" in text
            or str(row["title"] or "").upper().startswith(("8-K/A", "6-K/A")) and "pro forma" in text
        )
        positive_terms = any(value in text for value in ("record throughput", "strong performance", "raises guidance"))
        if resolved_doubt or amendment_only:
            base, bucket, direction, ceiling, reversible, action = (
                30,
                "low_value_official_noise",
                "resolution_or_amendment_without_new_negative_claim",
                "B_review_ceiling",
                "negative_condition_not_currently_established",
                "confirm_no_new_adverse_fact_or_reject",
            )
            reasons.append("resolved_or_amendment_context")
        elif positive_terms and not negative_terms:
            base, bucket, direction, ceiling, reversible, action = (
                35,
                "direction_needed",
                "positive_likely_numbers_unbenchmarked",
                "B_review_ceiling",
                "performance_can_reverse",
                "compare_reported_metrics_with_prior_period_without_inventing_consensus",
            )
            reasons.append("positive_release_language")
        elif periodic_form and not negative_terms:
            base, bucket, direction, ceiling, reversible, action = (
                35,
                "low_value_official_noise",
                "periodic_filing_without_event_claim",
                "B_review_ceiling",
                "ordinary_periodic_reporting",
                "find_specific_miss_guidance_change_or_distress_claim_or_reject",
            )
            reasons.append("periodic_form_without_specific_event")

    score = base
    authority = str(row["authority_tier"] or "")
    if authority.startswith("P0"):
        score += 5
        reasons.append("P0_official_source")
    if row["enrichment_status"] == "PARSED":
        score += 5
        reasons.append("SEC_primary_text_parsed")
    elif row["page_evidence_status"] == "machine_extracted_unreviewed":
        reasons.append("official_primary_page_text_machine_extracted_unreviewed")
    elif row["page_evidence_status"] == "confirmed_primary":
        reasons.append("official_primary_page_text_confirmed")
    if row["matched_event_type"]:
        score += 4
        reasons.append("document_type_match")
    if row["company_name"]:
        score += 2
        reasons.append("named_company")
    if bucket in {"macro_digest", "regulatory_digest", "low_value_official_noise"}:
        score -= 5
        reasons.append("digest_or_noise_penalty")
    return Triage(min(100, max(0, score)), bucket, direction, ceiling, reversible, action, tuple(reasons))


def evidence_readiness(row: Any) -> str:
    """Describe review material availability without implying adjudication."""
    has_sec_text = row["enrichment_status"] == "PARSED" and bool(
        str(row["evidence_excerpt"] or "").strip()
    )
    has_official_page_text = bool(str(row["page_evidence_passage"] or "").strip()) and row[
        "page_evidence_status"
    ] in {"machine_extracted_unreviewed", "confirmed_primary"}
    if has_sec_text or has_official_page_text:
        return "primary_text_ready"
    if str(row["authority_tier"]).startswith("P0"):
        return "primary_link_ready"
    return "discovery_only"


def build(connection: Any) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT e.*,j.priority AS old_priority,r.title,r.summary AS observation_summary,
                  r.canonical_url,s.authority_tier,
                  x.status AS enrichment_status,x.matched_event_type,x.confidence,
                  x.primary_document_url,x.evidence_excerpt,
                  ev.evidence_url AS page_evidence_url,
                  ev.evidence_passage AS page_evidence_passage,
                  ev.evidence_status AS page_evidence_status
           FROM canonical_events e
           JOIN pipeline_jobs j ON j.event_id=e.event_id
           JOIN event_observations eo ON eo.event_id=e.event_id
           JOIN latest_source_content r ON r.observation_id=eo.observation_id
           JOIN sources s ON s.source_id=r.source_id
           LEFT JOIN sec_filing_enrichments x ON x.observation_id=r.observation_id
           LEFT JOIN event_evidence ev
             ON ev.event_id=e.event_id AND ev.observation_id=r.observation_id
           WHERE j.job_type='live_primary_evidence_review'
             AND j.status='PENDING_PRIMARY_EVIDENCE'
           ORDER BY e.event_id,r.local_received_at LIMIT 1000"""
    ).fetchall()
    by_event: dict[str, Any] = {}
    for row in rows:
        current = by_event.get(row["event_id"])
        if current is None or (str(row["authority_tier"]).startswith("P0") and not str(current["authority_tier"]).startswith("P0")):
            by_event[row["event_id"]] = row
    now = utc_now()
    output = []
    for row in by_event.values():
        item = triage_row(row)
        readiness = evidence_readiness(row)
        connection.execute(
            """INSERT INTO event_review_triage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)
               ON CONFLICT(event_id) DO UPDATE SET event_version=excluded.event_version,
               review_score=excluded.review_score,review_bucket=excluded.review_bucket,
               direction_status=excluded.direction_status,evidence_readiness=excluded.evidence_readiness,
               severity_ceiling=excluded.severity_ceiling,reversibility_flag=excluded.reversibility_flag,
               next_action=excluded.next_action,reason_codes_json=excluded.reason_codes_json,
               updated_at=excluded.updated_at,no_trading=1""",
            (row["event_id"],row["current_version"],item.score,item.bucket,item.direction,
             readiness,
             item.ceiling,item.reversible,item.action,stable_json(item.reasons),now,now),
        )
        connection.execute("UPDATE pipeline_jobs SET priority=?,updated_at=? WHERE event_id=? AND job_type='live_primary_evidence_review'",(item.score,now,row["event_id"]))
        output.append({"event_id":row["event_id"],"event_date":row["event_date"],"event_type":row["event_type"],"company_name":row["company_name"] or "","title":row["title"],"review_score":item.score,"review_bucket":item.bucket,"direction_status":item.direction,"severity_ceiling":item.ceiling,"evidence_readiness":readiness,"evidence_status":row["page_evidence_status"] or row["enrichment_status"] or "link_only","next_action":item.action,"evidence_url":row["page_evidence_url"] or row["primary_document_url"] or row["canonical_url"] or "","evidence_excerpt":row["page_evidence_passage"] or row["evidence_excerpt"] or ""})
    connection.commit()
    return sorted(output,key=lambda value:(-value["review_score"],value["event_date"],value["event_id"]))


def write_outputs(rows: list[dict[str, Any]], csv_path: Path, report_path: Path) -> None:
    csv_path.parent.mkdir(parents=True,exist_ok=True)
    fields=list(rows[0]) if rows else ["event_id"]
    with csv_path.open("w",encoding="utf-8-sig",newline="") as handle:
        writer=csv.DictWriter(handle,fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    counts: dict[str,int]={}
    for row in rows: counts[row["review_bucket"]]=counts.get(row["review_bucket"],0)+1
    lines=["# Live Review Triage","",f"- Pending events ranked: `{len(rows)}`","- Queue score is review priority, not severity and not a trading signal.","- `S_deep_review_only` never means an automatic S label.","", "## Buckets",""]
    lines += [f"- {key}: `{value}`" for key,value in sorted(counts.items())]
    lines += ["","## Top 15","","| rank | score | bucket | type | company/title | direction | ceiling | next action |","|---:|---:|---|---|---|---|---|---|"]
    for index,row in enumerate(rows[:15],1):
        label=(row["company_name"] or row["title"]).replace("|","/")[:70]
        lines.append(f"| {index} | {row['review_score']} | {row['review_bucket']} | {row['event_type']} | {label} | {row['direction_status']} | {row['severity_ceiling']} | {row['next_action']} |")
    report_path.parent.mkdir(parents=True,exist_ok=True); report_path.write_text("\n".join(lines)+"\n",encoding="utf-8")


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--db",type=Path,default=DEFAULT_DB); parser.add_argument("--csv",type=Path,default=DEFAULT_CSV); parser.add_argument("--report",type=Path,default=DEFAULT_REPORT); args=parser.parse_args()
    connection=open_ledger(args.db)
    try: rows=build(connection)
    finally: connection.close()
    write_outputs(rows,args.csv,args.report); print(json.dumps({"events":len(rows),"top_score":rows[0]["review_score"] if rows else None},sort_keys=True)); print(f"REPORT={args.report}"); return 0


if __name__=="__main__": raise SystemExit(main())
