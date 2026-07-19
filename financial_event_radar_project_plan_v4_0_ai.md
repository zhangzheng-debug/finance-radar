# Finance Radar V4.0 — AI Execution Specification

```yaml
document:
  id: finance-radar-v4-ai-spec
  version: "4.0"
  date: "2026-07-17"
  status: proposed_for_teacher_review
  language: zh-CN
  human_companion: financial_event_radar_project_proposal_v4_0_human.docx

project:
  title_zh: 基于多源证据链与实时Web控制台的金融事件情报Agent
  short_name: Finance Radar
  positioning: 全极性事实核验 + 重大下行风险优先级 + 可审计回放
  project_class: 自主高难度创新选题
  target_score: 95_plus
  primary_surface: web_situation_room
  secondary_surface: telegram_notification
  deployment_target: singapore_vps
  prohibited_capability: trading
```

## 1. Objective

Build a server-deployed financial-event intelligence workbench that:

1. continuously collects official and discovery sources;
2. persists immutable source observations, revisions, event versions and evidence history;
3. uses an LLM-based Evidence Agent for claim extraction, evidence planning and cited summaries;
4. uses a small CPU model only for adverse-risk review routing;
5. applies deterministic evidence, finality and no-trading gates;
6. presents the system through a professional Web Situation Room;
7. keeps Telegram as a notification/deep-link channel, not the primary terminal;
8. supports deterministic replay when live external events are unavailable.

## 2. Truthful Current State

```yaml
current_state:
  verified_present:
    sqlite_schema_version: 12
    registered_sources: 18
    canonical_events: 1160
    raw_observations: 3556
    source_revisions: 710
    event_versions: 2081
    evidence_rows: 2386
    market_metrics_audit_only: 1898
    tests_last_verified: 232
    test_result: PASS
    safety_audit: PASS
    no_trading_violations: 0
  not_yet_implemented:
    - persistent_background_scheduler
    - web_terminal
    - fastapi_read_api
    - executable_replay_runner
    - runtime_llm_agent
    - trained_small_model
    - docker_compose_deployment
    - valid_git_repository_history
    - agent_process_documents
    - measured_test_coverage
  important_interpretation:
    - Existing SEC/OpenNews/official adapters prove source feasibility, not continuous production operation.
    - Existing 792 historical adjudications are a downside-risk-focused corpus, not a LONG/SHORT dataset.
    - Existing severity grades express event importance/finality, not price direction.
```

## 3. Scope Decision

### 3.1 Included in V4 core

- Web Situation Room as the main user interface.
- Singapore VPS deployment with HTTPS and automatic restart.
- Persistent event history, source revisions, evidence snapshots and model/agent traces.
- LIVE, RECENT_CAPTURE and REPLAY demonstration modes.
- All-polarity event discovery and fact verification.
- Adverse-risk-focused review routing model in shadow mode.
- Telegram alerts linking back to the Web event detail page.
- Three student-handwritten forbidden-zone kernels.
- Blind bug-injection and improvised-change rehearsal.

### 3.2 Explicit non-goals

- No order placement, positions, balances or trading-account access.
- No universal price-direction or return prediction.
- No claim that adverse news always has a larger market move than favorable news.
- No Kubernetes, Redis, Celery or microservice sprawl in the 12-day core.
- No automatic S grade, automatic publication of unresolved claims, or model-controlled alert eligibility.
- No dependency on a new SEC material event occurring during the live defense.

## 4. Architecture Decision

Use a modular monolith and one repository. Deploy the same application image with different commands rather than building many independent services.

```yaml
deployment:
  edge:
    component: Caddy
    responsibilities: [HTTPS, certificate_renewal, reverse_proxy, security_headers]
  web:
    component: Streamlit_multipage
    port: 8501
    responsibilities: [situation_room, event_detail, replay_lab, operations_model_monitor]
  api:
    component: FastAPI
    port: 8000
    mode: read_mostly
    responsibilities: [query_contracts, health, source_status, event_timeline, trace, replay_control]
  worker:
    component: Python_worker
    responsibilities: [scheduled_collection, candidate_extraction, evidence_enrichment, model_shadow_inference]
  notifier:
    component: Telegram_outbox_consumer
    responsibilities: [idempotent_delivery, edit_or_correct, web_deep_links]
  data:
    primary_core: SQLite_WAL_on_persistent_VPS_volume
    object_store_core: content_addressed_filesystem_volume
    backup_core: online_sqlite_backup_plus_snapshot_retention
    upgrade_adapter: PostgreSQL
  model_runtime:
    component: trusted_local_artifact_loaded_by_worker
    compute: CPU
    initial_model: TFIDF_plus_calibrated_logistic_regression
    mode: shadow
  process_supervision:
    component: Docker_Compose_restart_policy
```

### 4.1 Why SQLite remains the 12-day core

- Current ledger is already implemented and validated on SQLite Schema 12.
- The current database is small and the workflow has a controlled single writer.
- WAL, runtime leases, idempotent jobs and durable outbox already exist.
- Rewriting all raw SQL for PostgreSQL would consume the period without improving the core user proof.
- Professional operation is demonstrated through persistence, backups, restore testing, health checks and auditability.

PostgreSQL is an upgrade gate, not a score-critical dependency. Activate it only after the repository interface and migration tests pass.

## 5. Primary Web Terminal

```yaml
web_pages:
  situation_room:
    must_show:
      - live_event_stream
      - candidate_verified_rejected_counts
      - source_health_and_last_success
      - collection_and_processing_latency
      - review_queue
      - current_demo_mode_badge
  event_detail:
    must_show:
      - event_identity_and_version
      - atomic_claims
      - claim_evidence_matrix
      - supporting_and_contradicting_passages
      - source_authority_and_independence
      - event_timeline
      - model_shadow_output
      - agent_trace_and_human_override
      - explicit_no_trading_banner
  replay_lab:
    must_show:
      - case_selector
      - simulated_clock
      - step_by_step_state_changes
      - same_downstream_code_assertion
      - reset_and_rerun
  operations_model:
    must_show:
      - API_and_worker_health
      - source_cursors
      - last_backup_and_restore_drill
      - model_version_hash_metrics_and_shadow_status
      - audit_violation_counters
```

UI rule: Telegram is never the only way to inspect evidence. Every alert contains a stable Web URL such as `/events/{event_id}`.

## 6. API Contract

```yaml
api_v1:
  - GET /api/v1/health
  - GET /api/v1/sources/health
  - GET /api/v1/events
  - GET /api/v1/events/{event_id}
  - GET /api/v1/events/{event_id}/timeline
  - GET /api/v1/events/{event_id}/evidence
  - GET /api/v1/events/{event_id}/trace
  - GET /api/v1/model/status
  - POST /api/v1/replays/{case_id}/run
  - POST /api/v1/replays/{case_id}/reset
forbidden_endpoints:
  - orders
  - positions
  - balances
  - brokerage_accounts
  - trade_execution
```

All response payloads require `schema_version`, `trace_id`, `generated_at` and explicit error codes.

## 7. Storage and History Contract

```yaml
history_layers:
  raw_observation: immutable_first_capture
  source_revision: append_only_content_revision
  canonical_event: current_event_pointer
  event_version: append_only_state_history
  evidence_edge: claim_to_exact_passage_relation
  agent_decision: model_prompt_tool_guardrail_trace
  model_run: input_hash_model_hash_output_latency_shadow_decision
  human_override: actor_time_reason_before_after
  alert_outbox: idempotent_notification_state
evidence_objects:
  path_rule: sha256_prefix/sha256
  allowed_types: [html, txt, pdf, json]
  database_stores: [hash, mime_type, byte_length, source_url, fetched_at]
retention:
  database_backups_days: 14
  weekly_backups_count: 8
  pre_migration_backup: required
  restore_drill_before_defense: required
```

## 8. AI and Model Responsibilities

### 8.1 Evidence Agent

```yaml
evidence_agent:
  nodes:
    - claim_extractor
    - evidence_plan_builder
    - evidence_relation_proposer
    - cited_summary_renderer
  output_contracts:
    - EventClaim
    - EvidenceEdgeProposal
    - AgentDecision
  constraints:
    max_research_rounds: 3
    structured_output_required: true
    source_allowlist_required: true
    evidence_excerpt_required: true
    unresolved_conflict_result: HUMAN_REVIEW
    missing_evidence_result: INSUFFICIENT
    model_can_assign_final_S: false
```

### 8.2 Small risk-routing model

The model target is adverse material-risk routing, not sentiment trading.

```yaml
risk_router_v1:
  target_task: adverse_material_risk_routing
  target_positive_class:
    - bankruptcy_or_equity_death
    - forced_delisting_or_suspension
    - extreme_dilution_or_recapitalization
    - cash_exhaustion_or_financing_dependency
    - major_regulatory_or_operational_risk
  non_target_controls:
    - favorable_disclosures
    - neutral_administration
    - ordinary_financing
    - paid_merger_delisting
    - mechanical_reverse_split
    - stale_or_duplicate_events
    - rejected_false_positives
  outputs:
    decision: [RISK_REVIEW, NON_TARGET, ABSTAIN]
    fields: [risk_probability, event_family_probabilities, model_version, feature_version, reason_features]
  forbidden_outputs: [LONG, SHORT, expected_return, severity_grade, alert_permission]
  split_strategy: time_plus_issuer_plus_event_chain
  shadow_mode: true
  promotion_gate:
    - beats_rules_PR_AUC_by_at_least_0_05_or_remains_shadow
    - risk_Recall_at_review_budget_at_least_0_90
    - favorable_neutral_false_escalation_at_most_0_10
    - zero_training_leakage_audit_findings
  artifact_bundle:
    - model.joblib
    - model.sha256
    - feature_schema.json
    - metrics.json
    - model_card.md
    - data_card.md
```

Important: model task focus on adverse events does not mean training on adverse examples only. Favorable, neutral and rejected cases are required as non-target controls.

### 8.3 Required training-data repair

Replace free-text inference from `training_role` with structured fields:

```yaml
training_contract:
  training_eligible: boolean
  exclusion_reason: nullable_enum
  label_task: enum[risk_routing, event_family, direction, severity]
  label_source: enum[human, rule, imported]
  label_version: string
```

## 9. Demo Modes

```yaml
demo_modes:
  LIVE:
    proves: [external_connectivity, cursor_health, newest_capture, candidate_formation]
    success_without_new_event: true
  RECENT_CAPTURE:
    proves: [real_persisted_history, real_timestamps, evidence_available]
    labeling_requirement: show_published_received_processed_times
  REPLAY:
    proves: [agent_reasoning, evidence_gate, finality, conflict_handling, repeatability]
    cases: [NINEQ_true_terminal_event, WOLF2_difficult_non_S_control]
    implementation_rule: only_input_adapter_and_clock_may_change
```

Three-minute defense path:

1. 0:00-0:25 — Web Situation Room, source health, current mode.
2. 0:25-0:50 — latest real capture and persistent history.
3. 0:50-1:35 — NINEQ replay: claims, evidence and finality transition.
4. 1:35-2:10 — WOLF2 replay: conflict and refusal to promote S.
5. 2:10-2:35 — risk-router shadow output and model card.
6. 2:35-3:00 — trace, audit, Telegram deep link and no-trading proof.

## 10. Student-Handwritten Forbidden Zones

```yaml
forbidden_zones:
  FZ1:
    file: app/core/event_fingerprint.py
    function: deterministic_revision_and_event_identity
    effective_LOC_target: 80_to_150
    minimum_student_designed_tests: 18
  FZ2:
    file: app/core/evidence_gate.py
    function: claim_evidence_coverage_conflict_and_independence
    effective_LOC_target: 80_to_150
    minimum_student_designed_tests: 18
  FZ3_preferred:
    file: app/core/finality_gate.py
    function: legal_finality_identity_conflict_grade_cap_and_no_trading
    effective_LOC_target: 80_to_150
    minimum_student_designed_tests: 20
    approval_required: teacher_day_1
  FZ3_A10_fallback:
    file: app/core/diversity_reranker.py
    function: issuer_family_source_diversity_for_review_queue
```

Existing AI-assisted code cannot be retroactively declared handwritten. Students must implement the final forbidden-zone code themselves and preserve design notes, commits, tests and walkthrough scripts.

## 11. Repository and Deployment Layout

```text
app/
  api/                 # FastAPI routers and DTOs
  core/                # forbidden-zone pure kernels and domain contracts
  services/            # query, replay, evidence and notification services
  web/                 # Streamlit multipage Situation Room
  workers/             # collector, evidence and model jobs
  models/              # risk-router load/inference contract
  storage/             # SQLite repository and PostgreSQL upgrade adapter
replay/
  fixtures/
  cases/
deployment/
  Dockerfile
  compose.yml
  Caddyfile
  backup/
tests/
  unit/
  contract/
  integration/
  replay/
.agent/
  architecture.md
  api_contracts.md
  data_model.md
  test_strategy.md
  deployment_runbook.md
  coding_conventions.md
  fixes.md
  forbidden_zones.md
  ai_usage.md
  sprint_1_retro.md
  sprint_2_retro.md
  sprint_3_retro.md
```

## 12. Twelve-Day Execution Plan

```yaml
sprints:
  preparation_day_1:
    goals:
      - teacher_approves_high_difficulty_self_topic
      - teacher_approves_custom_forbidden_zones_or_selects_A10_fallback
      - initialize_valid_git_and_agent_documents
      - freeze_security_and_no_trading_boundary
  design_days_2_3:
    goals:
      - human_authored_requirements_architecture_api_data_test_deployment_designs
      - repository_contract_and_web_wireframe
      - freeze_replay_cases_and_model_dataset_contract
  sprint_1_days_4_6:
    goals:
      - VPS_HTTPS_and_persistent_volume
      - continuous_worker
      - FastAPI_health_events_and_detail
      - Web_situation_room_and_event_detail
      - replay_case_runs_through_shared_downstream
  sprint_2_days_7_9:
    goals:
      - Evidence_Agent_structured_nodes
      - claim_evidence_matrix_UI
      - risk_router_baselines_and_shadow_deployment
      - Telegram_web_deep_links
      - first_blind_bug_and_change_drills
  sprint_3_days_10_11:
    goals:
      - model_data_cards_and_ablation
      - backup_restore_drill
      - coverage_and_security_audit
      - repeated_blind_bug_and_improvised_change_drills
      - one_command_demo_and_fallback
  defense_day_12:
    goals:
      - three_mode_web_demo
      - forbidden_zone_walkthrough
      - bug_injection_and_improvised_change
      - individual_contribution_proof
```

## 13. Acceptance Gates

```yaml
acceptance:
  product:
    - web_is_primary_terminal
    - telegram_alert_links_to_web_detail
    - live_recent_replay_modes_visibly_labeled
    - no_demo_step_depends_on_random_external_event
  runtime:
    - services_auto_restart_after_server_reboot
    - health_endpoint_reports_api_worker_data_model_backup
    - source_polling_continues_for_24_hours_without_duplicate_explosion
    - restore_drill_succeeds
  evidence:
    - every_major_claim_has_exact_citation_or_is_INSUFFICIENT
    - unresolved_contradiction_forces_HUMAN_REVIEW
    - model_cannot_promote_S
  engineering:
    - all_tests_pass
    - forbidden_zone_line_and_branch_coverage_above_80_percent
    - full_regression_under_60_seconds
    - targeted_forbidden_zone_tests_under_10_seconds
    - valid_git_history_and_agent_documents_exist
  examination:
    - each_member_explains_owned_forbidden_zone_in_10_minutes
    - each_sprint_contains_3_blind_injected_bugs
    - each_member_completes_at_least_3_timed_bug_drills
    - excellent_target_is_2_to_3_bugs_fixed_and_explained_within_30_minutes
    - improvised_change_is_implemented_and_regression_tested_within_30_minutes
    - internal_bug_fixtures_are_labeled_rehearsal_not_official_exam_predictions
```

## 14. 95+ Scoring Logic

```yaml
score_assessment:
  current_repository_if_graded_today: not_yet_excellent
  reason:
    - web_server_model_and_replay_are_not_implemented
    - git_agent_logs_and_coverage_evidence_are_missing
    - forbidden_zones_are_currently_too_broad
  V4_core_if_implemented_and_verified: realistic_92_to_96
  V4_high_difficulty_if_teacher_approved_and_defense_strong: realistic_95_plus
  maximum_score_dependency:
    - teacher_high_difficulty_classification
    - individual_code_understanding
    - live_bug_and_change_performance
    - evidence_backed_AI_runtime
```

The project is already excellent in research depth and data foundation. It becomes an excellent course deliverable only after the V4 operational, Web, model, process and examination gates are actually implemented.

## 15. Priority Order

1. Teacher approval of self-topic and forbidden zones.
2. Valid Git, `.agent`, human design artifacts and test-plan ownership.
3. VPS vertical slice: worker → persistent ledger → API → Web detail.
4. Shared Replay path and deterministic evidence/finality gates.
5. Evidence Agent runtime with strict schemas.
6. Small risk-router model in shadow mode plus model/data cards.
7. Telegram deep links, observability, backups and restore drill.
8. Blind bug and improvised-change training.
9. PostgreSQL or richer frontend only after all prior gates are green.

## 16. Authoritative References

- School mobilization PDF and project-list DOCX supplied by the user.
- SEC Developer Resources and Fair Access: https://www.sec.gov/about/developer-resources
- FastAPI deployment concepts: https://fastapi.tiangolo.com/deployment/concepts/
- Docker Compose production guidance: https://docs.docker.com/compose/how-tos/production/
- PostgreSQL backup and restore: https://www.postgresql.org/docs/current/backup.html
- Model Cards: https://arxiv.org/abs/1810.03993
- Datasheets for Datasets: https://arxiv.org/abs/1803.09010
