# IBKR TWS read-only market-data status

Checked on 2026-07-14 (Asia/Shanghai).

## Safety boundary

- TWS account: simulated/paper account.
- TWS Socket API: enabled.
- Socket endpoint: `127.0.0.1:7497`.
- TWS **Read-Only API**: enabled and preserved.
- Probe scope: `reqMarketDataType` and `reqMktData` snapshots only.
- The probe contains no account, position, execution, order, cancel-order, or
  service-management requests.

## Proven result

The local TWS socket accepted the upgraded Python client and returned both live
spot-FX data and a delayed NYMEX crude-oil future snapshot.

EUR.USD spot FX:

| Field | Value |
|---|---:|
| Data type | Live |
| Bid | 1.1429 |
| Ask | 1.1429 |
| Close | 1.1381 |
| High | 1.14625 |
| Low | 1.13785 |

CL August 2026 future:

| Field | Value |
|---|---:|
| Data type | Delayed |
| Bid | 79.53 |
| Ask | 79.55 |
| Last | 79.55 |
| Open | 78.04 |
| High | 81.27 |
| Low | 77.84 |
| Close | 78.14 |
| Volume | 282204 |

This proves the TWS -> local Socket API -> Python -> Finance Radar read-only
market-data path.

## Remaining limitations found by the live probe

| Asset | Result | Cause / next gate |
|---|---|---|
| NYMEX CL future | PASS | Delayed snapshot returned. |
| AAPL US stock | BLOCKED | TWS error 10089: API market data needs an additional entitlement/subscription. |
| EUR.USD spot FX | PASS | Live snapshot returned after upgrading to official `ibapi 10.48.1`. |

The official API 10.48.1 package was downloaded from Interactive Brokers,
verified as Authenticode-signed by Interactive Brokers Group, Inc., extracted to
`%LOCALAPPDATA%\IBKR_TWS_API_1048`, and installed into the active Python 3.12
environment. The upgraded probe now receives prices for two of its three tested
asset classes; the remaining AAPL limitation is an account market-data
entitlement rather than a software or connectivity failure.

## Reproduce

```powershell
python scripts/ibkr_readonly_probe.py --timeout 18
python scripts/gate0_external_preflight.py
```

The latest combined Gate 0 run included IBKR and completed with 12 PASS, 3 WARN,
0 FAIL, and 5 BLOCKED.
