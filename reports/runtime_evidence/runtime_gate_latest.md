# Finance Radar 24-hour runtime evidence

- Captured: `2026-07-20T18:03:18.798488+00:00`
- Gate: **PASS**
- Chain sequence: `228`
- Record SHA-256: `47d98b23e02807ee03350b4d1cdef7307b64c56589b17d3a70d974f6373bbf11`
- Observed window: `57.393` / `24` hours
- Cycles: `281`; success rate: `0.992883`
- SUCCESS / DEGRADED / FAILED: `279` / `2` / `0`
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
- [x] `runtime_window_complete`

## Ledger snapshot

- Sources: `22`
- Raw observations: `4573`
- Canonical events: `1349`
- Event versions: `2303`
- Evidence rows: `2396`

`PASS` is emitted only when every safety/health check and the persisted 24-hour Worker gate are true. A known eligibility time is a lower bound, not a promise.
