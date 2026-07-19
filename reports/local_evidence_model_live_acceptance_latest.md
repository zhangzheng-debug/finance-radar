# Local Evidence Model Live Acceptance

- Status: **PASS**
- Release: `/opt/finance-radar/releases/20260718T173521Z`
- Model: `qwen2.5-0.5b-instruct-q4_k_m`
- Runtime: loopback-only llama.cpp; no external inference network.
- Model task: advisory summary only; deterministic evidence records remain authoritative.
- Frozen cases: 8
- Contract / record / citation / injection: 100% / 100% / 100% / 100%
- Frozen latency p50 / p95: 2805.921 / 4606.826 ms
- Live event: `FR-LIVE-1fbb387c7ddd4c42ffaf65f674ca8f29`; model used: `True`; latency: 6318.384 ms
- Promotion decision: **REMAIN_SHADOW**; no trading endpoints or actions.

| Check | Result |
|---|---:|
| comparison_gate_pass | PASS |
| comparison_remains_shadow | PASS |
| all_frozen_cases_accepted | PASS |
| live_agent_used_local_model | PASS |
| live_model_task_summary_only | PASS |
| deterministic_gate_authoritative | PASS |
| no_trading | PASS |
| model_hash_pinned | PASS |
| model_visible_on_loopback | PASS |
| model_service_active | PASS |
| api_service_active | PASS |
| zero_model_restarts | PASS |
