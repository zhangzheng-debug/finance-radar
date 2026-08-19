# Finance Radar deployment

Two supported deployment shapes exist. The production-qualified shape is a
resource-bounded systemd + Nginx host; Docker Compose + Caddy remains the
portable, non-production alternative until it has equivalent resource and role
mutual-exclusion gates. Host, region and retired-topology facts are deliberately
kept out of this evergreen runbook: verify them through `CURRENT_STATE.md` and a
fresh authenticated inventory. Telegram delivery is always opt-in so a default
deployment cannot send messages accidentally.

Before packaging or cutover, generate and verify a source/archive-bound release
manifest and rollback checklist. The cross-platform workflow and optional
systemd installer gate are documented in
[`RELEASE_AUDIT.md`](RELEASE_AUDIT.md).

## VPS preparation

1. Install Docker Engine and the Compose plugin.
2. Copy the repository and existing `data/finance_radar.sqlite3` to the VPS.
3. Create `.env` from `.env.example`; set distinct strong `FINANCE_RADAR_REVIEWER_TOKEN`, `FINANCE_RADAR_OPERATOR_TOKEN` and `FINANCE_RADAR_ADMIN_TOKEN` values, plus the public domain and SEC user agent. These tokens are for loopback internal UIs only; the public Web service must never receive them. Human blind-review writes additionally require `FINANCE_RADAR_REVIEWER_PRINCIPALS_JSON`: a distinct token per real reviewer/arbiter, never one shared alias.
4. Give container UID 10001 write access to `data/`, `artifacts/` and `reports/`.
5. Train or copy the risk-router artifact before starting the stack.

```bash
python scripts/train_risk_router.py
docker compose -f deployment/compose.yml config
docker compose -f deployment/compose.yml up -d --build
docker compose -f deployment/compose.yml ps
curl -fsS https://YOUR_DOMAIN/ >/dev/null
```

The portable Caddy edge deliberately returns `404` for `/api/*`, `/docs`,
`/openapi.json`, and `/finance-radar-api/*`. Run health checks from inside the
private Compose network instead:

```bash
docker compose -f deployment/compose.yml exec api \
  python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=5)"
```

The public `web` container does not load `.env`; it receives only its private
API URL, `FINANCE_RADAR_UI_ROLE=public`, and debug-off. When internal access is
needed, use exactly one opt-in internal profile. All bind only to host loopback:

```bash
docker compose -f deployment/compose.yml --profile admin up -d admin
# Open an SSH tunnel to 127.0.0.1:18502, then browse /radar-admin/ locally.
docker compose -f deployment/compose.yml --profile admin stop admin

docker compose -f deployment/compose.yml --profile reviewer up -d reviewer
# Reviewer: 127.0.0.1:18503/radar-review/ (human review only)
docker compose -f deployment/compose.yml --profile reviewer stop reviewer

docker compose -f deployment/compose.yml --profile operator up -d operator
# Operator: 127.0.0.1:18504/radar-ops/ (runtime/model operations only)
docker compose -f deployment/compose.yml --profile operator stop operator
```

Enable Telegram only after a dry-run and explicit operator review:

```bash
docker compose -f deployment/compose.yml run --rm notifier python -m app.workers.notifier --once
docker compose -f deployment/compose.yml --profile notifications up -d notifier
```

Backup and restore verification is automatic once per day and can also be run manually. The current retention policy keeps exactly one latest verified daily bundle and no weekly bundle; the prior bundle is removed only after the replacement passes its restore checks:

```bash
docker compose -f deployment/compose.yml run --rm backup python -m app.ops.backup backup
```

No service exposes order, position, balance, brokerage-account or trade-execution endpoints.

## Encrypted migration restore audit

The Windows off-host job does more than download and hash the archive. Before it removes temporary plaintext, it calls `scripts/audit_migration_restore.py` to scan archive paths/links, verify every `MANIFEST.sha256` entry, restore only the ledger and operations SQLite files, run `PRAGMA quick_check` plus `PRAGMA integrity_check`, and verify the immutable current release, external-blind promotion guard and no-trading exclusions. It accepts historical operations schemas 2/3/4/6 and the current schema 7 (including the light-verification, formal-mutation and atomic blind-freeze receipt tables).

The advisory local Evidence Agent is declared independently in
`config/LOCAL_EVIDENCE_MODEL_CAPABILITY.json`. A current unit file does not by
itself require a GGUF restore dependency: a declared absent model is accepted
and remains disabled after recovery. Archives created before this declaration
retain their original stricter model-hash check.

For a current release record, pass the exact ID produced by
`release_audit.py`: `YYYYMMDDTHHMMSSZ-<12-character-commit>`. The migration
audit, preparer, Windows orchestrator and Linux activation script share the
same path-safe 1-to-96-character release-ID contract; historic timestamp-only
snapshot IDs in the examples below remain accepted for backward-compatible
recovery.

Manual verification of the accepted snapshot:

```powershell
python scripts/audit_migration_restore.py `
  server_migration_backup/20260719T045536Z/finance-radar-migration-20260719T045536Z.tgz.aesgcm `
  --expected-release 20260719T044852Z `
  --expected-sha256 ac3ed8ba2a1ebd0f90eddfff921b39f683f17a397bfa9b2d6e074a467b24c1a5 `
  --workspace-root "D:\FinanceRadarScratch\migration-audit"
```

The audit never extracts arbitrary archive paths and never prints the environment file or backup passphrase. Its temporary plaintext workspace is removed after success or failure. On this Windows workstation, migration archives and their large temporary plaintext workspaces default to `D:\FinanceRadarBackups` and `D:\FinanceRadarScratch`; a historic C: archive remains recoverable by passing its explicit archive/key path.

## Replacement VPS cutover

The historical 2026-07-19 snapshot also passed a full service-restore preparation drill, not only the selective two-database audit. `scripts/prepare_migration_restore.py` authenticated all 9,860 manifest entries, materialized 9,861 regular files (1,559,757,804 unpacked bytes), skipped all 78 archive symlinks, and produced an explicit 78-link activation plan. It includes release `20260719T044852Z`, the pinned llama.cpp runtime, GGUF model, operations Schema 3, 83 evidence objects including 81 official raw-source snapshots, honest T+ horizon jobs and 24 unlabeled adjudication tasks. That historical snapshot recorded 30 daily and 12 weekly online copies; it is not the current policy. The current production policy keeps one verified daily bundle and zero weekly bundles. Evidence pagination continues beyond archived or persistently failing head rows. Evidence is in `reports/migration_service_restore_drill_latest.json` and `.md`.

Run the Windows orchestrator without `-Activate` first. This uses the encrypted archive and can use the second ACL-restricted recovery-key copy outside the repository:

```powershell
.\scripts\restore_migration_to_vps.ps1 `
  -EncryptedArchive "server_migration_backup\20260719T045536Z\finance-radar-migration-20260719T045536Z.tgz.aesgcm" `
  -PassphraseFile "D:\FinanceRadarRecovery\finance-radar-backup-passphrase.txt" `
  -ExpectedRelease 20260719T044852Z `
  -ExpectedSha256 ac3ed8ba2a1ebd0f90eddfff921b39f683f17a397bfa9b2d6e074a467b24c1a5
```

Only after a clean replacement VPS is available, add the new target and the explicit activation gate:

```powershell
.\scripts\restore_migration_to_vps.ps1 `
  -SshHost root@NEW_VPS_IP `
  -IdentityFile "C:\path\to\new-vps-key" `
  -EncryptedArchive "server_migration_backup\20260719T045536Z\finance-radar-migration-20260719T045536Z.tgz.aesgcm" `
  -PassphraseFile "D:\FinanceRadarRecovery\finance-radar-backup-passphrase.txt" `
  -ExpectedRelease 20260719T044852Z `
  -ExpectedSha256 ac3ed8ba2a1ebd0f90eddfff921b39f683f17a397bfa9b2d6e074a467b24c1a5 `
  -PublicWebUrl "https://NEW_DOMAIN/radar" `
  -Activate
```

Before transferring plaintext, activation runs `scripts/replacement_vps_preflight.py` on the target. It requires a clean x86_64 systemd host, Python/venv runtime modules, the core and Nginx/Certbot/OpenSSL tools, free loopback ports 18000/18501/18601, at least 1 GiB available memory, calculated disk headroom, and a simple HTTPS `/radar` URL. The report is copied back to `reports/replacement_vps_preflight_latest.json`; any false check stops before archive transfer. Activation also refuses the current Singapore IP, refuses an existing `/opt/finance-radar`, managed Finance Radar unit or managed environment file, and never restores the trading project. A failed activation stops/disables every installed Radar runtime unit, removes the units and environment files it created, reloads systemd, and preserves the staged tree under a timestamped `.failed-*` path. Certificate issuance and Nginx cutover remain separate for a replacement host because its new IP/domain is intentionally not embedded in the encrypted archive. By contrast, an in-place release through `install_remote.sh` has a mandatory Nginx candidate install, `nginx -t`, reload and edge-deny check stage. Do not decommission the old VPS until the new endpoint passes the current 19-check product audit, 17-check market audit, and a fresh off-host backup.

## Existing non-Docker VPS

For a systemd VPS, `deployment/systemd/` provides isolated services on loopback
ports 18000/18501. Mutable data lives in `/opt/finance-radar/shared`; releases
remain immutable and rollbackable. The public Web unit reads only
`/etc/finance-radar-public.env`, which the installer creates from three fixed,
non-secret values. It never loads `/etc/finance-radar.env` and explicitly
removes `FINANCE_RADAR_ADMIN_TOKEN` from its process environment.

Human reviewer identities are provisioned separately from the shared Reviewer
UI token. The installer creates
`/etc/finance-radar-reviewer-principals.json` as root-owned `0600` containing
`[]`, so human-label writes fail closed. Generate an owner-approved candidate
file without printing its secrets, transfer it through the authenticated
administration channel, validate it, then install and restart only the API:

```bash
python scripts/generate_reviewer_principals.py \
  --output reviewer-principals.json \
  --reviewer-id reviewer-a --reviewer-id reviewer-b --arbiter-id arbiter-c
python -m json.tool reviewer-principals.json >/dev/null
sudo install -m 0600 -o root -g root reviewer-principals.json \
  /etc/finance-radar-reviewer-principals.json
sudo systemctl restart finance-radar-api
```

Distribute each token only to its named person. The API consumes this file via
systemd `LoadCredential`; it is not copied into `/etc/finance-radar.env`, the
public Web process, migration archives, reports, logs or browser code. A
disaster restore deliberately resets the file to `[]`; principals must be
re-authorized and reprovisioned. Compose/local development may instead set the
JSON environment variable shown in `.env.example`, but production uses the
credential file.

The versioned Worker unit includes `MemoryHigh=380M`, `MemoryMax=520M` and
`TasksMax=128`. These are primary-unit limits, not a server-only drop-in, so a
normal release and a disaster restore carry the same memory safety boundary.
Verify the effective values with `systemctl show finance-radar-worker -p
MemoryHigh -p MemoryMax -p TasksMax`; inspect any older local drop-ins with
`systemctl cat finance-radar-worker` before manually removing them.

`install_remote.sh` is a health-gated transaction, not an install-only helper.
After the archive/manifest gate it refuses any active or boot-enabled internal UI and an
active backup job, launches a one-shot resource-bounded candidate backup bridge,
and records the resulting snapshot ID and manifest hash. The bridge never changes
`current`, the installed backup unit, or live operations state before cutover; it
retains the backup service's complete two-database restore verification and accepts
only a full recovery bundle. The candidate code stays root-owned and is readable,
not writable, by the runtime account while the root bridge is in progress. Before
the symlink changes, the verified bundle is revalidated and atomically moved into
root-only custody outside normal retention. Only then are superseded daily
bundles removed, and the installer proves that the post-cutover bundle plus its
isolated SQLite restore still fit on disk. The normal backup service retains only
the newest successfully restored daily bundle. A verified hold is removed only
after the post-cutover bundle and services pass; a failed cutover preserves its
root-only hold for recovery review, with two retained failed holds requiring
explicit operator review before another cutover. The optional
`FINANCE_RADAR_DEPLOY_HOLD_MODE=physical_copy` mode remains available for hosts
with capacity for three full bundles. It then records the previous release, privately
snapshots the service units, protected/public environments, Nginx candidate and
renewal hook, and the shared venv. It switches `current`, enables/restarts only
API/Web/Worker/backup-timer, verifies both loopback health endpoints, then uses
the versioned direct-endpoint installer to run `nginx -t`, reload Nginx and
check the public page plus expected `404` denials. Any failure restores the
previous symlink and touched files, reloads the prior Nginx configuration, and
restarts the prior services; the failed release remains on disk for inspection.
The manual loopback Reviewer, Operator and Admin services are neither enabled nor allowed to be active
during this cutover; an existing internal session makes the installer stop
before the symlink change rather than terminating that session.

The Evidence Agent also has an optional, independent loopback service on port
18601. `install_local_evidence_model.sh` pins both llama.cpp and the GGUF model
by SHA-256, enforces a resource gate, and requires explicit `--activate`.
The API does not require this service to start: any timeout or contract failure
falls back to deterministic evidence gates. The model performs summary-only
shadow work and cannot classify claims, assign final status, or trade. See
`docs/LOCAL_EVIDENCE_MODEL.md` and the frozen comparison reports.

## Formal light verification

The continuous Worker is observation-only and runs with `--no-light-verify`.
It never receives an evergreen authorization and never applies formal state.
`--light-verify-dry-run` can produce a read-only candidate report, while the
standalone command is also read-only unless `--apply` is explicitly requested.

An apply is a short, scoped operator batch: first review a dry-run, then create
an expiring JSON authorization contract containing the exact event IDs, current
versions, evidence fingerprints, purpose, approver, batch ID and maximum
applications. Only then invoke the matching command, for example:

```bash
python scripts/light_verify.py --limit 25 --max-applies 25 --daily-budget 100 \
  --batch-id approved-20260804-01 --apply \
  --authorization user_explicit_light_verification \
  --authorization-file /secure/path/approved-20260804-01.json
```

The command rejects a missing, expired, mismatched or broader contract. It
records the authorization context with every attempted mutation; no market
outcome, order, position, balance or trading endpoint is involved.

Primary public endpoint is deployment-specific and must be passed explicitly as
`https://YOUR_DOMAIN[:PORT]/radar/`; it is not a repository constant.

The public edge returns `404` for `/finance-radar-api/`,
`/radar/offhost-status.json`, the internal page slugs, `/radar-review/`,
`/radar-ops/` and `/radar-admin/`. FastAPI remains available only at
`http://127.0.0.1:18000` on the server. Off-host verification receipts stay on
the operator workstation under `D:\FinanceRadarBackups`; the public edge never
serves their timestamps, hashes, release identifiers or ledger counts.

The three internal UIs are manual, non-enabled, mutually conflicting systemd
services. They load only their scoped token (Admin is the explicit full-access
exception), listen on loopback, and are not referenced by Nginx. Start exactly
one for an SSH-tunnel session, then stop it:

```bash
sudo systemctl start finance-radar-admin
ssh -N -L 18502:127.0.0.1:18502 ubuntu@YOUR_SERVER
# Browse http://127.0.0.1:18502/radar-admin/ on the operator workstation.
sudo systemctl stop finance-radar-admin

sudo systemctl start finance-radar-reviewer
ssh -N -L 18503:127.0.0.1:18503 ubuntu@YOUR_SERVER
# Browse http://127.0.0.1:18503/radar-review/ for evidence review.
sudo systemctl stop finance-radar-reviewer

sudo systemctl start finance-radar-operator
ssh -N -L 18504:127.0.0.1:18504 ubuntu@YOUR_SERVER
# Browse http://127.0.0.1:18504/radar-ops/ for runtime operations.
sudo systemctl stop finance-radar-operator
```

There is intentionally no `[Install]` section in any internal UI unit, so none
can be enabled as a boot service. Do not add a public Nginx route for ports
18502, 18503 or 18504.

The retired Singapore/Cloudflare route is no longer part of production. The AWS direct endpoint uses a Let's Encrypt certificate and Certbot renewal hook. Every Nginx installer validates with `nginx -t`, retains a timestamped rollback copy, and restores it automatically if reload fails; the in-place release transaction additionally performs the versioned candidate install and expected `404` edge checks before it reports success. Existing Xray and WireGuard listeners remain outside the Finance Radar deployment scope.

For a manual Telegram preview on this VPS, source `/etc/finance-radar.env` before running `python -m app.workers.notifier --once`; otherwise a standalone SSH shell does not inherit the public Web URL used by systemd. The preview refreshes only `PENDING/RETRY` payloads and never sends without `--send`.

For an audited AWS notification cutover, install `deployment/systemd/telegram_admin.sh`, expire the pre-cutover backlog, enqueue the idempotent operational test, and run `--probe --send`. Only after that succeeds should `finance-radar-worker-send.conf` be installed as a systemd drop-in. The worker still applies a 24-hour stale-outbox cutoff before every delivery cycle.
