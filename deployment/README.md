# Finance Radar deployment

Two supported deployment shapes exist. The current Singapore VPS uses systemd + Nginx and is live; Docker Compose + Caddy remains the portable alternative. Telegram delivery is always opt-in so a default deployment cannot send messages accidentally.

## VPS preparation

1. Install Docker Engine and the Compose plugin.
2. Copy the repository and existing `data/finance_radar.sqlite3` to the VPS.
3. Create `.env` from `.env.example`; set a strong `FINANCE_RADAR_ADMIN_TOKEN`, public domain and SEC user agent.
4. Give container UID 10001 write access to `data/`, `artifacts/` and `reports/`.
5. Train or copy the risk-router artifact before starting the stack.

```bash
python scripts/train_risk_router.py
docker compose -f deployment/compose.yml config
docker compose -f deployment/compose.yml up -d --build
docker compose -f deployment/compose.yml ps
curl -fsS https://YOUR_DOMAIN/api/v1/health
```

Enable Telegram only after a dry-run and explicit operator review:

```bash
docker compose -f deployment/compose.yml run --rm notifier python -m app.workers.notifier --once
docker compose -f deployment/compose.yml --profile notifications up -d notifier
```

Backup and restore verification is automatic once per day and can also be run manually:

```bash
docker compose -f deployment/compose.yml run --rm backup python -m app.ops.backup backup
```

No service exposes order, position, balance, brokerage-account or trade-execution endpoints.

## Encrypted migration restore audit

The Windows off-host job does more than download and hash the archive. Before it removes temporary plaintext, it calls `scripts/audit_migration_restore.py` to scan archive paths/links, verify every `MANIFEST.sha256` entry, restore only the ledger and operations SQLite files, run `PRAGMA quick_check` plus `PRAGMA integrity_check`, and verify the immutable current release, external-blind promotion guard and no-trading exclusions.

Manual verification of the accepted snapshot:

```powershell
python scripts/audit_migration_restore.py `
  server_migration_backup/20260719T045536Z/finance-radar-migration-20260719T045536Z.tgz.aesgcm `
  --expected-release 20260719T044852Z `
  --expected-sha256 ac3ed8ba2a1ebd0f90eddfff921b39f683f17a397bfa9b2d6e074a467b24c1a5
```

The audit never extracts arbitrary archive paths and never prints the environment file or backup passphrase. Its temporary plaintext workspace is removed after success or failure.

## Replacement VPS cutover

The latest snapshot has also passed a full service-restore preparation drill, not only the selective two-database audit. `scripts/prepare_migration_restore.py` authenticated all 9,860 manifest entries, materialized 9,861 regular files (1,559,757,804 unpacked bytes), skipped all 78 archive symlinks, and produced an explicit 78-link activation plan. It includes release `20260719T044852Z`, the pinned llama.cpp runtime, GGUF model, operations Schema 3, 83 evidence objects including 81 official raw-source snapshots, honest T+ horizon jobs and 24 unlabeled adjudication tasks. The worker runs every five minutes; event/evidence records have no TTL; online backups retain 30 daily and 12 weekly copies. Evidence pagination continues beyond archived or persistently failing head rows. Evidence is in `reports/migration_service_restore_drill_latest.json` and `.md`.

Run the Windows orchestrator without `-Activate` first. This uses the encrypted archive and can use the second ACL-restricted recovery-key copy outside the repository:

```powershell
.\scripts\restore_migration_to_vps.ps1 `
  -EncryptedArchive "server_migration_backup\20260719T045536Z\finance-radar-migration-20260719T045536Z.tgz.aesgcm" `
  -PassphraseFile "C:\Users\MR\Documents\FinanceRadar-Recovery\finance-radar-backup-passphrase.txt" `
  -ExpectedRelease 20260719T044852Z `
  -ExpectedSha256 ac3ed8ba2a1ebd0f90eddfff921b39f683f17a397bfa9b2d6e074a467b24c1a5
```

Only after a clean replacement VPS is available, add the new target and the explicit activation gate:

```powershell
.\scripts\restore_migration_to_vps.ps1 `
  -SshHost root@NEW_VPS_IP `
  -IdentityFile "C:\path\to\new-vps-key" `
  -EncryptedArchive "server_migration_backup\20260719T045536Z\finance-radar-migration-20260719T045536Z.tgz.aesgcm" `
  -PassphraseFile "C:\Users\MR\Documents\FinanceRadar-Recovery\finance-radar-backup-passphrase.txt" `
  -ExpectedRelease 20260719T044852Z `
  -ExpectedSha256 ac3ed8ba2a1ebd0f90eddfff921b39f683f17a397bfa9b2d6e074a467b24c1a5 `
  -PublicWebUrl "https://NEW_DOMAIN/radar" `
  -Activate
```

Before transferring plaintext, activation runs `scripts/replacement_vps_preflight.py` on the target. It requires a clean x86_64 systemd host, Python/venv runtime modules, the core and Nginx/Certbot/OpenSSL tools, free loopback ports 18000/18501/18601, at least 1 GiB available memory, calculated disk headroom, and a simple HTTPS `/radar` URL. The report is copied back to `reports/replacement_vps_preflight_latest.json`; any false check stops before archive transfer. Activation also refuses the current Singapore IP, refuses an existing `/opt/finance-radar` or Finance Radar unit, and never restores the trading project. It installs and health-checks API/Web/Worker/backup services but leaves certificate issuance and Nginx cutover separate. Do not decommission the old VPS until the new endpoint passes the current 19-check product audit, 17-check market audit, and a fresh off-host backup.

## Existing non-Docker VPS

For the current Singapore VPS, `deployment/systemd/` provides isolated services on loopback ports 18000/18501. Mutable data lives in `/opt/finance-radar/shared`; releases remain immutable and rollbackable.

The Evidence Agent also has an optional, independent loopback service on port
18601. `install_local_evidence_model.sh` pins both llama.cpp and the GGUF model
by SHA-256, enforces a resource gate, and requires explicit `--activate`.
The API does not require this service to start: any timeout or contract failure
falls back to deterministic evidence gates. The model performs summary-only
shadow work and cannot classify claims, assign final status, or trade. See
`docs/LOCAL_EVIDENCE_MODEL.md` and the frozen comparison reports.

Primary public endpoint:

- `https://radar.167-172-69-16.sslip.io:8443/radar/`
- `https://radar.167-172-69-16.sslip.io:8443/finance-radar-api/`

The original `https://sg.zb1og.cn/radar/` route is retained, but Cloudflare may require a human challenge. The direct endpoint uses a Let's Encrypt certificate and Certbot renewal hook. Every Nginx installer validates with `nginx -t`, retains a timestamped rollback copy, and restores it automatically if reload fails. Existing xray listeners and the trading project remain untouched.

For a manual Telegram preview on this VPS, source `/etc/finance-radar.env` before running `python -m app.workers.notifier --once`; otherwise a standalone SSH shell does not inherit the public Web URL used by systemd. The preview refreshes only `PENDING/RETRY` payloads and never sends without `--send`.
