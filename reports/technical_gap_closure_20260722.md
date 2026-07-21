# Finance Radar non-human technical gap closure

- Acceptance date: 2026-07-22 (Asia/Shanghai)
- AWS EC2: `i-0fa9bfafa5eab00bf` (`18.208.34.152`)
- Public terminal: `https://radar.18-208-34-152.sslip.io:8443/radar/`
- API mode: live, read-only, no trading
- Active application release: `/opt/finance-radar/releases/20260721T184054Z`

## Closed gaps

1. **Durable migration and off-host recovery**
   - A complete AWS migration archive was restored in an isolated directory and passed SQLite integrity checks.
   - The off-host artifact is encrypted with AES-256-GCM and excluded from Git.
   - A Windows scheduled task runs the pull-and-verify workflow daily at 03:30; a manual execution completed with result `0`.
   - The public operations page exposes only non-secret recovery status.

2. **Professional live terminal**
   - Evidence Terminal v2 is deployed as the AWS read-only web terminal.
   - Live API data and frozen UI fixtures are visibly separated.
   - Situation, Workbench, Replay, Operations/Model and Adjudication views are available.
   - The terminal shows true Worker, source, evidence, backup, model and adjudication states.

3. **Evidence retention**
   - Operations Schema 4 distinguishes exact excerpts from immutable source snapshots.
   - Official `text/plain` documents can be archived.
   - Persistent fetch failures use backoff, so one blocked official page cannot stall the archive queue.

4. **Issuer and market context**
   - SEC filing events are mapped to factual tickers through the official SEC company-ticker index.
   - Candidate events may receive identity metadata, but market observation remains gated to verified events.
   - Market context is read-only, direction is `ABSTAIN`, and missed windows remain explicitly unavailable.

5. **Source observability**
   - Every registered source is classified as actively polled or static imported.
   - OpenNews polling is recorded instead of appearing as an unobserved source.
   - The public UI distinguishes live cursor freshness from historical/static sources.

6. **Telegram alert safety**
   - Old pending alerts were expired instead of being replayed.
   - One audited operational test was delivered successfully.
   - The continuous Worker is configured with `--send`; only future eligible verified events can enter delivery.
   - Credentials remain only in the server environment and are excluded from Git and migration artifacts.

## Acceptance snapshot

- Ledger Schema: 12, `quick_check=ok`
- Operations Schema: 4, `quick_check=ok`
- Events: 1,670 total; 546 verified; 773 review candidates
- Evidence: 2,399 evidence edges; 1,595 immutable evidence objects
- Sources: 22 registered; actively polled and static imports are separately reported
- Worker: `SUCCESS`, 300-second interval, Telegram sending enabled
- Online backups: 48, latest state `VERIFIED`
- Off-host recovery: `VERIFIED`, full isolated restore `PASS`
- Safety audit: 0 trading-boundary violations, 0 auto-verification violations, 0 market-feature leakage violations
- Automated verification: 364 tests and 17 subtests passed
- Browser acceptance: Situation, Operations/Model and Adjudication pages loaded from the public AWS URL with live API data

## Deliberately deferred human work

These are not technical faults and must not be automated away:

- complete the 24-item dual-review blind adjudication queue;
- resolve primary-evidence and evidence-review queues through human judgment;
- create a new label-first external blind set before any model promotion.

The current external blind evaluation remains a visible `FAIL` and the model remains `SHADOW`. This is the correct safety state: the model has high downside-risk recall but is not a reliable general positive/negative news classifier.
