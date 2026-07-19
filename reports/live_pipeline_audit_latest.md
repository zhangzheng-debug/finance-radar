# Live Pipeline Audit

- Audited at: `2026-07-16T17:56:27.402158+00:00`
- Result: `PASS`
- Schema version: `12`
- Canonical events: `1160`
- Raw observations: `3556`

## Safety checks

- canonical_no_trading_violations: `0`
- impact_no_trading_violations: `0`
- non_abstain_asset_impacts: `0`
- candidate_market_observation_violations: `0`
- candidate_outbox_violations: `0`
- auto_verification_violations: `0`
- official_auto_promotion_violations: `0`
- official_multi_event_cluster_violations: `0`
- event_chain_primary_count_violations: `0`
- event_chain_primary_pointer_violations: `0`
- event_chain_no_trading_violations: `0`
- source_cursor_errors: `0`
- sec_enrichment_errors: `0`
- sec_enrichment_read_only_violations: `0`
- review_triage_no_trading_violations: `0`
- review_triage_auto_s_violations: `0`
- pending_review_without_triage: `0`
- runtime_leases: `0`
- alert_delivery_leases: `0`

## Official source cursors

- bls_key_indicators: `SUCCESS`; last success `2026-07-16T02:43:50.692545+00:00`
- cftc_enforcement: `NOT_MODIFIED`; last success `2026-07-16T03:37:45.466348+00:00`
- fda_medwatch: `NOT_MODIFIED`; last success `2026-07-16T03:37:46.218661+00:00`
- fdic_press_releases: `NOT_MODIFIED`; last success `2026-07-16T03:37:55.492232+00:00`
- federal_reserve_press: `NOT_MODIFIED`; last success `2026-07-16T03:37:28.557621+00:00`
- ftc_press: `SUCCESS`; last success `2026-07-16T03:37:51.619998+00:00`
- sec_current_filings: `SUCCESS`; last success `2026-07-16T03:37:29.368585+00:00`
- sec_litigation_releases: `NOT_MODIFIED`; last success `2026-07-16T03:37:52.360705+00:00`
- sec_trading_suspensions: `NOT_MODIFIED`; last success `2026-07-16T03:37:53.082258+00:00`

## Official candidate types

- annual_cra_eligible_geography_list: `1`
- annual_large_bank_stress_test_results: `1`
- annual_meeting_press_release_followup: `1`
- annual_meeting_voting_report: `1`
- auditor_change_without_disagreement: `1`
- bank_holding_company_written_agreement: `1`
- bank_president_individual_cease_and_desist_for_unsafe_lending: `1`
- bank_stress_test_result_schedule_announcement: `1`
- business_combination_shareholder_approval: `1`
- chief_financial_officer_appointment: `1`
- clo_refinancing: `1`
- consumer_price_index_release: `1`
- convertible_debt_financing: `1`
- credit_facility_amendment: `1`
- credit_facility_expansion_extension_and_margin_reduction: `1`
- discount_rate_meeting_minutes_release: `1`
- employee_warrant_grant: `1`
- employment_situation_release: `1`
- enforcement_action_termination: `1`
- equity_incentive_plan_share_reserve_reduction: `1`
- executive_chair_cfo_and_board_appointments: `1`
- extreme_convertible_preferred_financing: `1`
- financial_data_standards_final_rule: `1`
- fomc_rate_hold_and_policy_statement: `1`
- fomc_same_meeting_economic_projections_update: `1`
- fomc_same_meeting_minutes_followup: `1`
- former_bank_ceo_prohibition_and_fine_for_unsafe_lending_and_records_misconduct: `1`
- former_bank_employee_industry_prohibition_for_embezzlement: `1`
- fully_vested_executive_rsu_bonus: `1`
- going_concern_cash_burn_negative_working_capital_and_equity_financing: `1`
- going_concern_cash_exhaustion_and_realized_extreme_prefunded_warrant_dilution: `1`
- going_concern_cash_exhaustion_debt_and_convertible_financing_dependency: `1`
- going_concern_cash_shortfall_and_financing_dependency: `1`
- going_concern_near_zero_cash_negative_working_capital_and_convertible_financing_dependency: `1`
- job_openings_and_labor_turnover_release: `1`
- listing_compliance_extension: `1`
- minimum_bid_price_deficiency_notice: `1`
- monetary_policy_research_task_force_announcement: `1`
- nda_resubmission_regulatory_process_update: `1`
- nonrecourse_mortgage_maturity_default: `1`
- offering_or_dilution: `2`
- ordinary_annual_report_with_mixed_operating_results: `1`
- planned_ceo_retirement_with_internal_successor: `1`
- positive_earnings_and_acquisition_integration_update: `1`
- positive_exploration_drill_results: `1`
- positive_preliminary_earnings_update: `1`
- positive_preliminary_healthcare_operating_kpis: `1`
- positive_production_and_liquidity_update: `1`
- positive_quarterly_production_and_liquidity_update: `1`
- precautionary_wildfire_evacuation_and_exploration_suspension: `1`
- pro_forma_merger_financial_statement_amendment: `1`
- producer_price_index_release: `1`
- prompt_corrective_action_significantly_undercapitalized_bank: `1`
- related_party_funding_dependency_with_management_alleviated_going_concern_doubt: `1`
- reputation_risk_reference_removal: `1`
- reverse_merger_with_90_percent_as_converted_ownership_shift: `1`
- risk_based_aml_program_rule_proposal: `1`
- routine_annual_meeting_results: `1`
- routine_board_committee_appointment: `1`
- routine_board_vacancy_appointment: `1`
- routine_nav_and_leverage_update: `1`
- routine_nt_10q_extension_request: `1`
- senior_note_debt_refinancing_pricing: `1`
- senior_unsecured_debt_financing: `1`
- share_repurchase_authorization_expansion: `1`
- spac_ipo_closing: `1`
- spac_sponsor_working_capital_note: `1`
- stablecoin_customer_identification_rule_proposal: `1`
- warrant_inducement_financing: `1`

## Event chains

- `FR-CHAIN-FED-STRESS-TEST-2026` / `regulatory_result_release` / primary `FR-LIVE-a5e96d7bfae5211a7efb943195186bb9`
  - `FR-LIVE-a5e96d7bfae5211a7efb943195186bb9`: `primary_event`; primary_count `1`
  - `FR-LIVE-953209efd09334f37b61463df773fc5b`: `administrative_control`; primary_count `0`
- `FR-CHAIN-FOMC-2026-06-16-17` / `monetary_policy_meeting` / primary `FR-LIVE-a963817ac4043cab49f9cfc258c7c64d`
  - `FR-LIVE-a963817ac4043cab49f9cfc258c7c64d`: `primary_event`; primary_count `1`
  - `FR-LIVE-16cfb4b235818275461e5bc6df158b7a`: `followup_version`; primary_count `0`
  - `FR-LIVE-78b358190c1868babb4b2b42c431dd34`: `same_episode_support`; primary_count `0`
- `FR-CHAIN-SGML-AGM-2026` / `shareholder_meeting` / primary `FR-LIVE-ec122510ec5a5ac26c132d51115fad34`
  - `FR-LIVE-ec122510ec5a5ac26c132d51115fad34`: `primary_event`; primary_count `1`
  - `FR-LIVE-ed676036e32ffa74f70d9a0d647c2718`: `same_episode_support`; primary_count `0`
