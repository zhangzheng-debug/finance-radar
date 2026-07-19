# Finance Radar 24-hour runtime evidence

- Captured: `2026-07-19T05:33:18.494345+00:00`
- Gate: **WAITING**
- Chain sequence: `83`
- Record SHA-256: `9fde6d9d32fdacf35cb8314c85b76984635e4b1ab6eab617a74e301a19047583`
- Observed window: `20.868` / `24` hours
- Cycles: `269`; success rate: `0.936803`
- SUCCESS / DEGRADED / FAILED: `252` / `14` / `3`
- Latest Worker: `SUCCESS`
- Earliest known possible pass: `2026-07-19T12:11:35.589624+00:00`

## Gate checks

- [x] `api_status_ok`
- [x] `ledger_quick_check_ok`
- [x] `operations_quick_check_ok`
- [x] `latest_worker_success`
- [x] `latest_backup_verified`
- [x] `model_shadow_no_trading`
- [x] `safety_audits_zero`
- [ ] `runtime_window_complete`

## Ledger snapshot

- Sources: `22`
- Raw observations: `3951`
- Canonical events: `1194`
- Event versions: `2117`
- Evidence rows: `2394`

`PASS` is emitted only when every safety/health check and the persisted 24-hour Worker gate are true. A known eligibility time is a lower bound, not a promise.
