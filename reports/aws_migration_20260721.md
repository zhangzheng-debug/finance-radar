> **Historical infrastructure record — superseded.** The static-terminal
> cutover described below was retired on 2026-08-05. It is not evidence of the
> current production route: do not redeploy its artifact, Nginx source, or
> rollback instructions. The live public UI is the single Streamlit route.

# Finance Radar AWS migration record

- Migration date: 2026-07-21 (Asia/Shanghai)
- Source: Singapore VPS `167.172.69.16`
- Target: AWS EC2 `i-0fa9bfafa5eab00bf` (`us-vpn-news-1`, `18.208.34.152`)
- Scope: Finance Radar releases, mutable ledger and operations databases, evidence objects, reports, operational backups, local evidence model, service definitions, environment configuration, and preserved Nginx configuration.
- Explicit exclusions: trading project, VPN/Xray/WireGuard configuration, and old TLS private keys.

## Cutover evidence

- Source freeze: `2026-07-20T18:17:21Z`
- Accepted release: `/opt/finance-radar/releases/20260719T044852Z`
- Source database `PRAGMA quick_check`: `ok`
- Source counts at freeze:
  - raw observations: `4574`
  - canonical events: `1350`
  - event versions: `2304`
  - evidence edges: `2396`
  - evidence objects: `1313`
- Source ledger SHA-256: `0e85a2abbd82397e13d48da9915960e9fbce4f300cc43db59b3c89e40867bf17`
- Source operations SHA-256: `69beaef8b3945bd6d971882a465c3fc0e46e7a2b3716d20f13bc7257cdbf13f2`
- Final transferred AWS hashes matched both source hashes before AWS services were started.

## AWS runtime result

- Ubuntu 26.04 LTS, Python 3.14 virtual environment rebuilt from the pinned project requirements.
- Original source Python 3.12 virtual environment preserved at `/opt/finance-radar/venv-source-py312`.
- Added persistent 2 GiB swap because the EC2 instance has about 1 GiB RAM.
- Services enabled and active:
  - `finance-radar-api.service`
  - `finance-radar-web.service`
  - `finance-radar-worker.service`
  - `finance-radar-evidence-llm.service`
  - `finance-radar-backup.timer`
- First AWS worker cycle: `SUCCESS`, `NRestarts=0`.
- The first AWS cycle collected one additional SEC event:
  - raw observations: `4575`
  - canonical events: `1351`
  - event versions: `2306`
  - evidence edges: `2396`
- First AWS backup completed with `Result=success` and `ExecMainStatus=0`.
- AWS disk after migration and backup: `9.1 GiB used / 9.2 GiB free` on the 19 GiB root filesystem.
- Telegram remains `dry_run`; the system remains read-only and has no trading capability.
- A controlled EC2 reboot completed successfully. The public API recovered automatically after about 28 seconds.
- Post-reboot health check: schema `12`, database `quick_check=ok`, `1351` canonical events, `4575` raw observations, `2306` event versions, and `2396` evidence edges.
- Post-reboot Worker cycle finished `SUCCESS` at `2026-07-20T18:27:36.894545+00:00`, with `NRestarts=0`.
- Post-reboot backup state remained `VERIFIED`; the most recent recorded backup was created at `2026-07-20T18:22:19.651495+00:00`.
- Existing AWS `xray` and `wg-quick@wg0` services also recovered as `active`; their configuration was not changed by this migration.

## Network status

- Certificate issued for `radar.18-208-34-152.sslip.io`, expiring 2026-10-18 with automatic Certbot renewal configured.
- Nginx configuration validates successfully and listens on target port `8443`.
- AWS security group `sg-0e1efc94caea62166` (`launch-wizard-1`) now permits inbound TCP `8443` from `0.0.0.0/0`; all pre-existing rules were preserved.
- Public Web terminal: `https://radar.18-208-34-152.sslip.io:8443/radar/`
- Public API health: `https://radar.18-208-34-152.sslip.io:8443/finance-radar-api/api/v1/health`
- Public API overview: `https://radar.18-208-34-152.sslip.io:8443/finance-radar-api/api/v1/overview`
- External verification returned HTTP `200` for both Web and API after the reboot.

## Source and rollback state

- Singapore Finance Radar Worker, API, Web, evidence model, and backup timer are stopped to prevent divergent dual writers.
- The temporary AWS-to-Singapore migration SSH authorization was revoked after hash verification.
- Existing Singapore quant trading software and VPN services were not changed.
- Emergency rollback command:

```powershell
ssh -i C:\Users\MR\.ssh1\id_ed25519 root@167.172.69.16 "systemctl enable --now finance-radar-evidence-llm finance-radar-api finance-radar-web finance-radar-worker finance-radar-backup.timer"
```

## Evidence Terminal v2 cutover

- Cutover completed at approximately `2026-07-20T18:45Z`.
- Production UI: `https://radar.18-208-34-152.sslip.io:8443/radar/`
- Artifact: `claudeUI/prototype/index.html`
- Production artifact path: `/var/www/finance-radar-terminal/index.html`
- Artifact SHA-256: `61f0c011188e58dbb7a3d9cbaaf80be759ab4b32693c517caf7667e7e7342a47`
- Nginx configuration source: `claudeUI/deployment/nginx-finance-radar-static.conf`
- Previous Nginx configuration backup: `/opt/finance-radar/ui-backups/20260720T184500Z/finance-radar-aws.conf`
- The existing Streamlit backend remains active on `127.0.0.1:18501` as a rollback backend; the cutover changed only Nginx routing.
- Public verification: HTTP `200`, `LIVE CORE · READ ONLY`, schema `12`, `quick_check=ok`, zero horizontal overflow, and all five terminal views navigable.
- Live workbench verification loaded a real SEC event and its timeline, decision context, evidence gate, and read-only market-context state.
- Post-cutover system state: API, Streamlit backend, Worker, evidence model, and Nginx all `active`; latest Worker cycle `SUCCESS`; backup `VERIFIED`; trading-boundary violations `0`.
