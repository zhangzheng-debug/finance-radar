# Finance Radar V5.1 — AI Execution and Acceptance Specification

```yaml
spec_version: 5.1
snapshot_utc: 2026-07-19T05:03:20Z
positioning: full-polarity evidence ledger + adverse-risk review routing + deterministic replay
primary_surface: Evidence Terminal
deployment: Singapore VPS / systemd / Nginx / HTTPS
boundary: read-mostly intelligence; no trading
```

## 1. Verified current state

```yaml
public:
  web: https://radar.167-172-69-16.sslip.io:8443/radar/
  api: https://radar.167-172-69-16.sslip.io:8443/finance-radar-api/
  tls: valid
  api_rate_limit_per_client_per_minute: 180
release:
  accepted_runtime: /opt/finance-radar/releases/20260719T044852Z
  layout: immutable_release_plus_shared_data
ledger:
  schema: 12
  quick_check: ok
  sources: 22
  raw_observations: 3951
  canonical_events: 1194
  event_versions: 2117
  event_evidence: 2394
  market_metrics: 1898
operations:
  schema: 3
  quick_check: ok
  worker_cycles: 262
  latest_worker: SUCCESS
  worker_window_24h: {status: PARTIAL, source_of_truth: reports/runtime_evidence/runtime_gate_latest.json}
  latest_live_witness: {captured_at: 2026-07-19T05:17:26Z, observed_hours: 20.613, worker_cycles: 266, evidence_objects: 95, latest_worker: SUCCESS}
  verified_backup_runs: 35
  replay_runs: 7
  model_runs: 7
  agent_decisions: 3
  evidence_objects: 83
  adjudication_samples: 24
  adjudication_reviews: 0
evidence_archive:
  public_endpoint: /api/v1/evidence/archive
  source_snapshots: 81
  source_snapshot_bytes: 10936893
  exact_excerpts: 2
  mime: {html_objects: 80, pdf_objects: 1, json_objects: 0}
  sampled_sha256_integrity_failures: 0
  policy: {registered_official_sources_only: true, safe_registered_http_upgrade_to_https: true, redirect_host_revalidated: true, gradual_limit_per_worker_cycle: 4, paginates_past_archived_or_failed_head_rows: true, max_snapshot_bytes: 10485760, accepted_mime: [text/html, application/pdf, application/json], immutable_no_ttl: true, auto_verification_allowed: false, allowed_as_model_feature: false, no_trading: true}
retention:
  collection_interval_minutes: 5
  worker_restart: systemd_on_failure_after_20_seconds
  online_backup_daily: 30
  online_backup_weekly: 12
  event_and_evidence_ttl: none
market_capabilities:
  binance_public: {status: OBSERVED, role: PERSISTED_EVENT_OBSERVATION, deployment: SERVER_DIRECT, access: PUBLIC_NONE_AUTH}
  twelve_data: {status: OBSERVED, role: PERSISTED_EVENT_OBSERVATION, deployment: SERVER_DIRECT, access: API_KEY_MARKET_DATA_ONLY}
  ibkr_tws_readonly: {status: LOCAL_PROBE_ONLY, role: CAPABILITY_PROBE_ONLY, deployment: OPERATOR_DESKTOP, access: LOCAL_TWS_READ_ONLY}
  horizon_policy: {baseline: first_real_observer_snapshot, windows: [t_plus_5m, t_plus_30m, t_plus_1d], missed_window_behavior: record_MISSED_WINDOW_without_latest_quote_substitution, return_metric_scope: post_event_audit_only}
  boundary: {read_only: true, no_trading: true, account_data_used: false, order_endpoints_present: false, post_event_audit_only: true, allowed_as_model_feature: false}
  public_machine_audit: {status: PASS, checks: 17/17, report: reports/market_capabilities_live_latest.json}
quality:
  tests: 360_passed_plus_17_subtests
  test_seconds: 21.28
  streamlit_pages_without_runtime_exception: 5
  public_load:
    requests: 120
    concurrency: 15
    success_rate: 1.0
    p95_ms: 832
  product_acceptance_checks: 19/19
  public_browser_interactions: {status: BASELINE_PASS_REFRESH_REQUIRED_AFTER_UI_CHANGE, baseline_checks: 6/6, baseline_console_errors: 0, baseline_page_errors: 0, baseline_http_errors: 0}
  public_accessibility_machine_audit: {status: BASELINE_PASS_REFRESH_REQUIRED_AFTER_UI_CHANGE, baseline_pages: 5, baseline_blockers: 0, baseline_advisories: 0, baseline_browser_errors: 0, baseline_contrast_failures: 0, baseline_horizontal_overflow: 0, baseline_sampled_focus_visibility: 99/99}
  assistive_technology_user_test: external_pending
```

## 2. Objective and non-objectives

The system shall collect financial information from discovery and primary sources, preserve immutable observations and revisions, create canonical events, attach exact evidence passages, expose conflicts and insufficiency, route potentially material adverse events for human review, and persist all operational/model/agent traces.

It shall not expose or infer:

```yaml
forbidden:
  - orders
  - positions
  - balances
  - brokerage_accounts
  - trade_execution
  - LONG_or_SHORT
  - expected_return
  - target_price
  - automatic_fact_verification
  - automatic_final_grade_assignment
```

## 3. Runtime architecture

```text
official/discovery sources
  -> continuous systemd worker
  -> raw observations + cursor state
  -> candidate extraction + canonical ledger
  -> exact evidence links + immutable official HTML/PDF content-addressed objects
  -> evidence/finality gates
  -> shadow risk router and structured Evidence Agent
  -> FastAPI
  -> Streamlit Evidence Terminal
  -> Telegram dry-run outbox + Web deep links
```

```yaml
source_registry:
  P0_official_primary:
    - SEC_current_filings
    - SEC_litigation
    - SEC_trading_suspensions
    - CFTC_enforcement
    - FTC_press
    - FDIC_press
    - Federal_Reserve_press
    - BLS_key_indicators
    - FDA_MedWatch
    - ECB_press_and_speeches
    - ECB_statistical_press
    - EIA_press
  P1_issuer_official:
    - NVIDIA_official_newsroom
    - registered_issuer_or_project_official_channels
  P2_discovery:
    - OpenNews_free
    - historical_active_research
invariants:
  - authority_tier_is_not_sentiment
  - all_polarities_enter_the_ledger
  - P2_may_discover_but_cannot_auto_finalize
  - each_source_has_cursor_health_and_revision_semantics
```

```yaml
services:
  finance-radar-api: 127.0.0.1:18000
  finance-radar-web: 127.0.0.1:18501
  finance-radar-worker: continuous
  finance-radar-backup.timer: daily
runtime_evidence:
  script: scripts/capture_runtime_evidence.py
  windows_task: FinanceRadar-Runtime-Evidence
  interval_minutes: 15
  append_only_chain: SHA-256_previous_record
  history: reports/runtime_evidence/runtime_gate_history.jsonl
  latest_json: reports/runtime_evidence/runtime_gate_latest.json
  latest_human_report: reports/runtime_evidence/runtime_gate_latest.md
  scheduled_context_test_result: 0
  known_last_non_success_utc: 2026-07-18T12:11:35.589624Z
  earliest_known_possible_pass_utc: 2026-07-19T12:11:35.589624Z
  pass_rule: server_window_complete_AND_all_health_safety_checks
defense_evidence_pack:
  builder: scripts/build_defense_evidence_pack.py
  latest_report: artifacts/defense_pack/defense_pack_latest.json
  evidence_entries: 110
  defense_deck: artifacts/defense_deck/finance-radar-defense-deck-v1.pptx
  defense_deck_slides: 12
  defense_deck_QA: rendered_12_of_12_and_no_overflow
  verification: [secret_scan, zip_crc, manifest_inventory, per_file_SHA256]
  offline_purpose: reviewer_and_defense_evidence_when_VPS_or_network_is_unavailable
  excluded: [environment_files, recovery_key, encrypted_server_backup, telegram_send_capability, trading_project]
executable_offline_demo:
  builder: scripts/build_offline_demo.py
  verifier: scripts/verify_offline_demo.py
  archive: artifacts/offline_demo/finance-radar-offline-demo-latest.zip
  acceptance: reports/offline_demo_acceptance_latest.json
  selected_real_events: 22
  ledger: {schema: 12, sources: 18, observations: 78, evidence: 30, market_audit_metrics: 46}
  operations: {schema: 3, unlabeled_adjudication_samples: 10, human_reviews: 0}
  surfaces: [API, Situation_Room, Event_Intelligence, Replay_Lab, Operations_and_Model, Adjudication_Studio]
  acceptance_checks: 11_of_11_PASS
  external_network: process_guard_blocks_non_loopback
  model: SHADOW_with_failed_external_blind_gate_visible
  boundaries: [no_collectors, no_Telegram, no_broker_client, no_exchange_client, no_credentials, no_trading_routes]
course_readiness:
  script: scripts/audit_course_readiness.py
  manifest: config/course_evidence_manifest.json
  latest_json: reports/course_readiness_latest.json
  latest_human_report: reports/course_readiness_latest.md
  current_status: NOT_READY
  current_product_status: WAITING_runtime_24h_and_current_release_browser_QA
  current_course_process_status: WAITING_EXTERNAL
  current_repository_commits: 0
  false_course_checks: 11
  false_product_checks: [runtime_24h_hash_chain_pass, current_release_browser_QA_pass]
  invariants:
    - do_not_infer_teacher_approval
    - do_not_infer_student_authorship_from_AI_files
    - evidence_paths_must_be_inside_workspace_and_nonempty
    - optional_SHA256_must_match
    - commit_ids_must_resolve_to_real_git_commits
    - owner_and_reviewer_must_be_distinct_declared_members
    - three_timed_drills_and_one_improvised_change_per_member
    - browser_QA_report_release_must_equal_deploy_context_release
  hard_gates:
    product: python scripts/audit_course_readiness.py --require-product-ready
    full_course: python scripts/audit_course_readiness.py --require-ready
edge:
  nginx: public_8443_https
storage:
  mutable: /opt/finance-radar/shared
  releases: /opt/finance-radar/releases
isolation:
  quant_project: /root/ethusdc-pivot-bot
  rule: never enter, modify, restart, inspect credentials, or use for trading
adjudication:
  workflow_status: NOT_READY_FOR_FREEZE
  samples: 24
  reviews: 0
  reviewer_inputs: [materiality, polarity, evidence_state, rationale]
  target_label_submitted_by_reviewer: false
  peer_answers_hidden: true
  model_and_post_event_market_hidden: true
  conflict_resolution: distinct_third_arbiter
  public_write_controls_default_closed: true
  unauthenticated_queue_status: 403
  public_boundary_acceptance: 11/11
  production_changed: false
  blind_v2_frozen: false
```

## 4. Evidence Terminal contract

### Situation Room

```yaml
required:
  - compact_global_status_strip
  - recent_event_stream
  - review_queue
  - evidence_and_source_counts
  - worker_freshness
  - source_health
  - no_trading_boundary
```

### Event Workbench

```yaml
layout:
  left: saved_flow + selectable event stream
  center: event summary + exact evidence matrix + claims/edges + timeline
  right: separate decision context + visible read_only_market_context + agent/human review actions
market_context:
  crypto_provider: binance_public_market_data_only
  non_crypto_provider: twelve_data
  ibkr_role: operator_desktop_capability_probe_only
  required_fields: [provider, symbol, price, quote_currency, captured_age, freshness_or_unavailable]
  forbidden: [account_data, order_endpoint, position, balance, trade_trigger, model_training_feature]
navigation_continuity:
  situation_room_global_search: true
  recent_event_link_forces_all_events_flow: true
  url_filter_state_overrides_stale_widget_state: true
  preserved_fields: [flow, family, source, q, limit, event_id]
filter_facets:
  endpoint: /api/v1/events/facets
  fields: [event_family, discovery_source]
  aggregation_only: true
  read_only: true
  no_event_content: true
  no_trading: true
  event_family_widget: fuzzy_select_with_new_exact_value_allowed
  source_widget: fuzzy_select_with_exact_filter
  situation_room_commands: [Replay_Lab, Operations_and_Model, top_event_families, top_sources]
saved_flows:
  scope: device_local_browser_only
  maximum_named_flows: 8
  stored_fields: [flow, family, source, q, limit]
  restore_behavior: clear_stale_event_id_then_apply_saved_filters
  server_write: false
  network_call: false
  sensitive_or_content_data_stored: false
separate_dimensions:
  - adverse_risk_route
  - evidence_count
  - highest_authority
  - new_or_revision
  - conflict_state
  - shadow_model_confidence
  - workflow_state
invariant: never collapse dimensions into a buy/sell score
```

### Replay Lab

```yaml
cases:
  - sec_bankruptcy_verified
  - positive_earnings_non_target
  - rumor_correction_abstain
  - sec_filing_corrected_abstain
required:
  - simulated_clock
  - horizontal_step_timeline
  - observation_to_evidence_transition
  - shadow_router_plus_evidence_gate
  - alert_eligibility_visibility
  - official_revision_kind_and_supersedes_step_visibility
  - official_correction_withdraws_alert_eligibility
  - reset_and_rerun
  - external_network_off
```

### Operations and Model

```yaml
required:
  - API_ledger_worker_source_backup_model_SLOs
  - source_errors_sorted_first
  - source_freshness_age
  - backup_restore_evidence
  - model_card_and_hash
  - feature_ablation
  - drift_warn_and_promotion_block_thresholds
  - three_safety_counters
```

### Adjudication Studio

```yaml
required:
  - public_aggregate_progress_only
  - source_masked_unlabeled_samples
  - two_hidden_independent_axis_reviews
  - distinct_third_arbiter_for_conflicts
  - derived_target_label_after_review_only
  - no_model_output_or_post_event_market_outcome
  - UNASSIGNED_split_until_freeze
  - public_write_controls_closed_by_default
current: {samples: 24, reviews: 0, status: NOT_READY_FOR_FREEZE}
```

## 5. Model governance

```yaml
task: adverse_material_risk_review_routing
outputs: [RISK_REVIEW, NON_TARGET, ABSTAIN]
mode: shadow
training_rows: 897
split:
  type: deterministic recent connected issuer/event-chain group holdout
  issuer_overlap: 0
  event_chain_overlap: 0
  external_blind_set: true
combined_metrics:
  coverage: 0.826667
  covered_accuracy: 0.956989
  abstain_rate: 0.173333
external_blind_v1:
  freeze_id: external-blind-v1-2dd91c8b9acf
  collection_timing: collected_and_frozen_after_model_v1_training
  rows: 40
  balance: {RISK_REVIEW: 20, NON_TARGET: 20}
  sources: [SEC_litigation, CFTC_enforcement, Federal_Reserve_press, NVIDIA_official_news]
  title_or_id_training_overlap: 0
  max_training_shingle_jaccard: 0.072464
  label_first: true
  coverage: 0.975
  covered_accuracy: 0.512821
  risk_recall: 1.0
  non_target_false_risk_rate: 0.95
  preregistered_gate: FAIL
  diagnosis: internal_taxonomy_shortcuts_and_unrepresentative_NON_TARGET_controls
  promotion: REMAIN_SHADOW
  input_contract_audit:
    risk_rows_with_evidence_body: 0_of_20
    content_model_benchmark_contract: FAIL
v2_candidate_attempt:
  artifact: risk-router-v2-candidate-3629350054e0
  production_deployed: false
  learned_fields: [company_name, observed_title_summary, confirmed_facts, exact_evidence]
  prohibited_fields_removed: [event_family, event_type, discovery_source, status, manual_grade, post_event_market_data]
  legacy_control_shortcut_hits_in_top_coefficients: 0
  development: {coverage: 0.404348, covered_accuracy: 0.784946}
  source_holdout: {sources: [ECB_press, ECB_statistical_press, EIA_press], coverage: 0.041667, covered_accuracy: 0.0}
  candidate_gate: FAIL
  status: REJECTED_CANDIDATE_NOT_DEPLOYED
  next_valid_step: redesign_labels_and_source_routing_then_freeze_evidence_enriched_blind_v2
ablation:
  word_only: {coverage: 0.724444, covered_accuracy: 0.938650}
  char_only: {coverage: 0.844444, covered_accuracy: 0.952632}
  combined: {coverage: 0.826667, covered_accuracy: 0.956989}
drift_warn_if:
  abstain_rate_absolute_delta_gte: 0.15
  risk_review_rate_absolute_delta_gte: 0.20
  mean_confidence_absolute_delta_gte: 0.12
  p95_latency_ms_gte: 100
promotion: REMAIN_SHADOW
```

The 225-row in-domain holdout must be described as a frozen grouped temporal holdout. Separately, external-blind-v1 is a 40-row label-first set collected and frozen after model-v1 training with zero title/ID overlap. It failed the preregistered accuracy and false-risk gates. Do not tune model v1 on it or reuse it as a promotion test.

## 6. Backup and server replacement contract

```yaml
server_snapshot:
  script: deployment/systemd/create_migration_backup.sh
  includes: releases + shared data/reports + SQLite online snapshots + env + systemd/Nginx + manifests
  excludes: TLS_private_keys + quant_trading_project
offhost_pull:
  script: scripts/pull_server_migration_backup.ps1
  transport: SSH/SCP
  remote_and_local_hash_match: required
  archive_integrity: tar_list_required
  at_rest: AES-256-GCM + scrypt
  encrypted_round_trip_sha256_match: required_before_plaintext_removal
  full_restore_audit: scripts/audit_migration_restore.py
  full_restore_requirements:
    - authenticated_decryption
    - archive_path_and_link_safety
    - all_manifest_hashes_match
    - restore_only_expected_SQLite_files
    - SQLite_quick_check_and_integrity_check
    - schema_12_and_schema_3
    - current_release_inventory
    - external_blind_promotion_guard
    - no_trading_project_or_TLS_private_keys
    - temporary_plaintext_cleanup
  replacement_service_restore:
    orchestrator: scripts/restore_migration_to_vps.ps1
    preparer: scripts/prepare_migration_restore.py
    activator: deployment/systemd/activate_prepared_restore.sh
    audit_only_default: true
    explicit_activation_required: true
    current_VPS_blocked_by_default: true
    nonempty_target_refused: true
    nginx_and_TLS: pending_new_endpoint
  local_retention: 7
  remote_temporary_cleanup: after_verified_local_copy
  latest_encrypted_archive: server_migration_backup/20260719T045536Z/finance-radar-migration-20260719T045536Z.tgz.aesgcm
  latest_plaintext_archive_sha256: ac3ed8ba2a1ebd0f90eddfff921b39f683f17a397bfa9b2d6e074a467b24c1a5
automation:
  windows_task: FinanceRadar-Offhost-Backup
  schedule_local: daily_02:30
  start_when_available: true
restore_proof:
  encrypted_round_trip_sha256_match: true
  full_isolated_restore: true
  archive_members_scanned: 11091
  regular_files_scanned: 9861
  manifest_entries_all_match: 9860
  unpacked_bytes_scanned: 1559757804
  restored_ledger: {schema: 12, quick_check: ok, integrity_check: ok, sources: 22, raw_observations: 3951, events: 1194, event_versions: 2117, evidence: 2394, market_metrics: 1898}
  restored_operations: {schema: 3, quick_check: ok, integrity_check: ok, worker_cycles: 262, backup_runs: 35, replay_runs: 7, model_runs: 7, evidence_objects: 83, raw_source_snapshots: 81, raw_source_snapshot_bytes: 10936893, raw_source_snapshot_integrity_failures: 0, adjudication_samples: 24, adjudication_reviews: 0}
  latest_verified_snapshot: 20260719T045536Z
  latest_snapshot_required_release: 20260719T044852Z
  latest_report_sync: automatic_JSON_and_Markdown
  latest_snapshot_external_blind_report_included: true
  latest_snapshot_official_source_collector_included: true
  latest_snapshot_trading_project_included: false
  latest_snapshot_TLS_private_keys_included: false
  temporary_plaintext_cleaned: true
  full_service_preparation:
    status: PREPARED_NOT_ACTIVATED
    complete_regular_files_extracted: 9861
    complete_unpacked_bytes: 1559757804
    archive_symlinks_followed: false
    restricted_symlinks_planned: 78
    temporary_plaintext_and_stage_cleaned: true
  second_recovery_key_copy:
    location_outside_repository: C:/Users/MR/Documents/FinanceRadar-Recovery
    byte_hash_matches_primary: true
    ACL_inheritance_protected: true
    access_rules: current_user_only
    included_in_deployment_or_defense_pack: false
```

The primary key file is excluded from version control. A byte-identical second copy now exists outside the repository under `C:/Users/MR/Documents/FinanceRadar-Recovery`; inheritance is disabled and only the current Windows user has an access rule. Neither key copy is included in deployment or defense artifacts. Decrypt only into a temporary restore workspace, run both restore gates, and use explicit clean-host activation when the replacement VPS is ready.

## 7. Demo contract

```yaml
LIVE:
  proves: external connectivity, service health, source cursors
  success_requires_new_event: false
RECENT_CAPTURE:
  proves: persisted real history and timestamps
REPLAY:
  proves: deterministic evidence transition and safe routing
three_minute_path:
  - Situation Room status
  - recent real event
  - SEC P2_to_P0 replay
  - positive_and_conflict_controls
  - model_ablation_and_drift_policy
  - backup_restore_and_no_trading
```

## 8. Acceptance gates

```yaml
green:
  - public_https_web_and_api
  - evidence_terminal_five_pages
  - event_workbench_complete_saved_filter_state
  - event_workbench_bounded_button_and_keyboard_navigation
  - safe_empty_and_API_outage_states_without_internal_diagnostic_leakage
  - automated_event_workbench_page_level_regression
  - persistent_history
  - live_recent_replay_modes
  - structured_agent_fallback
  - content_addressed_evidence
  - shadow_model_zero_group_overlap
  - feature_ablation_and_drift_thresholds
  - API_rate_limit
  - public_120_request_15_concurrency_load
  - verified_daily_weekly_backup
  - automated_encrypted_offhost_backup
  - offhost_encryption_restore_hash_round_trip
  - full_encrypted_migration_archive_isolated_restore
  - full_service_restore_preparation_without_activation
  - explicit_clean_replacement_host_activation_gate
  - second_local_recovery_key_copy_hash_and_ACL
  - automated_hash_chained_runtime_evidence_capture_and_fail_closed_gate
  - machine_readable_no_fabrication_course_readiness_gate
  - all_tests_under_60_seconds
  - safety_and_fault_drills
  - live_acceptance_external_blind_evidence_and_promotion_guards
  - prior_release_narrow_screen_visual_browser_QA_baseline
  - prior_release_live_22_source_table_browser_QA_baseline
  - replacement_VPS_clean_host_resource_port_and_TLS_tool_preflight
  - v3_dual_review_workflow_operational_with_public_writes_closed
red_or_external:
  - full_24_hour_runtime_window
  - current_release_real_browser_visual_interaction_accessibility_matrix
  - authentic_dual_content_adjudication_under_v3_contract_and_fresh_evidence_enriched_external_blind_v2
  - authentic_student_forbidden_zones
  - valid_student_contribution_history
  - per_member_timed_exam_rehearsal
  - teacher_high_difficulty_and_FZ3_approval
```

## 9. AI stop rules

AI may continue work on adapters, UI, APIs, operations, deterministic replay, tests, backup tooling and documentation. AI must not:

1. Write the student-only forbidden-zone final implementations.
2. Fabricate student commits, contribution history, teacher approval or timed practice evidence.
3. Label the grouped holdout as an external blind test, hide the failed external-blind-v1 result, or reuse blind-v1 to claim V2 promotion.
4. Send Telegram messages without an explicit human-authorized send action.
5. Add trading capability or inspect/use the quant project.

## 10. Remaining priority order

```yaml
P0_external_course:
  - teacher approval
  - student handwritten forbidden zones
  - authentic contribution history
  - individual timed bug and improvised-change rehearsals
P1_runtime:
  - accumulate and report complete 24-hour worker window
P1_UI_QA:
  - refresh_current_release_real_browser_visual_interaction_accessibility_matrix
P1_AI_quality:
  - collect_authentic_dual_content_adjudications_under_config/risk_label_contract_v3.json
  - freeze_new_evidence_enriched_external_blind_v2_only_after_candidate_gate_pass
completed_QA:
  - human_proposal_DOCX_10_of_10_full_resolution_render_QA_numbering_fixed_and_a11y_zero_findings
  - executable_offline_demo_11_of_11_with_real_PowerShell_start_API_ok_Web_200
  - v3_dual_review_adjudication_infrastructure_with_24_unlabeled_real_ledger_tasks
  - public_review_UI_default_closed_and_unauthenticated_queue_HTTP_403
  - public_adjudication_acceptance_11_of_11
  - current_release_20260719T044852Z_and_operations_schema_3_encrypted_restore
  - Chinese_interaction_layer_for_controls_forms_steps_and_status_copy_with_machine_enums_preserved
  - immutable_official_source_archive_81_raw_HTML_PDF_objects_10936893_bytes_zero_sampled_integrity_failures
  - Situation_Room_global_search_and_URL_to_widget_filter_continuity
  - device_local_named_Flow_save_restore_delete_capped_at_8_filter_only_no_network
  - read_only_event_facets_source_filter_fuzzy_family_selector_and_data_driven_terminal_commands
  - public_market_capability_audit_17_of_17_with_persisted_Binance_and_Twelve_plus_local_IBKR_probe_and_honest_T_plus_windows
  - current_release_five_page_AppTest_and_public_product_acceptance_19_of_19
  - current_release_browser_QA_release_binding_gate_with_baseline_separated_from_current
  - replacement_VPS_preflight_before_plaintext_transfer_for_resources_ports_runtime_edge_tools_and_HTTPS_URL
  - fresh_secondary_page_deep_links_restore_query_and_event_id_without_browser_errors
  - all_nested_Streamlit_stcore_routes_canonicalized_at_Nginx
  - prior_release_five_page_public_accessibility_machine_audit_PASS_with_zero_findings_retained_as_baseline
  - deployment_venv_ownership_and_service_user_sklearn_import_gate
  - professional_12_slide_defense_deck_with_speaker_notes_and_full_render_QA
  - prior_release_Chrome_1920x1080_defense_matrix_for_all_five_product_surfaces_retained_as_baseline
  - prior_release_Chrome_1366x768_screenshots_for_all_five_pages_retained_as_baseline
  - prior_release_Chrome_390x844_critical_path_and_mobile_scroll_remediation_evidence_retained_as_baseline
  - reports/ui_qa/README.md
  - Streamlit_v2_keyboard_component_with_button_fallback
  - public_Chrome_J_K_selected_row_and_event_id_acceptance
  - public_Chrome_slash_DOM_focus_acceptance
  - public_Chrome_replay_1_of_2_PENDING_to_2_of_2_MET_RISK_REVIEW
  - saved_flow_family_query_limit_event_id_URL_state
  - page_level_navigation_empty_reset_and_outage_AppTests
  - official_ECB_EIA_NVIDIA_source_adapters_and_live_browser_evidence
  - external_blind_v1_label_first_zero_overlap_evaluation
  - external_blind_v1_failure_attribution_and_shadow_block
  - local_llama_cpp_evidence_summary_comparison_and_live_acceptance
  - risk_router_v2_candidate_real_training_and_machine_rejection
  - risk_label_v3_independent_axes_and_deterministic_non_label_source_lanes
  - existing_877_row_candidate_manifest_machine_blocked_from_blind_v2
  - teacher_approval_request_template
  - student_execution_and_timed_defense_evidence_template
  - student_course_handoff_with_exact_human_ownership_and_machine_gate_commands
```

Do not add PostgreSQL, React, Kubernetes, Redis, Celery or automated trading while any P0/P1 item is red.
