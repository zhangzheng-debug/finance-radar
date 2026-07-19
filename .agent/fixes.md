# Fix log

## 2026-07-18 productization pass

- Added read-only ledger repository and separate operations-state database.
- Added versioned FastAPI envelope, four-page Web terminal and three demo modes.
- Added deterministic replay cases that cover downside risk, positive news, rumor conflict and an official SEC correction that withdraws prior alert eligibility.
- Trained first CPU risk router and wrote a machine-readable model card with leakage exclusions.
- Added an explicit positive-polarity guardrail after replay showed that a downside-specialized corpus could otherwise over-route positive news.
- Added continuous worker, backup scheduler, isolated restore verification and Telegram Web deep links.
- Fixed SQLite connection lifetime after Windows integration tests exposed locked temporary databases; connections now close explicitly.
- Added Compose/Caddy layout and CI. Docker runtime validation remains open because Docker is not installed on the local machine.
- Repaired malformed official RSS XML only after strict parsing fails; the live SEC litigation feed exercised the repair path (`xml_repaired=1`) and then produced three new events with primary evidence.
- Fixed missing SQLite commits in the operations repository. Replay, worker heartbeat, model run and backup audit rows now persist across processes and restarts; regression tests assert this.
- Migrated VPS mutable state from release-local directories to `/opt/finance-radar/shared` using copy-and-verify migration, preserving the previous release for rollback.
- Added partial-source `DEGRADED` worker semantics so a source failure is not misreported as a total pipeline outage.
- Added an independent TLS 1.3 demo endpoint after Cloudflare challenged non-browser/API access on the original domain; fixed WebSocket Origin handling by preserving the original Host and port.
- Replaced raw Worker/backup JSON in the Web terminal with evaluator-facing health metrics, per-source status and isolated-restore evidence.
