# Finance Radar V5.0 — AI Execution and Acceptance Specification

```yaml
document:
  id: finance-radar-v5-ai-spec
  version: "5.0"
  date: "2026-07-18"
  status: deployed_baseline_with_remaining_course_gates
  human_companion: financial_event_radar_project_proposal_v5_0_human.docx

project:
  title_zh: 基于多源证据链与实时Web控制台的金融事件情报Agent
  short_name: Finance Radar
  positioning: 全极性事实核验 + 重大下行风险优先级 + 可审计回放
  primary_surface: Web Situation Room
  secondary_surface: Telegram deep-link notification
  deployment: Singapore VPS + systemd + Nginx + HTTPS
  prohibited: [orders, positions, balances, brokerage_accounts, trade_execution]
```

## 1. Verified state

```yaml
verified_at_utc: 2026-07-18T10:24:00Z
public:
  web: https://radar.167-172-69-16.sslip.io:8443/radar/
  health: https://radar.167-172-69-16.sslip.io:8443/finance-radar-api/api/v1/health
ledger:
  schema: 12
  sources: 18
  events: 1185
  observations: 3729
  event_versions: 2108
  evidence_rows: 2394
  market_audit_rows: 1898
  safety_violations: 0
operations:
  schema: 2
  worker_cycles: 22
  backup_runs: 4
  replay_runs: 1
  agent_decisions: 1
  evidence_objects: 1
tests:
  result: 249_passed_plus_17_subtests
  duration_seconds: 8.32
  total_coverage_percent: 45
  critical_coverage:
    evidence_agent: 95
    operations: 92
    replay: 91
    api: 81
model:
  rows: 897
  split: recent_connected_issuer_event_chain_groups
  issuer_overlap: 0
  event_chain_overlap: 0
  coverage: 0.8267
  covered_accuracy: 0.9570
  mode: shadow
```

## 2. Product objective

The system shall continuously collect financial events, preserve immutable source and revision history, extract atomic claims, attach exact evidence passages, refuse unsupported or conflicted conclusions, route adverse material-risk cases for human review, and expose all state through a professional Web terminal. It shall never trade.

Negative-event specialization applies only to the risk-review model. Ingestion and fact verification remain full-polarity. Favorable and neutral cases are preserved and used as non-target controls.

## 3. Deployed architecture

```yaml
edge: Nginx HTTPS on public 8443
api: FastAPI on loopback 18000
web: Streamlit multipage on loopback 18501 with base path /radar
worker: systemd continuous collection worker
backup: systemd timer plus verified online SQLite backup
data:
  ledger: SQLite WAL Schema 12
  operations: SQLite WAL Schema 2
  evidence_objects: sha256_prefix/sha256.ext
  retention: {daily: 14, weekly: 8}
model: TFIDF word+char calibrated logistic regression, CPU, shadow
telegram: durable outbox, default dry-run, explicit --send only
```

Deployment must remain isolated from `/root/ethusdc-pivot-bot`. No code may read its credentials, balances, positions or trading functions.

## 4. Evidence Agent contract

```yaml
nodes:
  - claim_extractor
  - evidence_plan_builder
  - evidence_relation_proposer
  - cited_summary_renderer
outputs:
  - EventClaim
  - EvidenceEdge
  - AgentDecision
hard_gates:
  missing_exact_evidence: INSUFFICIENT
  unresolved_contradiction: HUMAN_REVIEW
  final_S_from_model: forbidden
  trading_action: forbidden
trace_fields:
  - trace_id
  - prompt_version
  - model_provider
  - model_snapshot
  - tool_calls
  - evidence_ids
  - guardrails
  - latency_ms
```

Current runtime provider is `deterministic_guarded_fallback`, with `llm_used=false`. This is an honest safe baseline, not an LLM claim. The next high-value upgrade is an approved LLM provider behind the same contracts, evaluated against the deterministic baseline. If unavailable, the fallback remains a valid product mode and the proposal must disclose it.

## 5. Web acceptance

```yaml
situation_room:
  - mode_badge
  - event_stream
  - verified_candidate_rejected_counts
  - source_health
  - worker_and_backup_status
event_intelligence:
  - identity_and_version
  - exact_evidence_matrix
  - model_shadow_output
  - agent_claims_edges_summary_trace
  - content_object_hashes
  - human_override_form
  - no_trading_banner
replay_lab:
  - case_selector
  - simulated_clock
  - step_state_changes
  - reset_and_rerun
operations_model:
  - api_worker_data_model_backup_health
  - source_cursors
  - model_metrics_and_hash
  - safety_counters
```

## 6. Demo contract

```yaml
LIVE:
  proves: external connectivity and current health
  success_requires_new_event: false
RECENT_CAPTURE:
  proves: real persisted history and timestamps
REPLAY:
  proves: deterministic evidence and routing behavior
  cases:
    - sec_bankruptcy_verified
    - positive_earnings_non_target
    - rumor_correction_abstain
```

Recommended three-minute path: Situation Room → recent real event → SEC replay evidence transition → conflict/positive control → model/data card → backup/trace/no-trading proof.

## 7. Model governance

```yaml
task: adverse_material_risk_routing
outputs: [RISK_REVIEW, NON_TARGET, ABSTAIN]
forbidden_outputs: [LONG, SHORT, expected_return, severity_grade, alert_permission]
split:
  time_priority: true
  issuer_grouping: required
  event_chain_grouping: required
  current_overlap: 0
artifacts:
  - risk_router.joblib
  - risk_router.sha256
  - risk_router_feature_schema.json
  - risk_router_metrics.json
  - risk_router_model_card.json
  - risk_router_model_card.md
  - risk_router_data_card.json
  - risk_router_data_card.md
  - risk_router_training_manifest.jsonl
promotion: remain_shadow_until_blind_and_drift_gates_pass
```

## 8. Remaining work — priority order

```yaml
P0_external_course_gates:
  - teacher_approves_high_difficulty_topic_and_FZ3
  - students_handwrite_three_forbidden_zones_with_authentic_commits
  - each_student_completes_timed_bug_and_improvised_change_drills
P1_runtime_evidence:
  - accumulate_24_hour_worker_proof
  - automate_encrypted_offhost_backup_sync
  - perform_service_restart_and_restore_rehearsal_before_defense
P1_AI_quality:
  - connect_approved_LLM_provider_or_local_model
  - compare_LLM_against_deterministic_fallback_on_frozen_cases
  - add_model_ablation_blind_holdout_and_drift_threshold
P2_hardening:
  - application_rate_limiting
  - small_screen_UI_QA
  - higher_concurrency_load_test
```

Do not spend core time on PostgreSQL, React, Kubernetes, Redis, Celery or automated trading while any P0/P1 item is red.

## 9. Student-only forbidden zones

```yaml
FZ1: app/core/event_fingerprint.py
FZ2: app/core/evidence_gate.py
FZ3_preferred: app/core/finality_gate.py
requirements:
  AI_generated_code_allowed: false
  student_design_notes: required
  authentic_commits: required
  unit_and_branch_coverage_percent: 80
  timed_walkthrough: required
  teacher_approval_for_FZ3: required
```

Existing AI-assisted code cannot be renamed or retroactively declared student-authored.

## 10. Acceptance gates

```yaml
already_green:
  - public_https_web_and_api
  - persistent_history
  - live_recent_replay_modes
  - structured_agent_fallback
  - content_addressed_evidence
  - shadow_model_with_zero_group_leakage
  - Telegram_dry_run_and_deep_links
  - verified_daily_weekly_backup
  - offhost_restore_proof
  - all_tests_under_60_seconds
  - safety_and_fault_drills
still_red_or_partial:
  - approved_real_LLM_comparison
  - 24_hour_runtime_window
  - automatic_encrypted_offhost_backup
  - authentic_student_forbidden_zones
  - valid_student_contribution_history
  - per_member_timed_exam_rehearsal
  - teacher_high_difficulty_approval
```

## 11. Score judgment

Engineering/product quality is already realistically excellent. A final 95+ result is plausible, not guaranteed. The remaining score risk is dominated by authentic student ownership and examination performance, not by missing dashboard features. AI must stop before writing the student-only zones or fabricating process evidence.
