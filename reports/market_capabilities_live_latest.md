# Live read-only market capability audit

- Generated: `2026-07-19T04:53:43.502450+00:00`
- Endpoint: `https://radar.167-172-69-16.sslip.io:8443/finance-radar-api/api/v1/market/capabilities`
- Status: `PASS`
- Meaning: quotes are post-event context only; they are not truth, causality, model features, or trading signals.

## Providers

| Provider | Role | Deployment | Status | Jobs completed | Snapshots | Last snapshot | Last error |
|---|---|---|---|---:|---:|---|---|
| binance_public | PERSISTED_EVENT_OBSERVATION | SERVER_DIRECT | OBSERVED | 2 | 2 | 2026-07-18T23:04:08.179391+00:00 | - |
| twelve_data | PERSISTED_EVENT_OBSERVATION | SERVER_DIRECT | OBSERVED | 3 | 3 | 2026-07-15T20:37:35.000022+00:00 | - |
| ibkr_tws_readonly | CAPABILITY_PROBE_ONLY | OPERATOR_DESKTOP | LOCAL_PROBE_ONLY | 0 | 0 | - | - |

## Machine checks

- `PASS` boundary_read_only
- `PASS` boundary_no_trading
- `PASS` boundary_no_account_data
- `PASS` boundary_post_event_only
- `PASS` boundary_not_model_feature
- `PASS` required_providers_present
- `PASS` all_providers_read_only
- `PASS` no_order_endpoints
- `PASS` binance_server_observed
- `PASS` twelve_server_observed
- `PASS` ibkr_local_probe_only
- `PASS` observer_relative_horizons_declared
- `PASS` missed_windows_never_backfilled
- `PASS` horizon_metrics_post_event_only
- `PASS` binance_public_role
- `PASS` twelve_data_role
- `PASS` ibkr_tws_readonly_role

## Boundary

- All providers are read-only and expose no order endpoint.
- Binance and Twelve Data are persisted server observations.
- IBKR TWS remains an operator-desktop capability probe; it is not a server dependency.
- T+5m/T+30m/T+1d are measured from the first real observer snapshot; missed windows are recorded, never backfilled.
