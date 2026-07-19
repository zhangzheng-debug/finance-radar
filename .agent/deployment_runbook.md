# Deployment runbook

Authoritative commands and prerequisites are in `deployment/README.md`.

## Accepted live deployment

The Singapore VPS runs an isolated systemd/Nginx deployment:

- Public Web: `https://radar.167-172-69-16.sslip.io:8443/radar/`
- Public API prefix: `https://radar.167-172-69-16.sslip.io:8443/finance-radar-api/`
- API/Web bind only to `127.0.0.1:18000` and `127.0.0.1:18501`.
- Immutable releases live in `/opt/finance-radar/releases`; mutable DB/reports live in `/opt/finance-radar/shared`.
- `finance-radar-api`, `finance-radar-web`, `finance-radar-worker`, `finance-radar-backup.timer`, and `certbot.timer` are active.
- The existing xray listener and `/root/ethusdc-pivot-bot` are outside the deployment boundary.

Run the public acceptance check from the repository:

```powershell
python scripts/collect_product_acceptance.py
```

The 2026-07-19 04:55 UTC migration snapshot binds release `20260719T044852Z`, which passed all 19 product checks plus all 17 market-capability checks, including TLS, Web/API health, Schema 12, worker success, verified restore, model shadow/no-trading, frozen external-blind evidence and promotion guard, all four replay fixtures, zero forbidden routes, zero ledger safety violations, an official raw-source evidence archive with sampled SHA-256 integrity, populated read-only event facets, and replacement-VPS fail-closed preflight. The systemd worker runs every five minutes and restarts 20 seconds after failure. Registered official evidence links are gradually archived at no more than four per cycle; safe HTTP-to-HTTPS canonicalization, final redirect revalidation, HTML/PDF/JSON MIME gates and immutable no-TTL storage apply. Pagination advances beyond already archived or persistently failing head rows so one 403 cannot permanently block later evidence. Situation Room has a global terminal search, data-backed family/source/Replay/Operations commands, and recent-event links enter the all-events flow. Event Workbench synchronizes fresh URL filters into widget state without letting stale sessions swallow a deep link. It preserves flow/family/source/query/limit/event state in the URL, offers fuzzy event-family suggestions, exact source filtering, up to eight device-local named Flows plus save/restore/delete controls, bounded previous/next controls, J/K, arrow and slash keyboard navigation through the Streamlit v2 component API, a safe non-leaking outage state, and honest read-only quote context. Operations separates event sources from Binance/Twelve/IBKR market capabilities and displays immutable evidence-archive counts and policy. User-facing controls, forms, replay steps and status copy now use one consistent Chinese interaction layer while machine-readable enums remain unchanged. Market observations use the first real observer snapshot as baseline, schedule T+5m/T+30m/T+1d follow-ups, record `MISSED_WINDOW` instead of substituting a latest quote, and keep any derived return post-event-audit-only and out of model features. Replay Lab stages evidence with Chinese run/next/show-all controls; the fourth SEC official-correction replay persists the expected `RISK_REVIEW -> CONFLICT_REVIEW / ABSTAIN` transition. The accepted ledger contains 22 sources, 3,951 raw observations, 1,194 events and 2,117 event versions.

The bounded public read-only load passed again on release `20260718T151927Z`: 120 requests at concurrency 15, 100% HTTP 200, p95 2.48 seconds and no response-envelope errors. Re-run with `python scripts/smoke_load_test.py --requests 120 --concurrency 15`.

The public API now exposes `X-RateLimit-Limit: 180` and returns the normal structured envelope with HTTP 429 when exhausted.

Daily off-host migration backup is registered in Windows Task Scheduler as `FinanceRadar-Offhost-Backup` at 02:30 with StartWhenAvailable. It invokes `scripts/pull_server_migration_backup.ps1`, uses SSH keepalive plus up to three SCP attempts, validates remote/local SHA-256 and tar integrity, encrypts at rest with AES-256-GCM+scrypt, and runs `scripts/audit_migration_restore.py` before deleting plaintext. The audit rejects path traversal, validates every manifest hash, restores only the two expected SQLite files, opens them read-only/immutable, checks both quick and full integrity, verifies release/model/safety boundaries, and atomically refreshes reviewer-facing latest JSON/Markdown reports. Seven encrypted off-host snapshots are retained; server-side verified backups retain 30 daily and 12 weekly copies. Only verified remote `/tmp/finance-radar-migration-*` staging pairs are removed. Accepted snapshot `20260719T045536Z` passed 11,091 archive members/9,860 manifest entries and contains release `20260719T044852Z`, the search/deep-link-safe five-page terminal with device-local named Flows, read-only facet commands and consistent Chinese interaction copy, 262 worker cycles, 35 backups, 83 evidence objects including 81 official raw-source snapshots, the local evidence model, persisted Binance/Twelve market context, T+ horizon jobs, replacement-VPS preflight and current Nginx configuration, while excluding the trading project and TLS keys. Its authenticated plaintext SHA-256 is `ac3ed8ba2a1ebd0f90eddfff921b39f683f17a397bfa9b2d6e074a467b24c1a5`; detailed evidence is in `reports/migration_full_restore_latest.json` and `.md`.

The same snapshot also passed the replacement-host service-preparation gate. `scripts/restore_migration_to_vps.ps1` defaults to audit-only, uses the encrypted archive plus either recovery-key copy, and persists the authenticated audit and full 1,559,757,804-byte preparation reports. `scripts/prepare_migration_restore.py` verifies all 9,860 manifest entries, extracts 9,861 regular files without following archive links, and emits a 78-link plan. Activation first executes `scripts/replacement_vps_preflight.py` remotely and fails before archive transfer unless root, Linux x86_64, resource headroom, Python/systemd/tool availability, free loopback ports, clean target paths and a simple HTTPS `/radar` URL all pass. `deployment/systemd/activate_prepared_restore.sh` requires an explicit `--activate`, refuses an existing `/opt/finance-radar` or service-unit collision, installs the loopback services, and leaves Nginx/TLS pending for the new endpoint. The current VPS is blocked as a target unless a separate override is explicitly provided. The second key copy is outside the repository under `C:\Users\MR\Documents\FinanceRadar-Recovery` with ACL inheritance disabled and one current-user access rule; it is never included in a deployment or defense pack.

Before an activated restore transfers plaintext, the orchestrator now uploads and runs `scripts/replacement_vps_preflight.py`. It fails closed on a non-root/non-x86_64/non-systemd host, missing Python/venv or Nginx/Certbot/OpenSSL tools, an existing Finance Radar tree/unit, occupied loopback ports 18000/18501/18601, less than 1 GiB available memory, insufficient calculated disk headroom, or a non-HTTPS/non-`/radar` public URL. The target report is copied back to `reports/replacement_vps_preflight_latest.json`; activation is not attempted when any check is false.

## Still-open production hardening

1. Capture 24 hours of uninterrupted worker cycles.
2. Copy the local backup key recovery material to a second safe location before the old VPS is removed; the retained migration archives are encrypted and have no persistent plaintext copy.
3. Re-run the real-browser visual/interaction/accessibility matrix for release `20260719T044852Z`; the evidence in `reports/ui_qa_20260719` proves the prior UI release and is retained as baseline, not relabeled as current.
4. The Docker/Caddy alternative remains statically validated only because Docker is absent locally.

Telegram remains dry-run by default; any external send still requires explicit `--send`.

`FinanceRadar-Runtime-Evidence` is a separate local scheduled task that runs `scripts/capture_runtime_evidence.py` every 15 minutes for 45 days. It reads only the public health endpoint, validates the existing JSONL SHA-256 chain before appending, atomically replaces the latest JSON/Markdown reports, and emits PASS only when the server-side 24-hour gate and all safety/health checks are true. Its manual scheduled-context run returned `0`; current evidence is under `reports/runtime_evidence/`.

Before a defense, travel or VPS replacement, run `python scripts/build_defense_evidence_pack.py`. The resulting ZIP under `artifacts/defense_pack/` contains the current taskbooks, acceptance/restore/runtime/model evidence and UI screenshots. The builder rejects secret-like values and forbidden paths, then verifies CRC, exact inventory and every evidence file against `MANIFEST.sha256`. It intentionally excludes environment files, recovery keys, encrypted migration archives, Telegram sending capability and the trading project. Rebuild after any material release; do not use this reviewer pack as the server restore archive.

When previewing Telegram manually on the VPS, load the systemd environment first so pending deep links do not fall back to localhost:

```bash
set -a
. /etc/finance-radar.env
set +a
cd /opt/finance-radar/current
/opt/finance-radar/venv/bin/python -m app.workers.notifier --once
```

The latest dry-run reports 11 pending alerts, zero external sends, and all event links use the direct HTTPS terminal.
