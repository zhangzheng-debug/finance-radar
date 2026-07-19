# Gate 0 External Dependency Preflight

- Run (UTC): `2026-07-15T20:11:40+00:00`
- Python: `3.12.2`
- PASS: **12**
- WARN: **2**
- FAIL: **1**
- BLOCKED: **5**

`BLOCKED` means the endpoint was intentionally not called because a required identity, API key, or destination was absent. It is not an API failure.

| Status | Probe | Group | Latency | HTTP | Evidence |
|---|---|---|---:|---:|---|
| PASS | Federal Reserve RSS | public | 16231 ms | 200 | Feed parsed and contains entries |
| PASS | BLS RSS | public | 942 ms | 200 | Feed parsed and contains entries |
| PASS | BLS Public Data API | public | 2237 ms | 200 | BLS public API returned CPI observations |
| WARN | GDELT DOC API | public | 25597 ms | - | URLError: <urlopen error _ssl.c:983: The handshake operation timed out> |
| PASS | Binance Spot REST | public | 798 ms | 200 | Exchange REST endpoint returned server time |
| PASS | Binance Spot Market-Data-Only REST | public | 5537 ms | 200 | Exchange REST endpoint returned server time |
| PASS | Binance USD-M Futures REST | public | 812 ms | 200 | Exchange REST endpoint returned server time |
| PASS | Binance USD-M Futures Aggregate-Trade WebSocket | public | 1219 ms | 101 | WebSocket upgraded and delivered stream bytes |
| PASS | Binance USD-M Futures Mark-Price WebSocket | public | 922 ms | 101 | WebSocket upgraded and delivered stream bytes |
| FAIL | Binance Public Quotes via Singapore SSH Relay | credentialed | 8374 ms | - | RuntimeError: ERROR: RuntimeError: remote market-data command failed (255): Connection timed out during banner exchange Connection to 167.172.69.16 port 22 timed out |
| WARN | IBKR TWS Read-Only Multi-Asset Market Data | local-app | 8783 ms | - | Received prices for 0/3 asset classes |
| PASS | SEC Submissions API | credentialed | 814 ms | 200 | SEC submissions JSON returned a valid filing list |
| BLOCKED | BEA Data API | credentialed | - | - | Missing configuration: BEA_API_KEY |
| BLOCKED | Marketaux News API | credentialed | - | - | Missing configuration: MARKETAUX_API_TOKEN |
| PASS | Twelve Data Multi-Asset Prices | credentialed | 16735 ms | 200 | Twelve Data returned stock, ETF, FX, and crypto prices |
| BLOCKED | Alpaca IEX Snapshot | credentialed | - | - | Missing configuration: APCA_API_KEY_ID, APCA_API_SECRET_KEY |
| BLOCKED | Alpaca Historical News | credentialed | - | - | Missing configuration: APCA_API_KEY_ID, APCA_API_SECRET_KEY |
| PASS | Telegram Bot getMe | credentialed | 16496 ms | 200 | Telegram read-only method succeeded |
| PASS | Telegram Bot getChat | credentialed | 16387 ms | 200 | Telegram read-only method succeeded |
| BLOCKED | FRED API | credentialed | - | - | Missing configuration: FRED_API_KEY |

## Configuration state

Only presence/absence is reported; secret values are never persisted.

- `SEC_USER_AGENT`: configured
- `BLS_API_KEY`: missing
- `BEA_API_KEY`: missing
- `MARKETAUX_API_TOKEN`: missing
- `APCA_API_KEY_ID`: missing
- `APCA_API_SECRET_KEY`: missing
- `TELEGRAM_BOT_TOKEN`: configured
- `TELEGRAM_CHAT_ID`: configured
- `FRED_API_KEY`: missing
- `TWELVE_DATA_API_KEY`: configured
- `BINANCE_REMOTE_SSH_HOST`: configured
- `BINANCE_REMOTE_SSH_PORT`: configured
- `BINANCE_REMOTE_SSH_USER`: configured
- `BINANCE_REMOTE_SSH_KEY`: configured
