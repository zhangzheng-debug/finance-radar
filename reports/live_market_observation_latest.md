# Live Read-Only Market Observation

- Newly scheduled: `0`
- Requested this run: `0`
- Completed: `0`
- Errors: `0`
- Missed windows: `13` (never backfilled with a latest quote)
- Horizon metrics written: `0`
- Scope: latest provider price only; no order, position, balance, or account endpoint exists.
- Provider policy: crypto -> Binance public spot market data; other assets -> Twelve Data.
- Neither selected price endpoint provides a source timestamp, so snapshots are explicitly marked `provider_timestamp_unavailable`.


## Job windows

| Window | Status | Count |
|---|---|---:|
| initial | CANCELLED_RELATION_DISABLED | 1 |
| initial | COMPLETED | 5 |
| t_plus_1d | MISSED_WINDOW | 3 |
| t_plus_1d | PENDING | 2 |
| t_plus_30m | MISSED_WINDOW | 5 |
| t_plus_5m | MISSED_WINDOW | 5 |

## Captures

| Event | Type | Window | Provider | Asset | Price | Captured UTC | Freshness |
|---|---|---|---|---:|---:|---|---|
| Ostium | protocol_incident_trading_paused | initial | binance_public | ARBUSDT | 0.08850000 USDT | 2026-07-18T22:56:23.222027+00:00 | provider_timestamp_unavailable |
| Ostium | protocol_incident_trading_paused | initial | binance_public | ETHUSDT | 1863.94000000 USDT | 2026-07-18T22:56:23.222027+00:00 | provider_timestamp_unavailable |
| Shamkhani shipping network | ofac_network_sanctions | initial | twelve_data | USO | 121.4 USD | 2026-07-15T20:37:35.000022+00:00 | provider_timestamp_unavailable |
| Ostium | protocol_incident_trading_paused | initial | twelve_data | ARB/USD | 0.088600002 USD | 2026-07-15T20:36:31.539583+00:00 | provider_timestamp_unavailable |
| Ostium | protocol_incident_trading_paused | initial | twelve_data | ETH/USD | 1926.45 USD | 2026-07-15T20:36:31.539583+00:00 | provider_timestamp_unavailable |
