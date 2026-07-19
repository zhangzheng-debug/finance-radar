# Finance Radar server migration handoff

Updated: 2026-07-19 02:36 UTC

## Before-August operating plan

The local recovery point below is already sufficient to survive loss of the
current VPS. The remaining work is freshness and cutover discipline, not
emergency backup construction.

1. **Now through July 27:** keep development local-first. Do not make the
   current VPS the only copy of code, evidence, databases, model files, or
   service configuration.
2. **July 28-30:** provision the replacement VPS, run the audit-only restore
   first, then activate on the new host. Keep the old host unchanged.
3. **After activation:** verify the five services, public HTTPS, all product
   checks, model health, deep links, keyboard interactions and Telegram dry-run.
4. **Overlap window:** keep both hosts for at least one successful worker cycle
   and one successful encrypted backup from the replacement host.
5. **Final freeze:** stop writes on the old Finance Radar worker, pull one final
   encrypted snapshot, verify it locally, then switch DNS/domain and retire the
   old VPS. Never include or operate `/root/ethusdc-pivot-bot`.

If the replacement is delayed, the accepted encrypted archive plus the offline
demo remain the presentation fallback. A final pre-cutover backup is strongly
preferred because the accepted recovery point is dated 2026-07-19.

## Accepted local recovery point

- Encrypted archive:
  `server_migration_backup/20260719T045536Z/finance-radar-migration-20260719T045536Z.tgz.aesgcm`
- Current application release: `20260719T044852Z`
- Authenticated plaintext SHA-256:
  `ac3ed8ba2a1ebd0f90eddfff921b39f683f17a397bfa9b2d6e074a467b24c1a5`
- Encryption: AES-256-GCM + scrypt
- Encrypted size: 788,301,307 bytes
- Archive members: 11,091
- Manifest entries verified: 9,860 / 9,860
- Regular files prepared: 9,861
- Unpacked bytes: 1,559,757,804
- Symlinks skipped and converted to an activation plan: 78

Both restored SQLite databases passed `quick_check` and `integrity_check`.
The restored ledger is Schema 12; the operations database is Schema 3 and
contains 24 unlabeled adjudication samples plus zero fabricated reviews. The
ledger snapshot contains 22 sources, 1,194 canonical events, 3,951 raw
observations, 2,117 event versions, 2,394 evidence edges and 1,898 post-event
market metrics. The operations snapshot contains 262 worker cycles, 35 verified
backup runs, seven replay runs, seven model runs and 83 content-addressed evidence
objects. Eighty-one of those objects are raw official-source snapshots (80 HTML and
one PDF, 10,936,893 bytes); their sampled public integrity audit reports zero
SHA-256 failures.
The archive includes every Finance Radar release, shared data and reports,
systemd/Nginx configuration, the Calm Institutional UI, and the complete local
Evidence Agent runtime: pinned llama.cpp, the 491 MB Qwen2.5 GGUF model, model
service unit, frozen comparisons, initial failure evidence, and live acceptance
report. The model file SHA-256 is
`74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`.

Release `20260719T044852Z` also contains the Calm Institutional five-page
UI, deterministic public accessibility audit, Nginx `_stcore` canonicalization,
and fresh-page deep-link handshake that restores filters and Event ID. The
release adds persisted Binance public crypto observations, an explicit
Twelve/Binance/IBKR capability matrix, visible Event Workbench market context,
Situation Room quick flows, a global terminal search, URL-to-widget filter
continuity that prevents stale session state from swallowing deep links, up to
eight device-local named Flows storing only filter state, a read-only facet API,
fuzzy event-family suggestions, exact source filtering, data-backed commands and a
consistent Chinese interaction layer for controls, forms, replay steps and status
copy while preserving machine-readable enums. It
also schedules observer-relative T+5m/T+30m/T+1d captures from the first real
snapshot, records missed windows without substituting a latest quote, and keeps
all computed returns in the post-event audit channel. It also contains the
HTTPS-only official HTML/PDF snapshot worker, domain/redirect/MIME/size safety
gates, `/api/v1/evidence/archive`, and the Operations evidence-archive panel.
Public product acceptance is 19/19 and the public market capability audit is
17/17. The prior
release's real-browser interaction acceptance is 6/6
with zero console/page/HTTP errors, and the five-page desktop/mobile
accessibility machine audit was PASS with zero blockers, advisories, browser
errors, contrast failures or horizontal overflow; refresh of that browser
matrix is still required for this material UI release.

The archive excludes `/root/ethusdc-pivot-bot`, SSH material, and TLS private
keys. Both local and remote temporary plaintext were checked as absent after
the audit.

For a presentation before the replacement VPS is ready, use the separate
`artifacts/offline_demo/finance-radar-offline-demo-latest.zip`. It is not a
production restore and contains no server configuration or credentials, but it
can run the API, all five Web pages, frozen Replay and the shadow model on one
Windows machine while external Python network access is blocked. Its current
independent acceptance is in `reports/offline_demo_acceptance_latest.*`.

The recovery passphrase exists in two ACL-restricted local locations. Never put
its value in documentation, chat, a commit, or migration command history. The
operator-safe note is:

`C:\Users\MR\Documents\FinanceRadar-Recovery\RECOVERY_README.md`

## Audit before a new VPS exists

This command is audit-only by default. It decrypts in a temporary workspace,
checks every manifest entry, prepares the complete service tree including the
model, performs no SSH transfer, and removes plaintext afterward.

```powershell
.\scripts\restore_migration_to_vps.ps1 `
  -EncryptedArchive ".\server_migration_backup\20260719T045536Z\finance-radar-migration-20260719T045536Z.tgz.aesgcm" `
  -PassphraseFile "C:\Users\MR\Documents\FinanceRadar-Recovery\finance-radar-backup-passphrase.txt" `
  -ExpectedRelease 20260719T044852Z `
  -ExpectedSha256 ac3ed8ba2a1ebd0f90eddfff921b39f683f17a397bfa9b2d6e074a467b24c1a5
```

Accepted result: `AUDIT_ONLY_PASS` and `PREPARED_NOT_ACTIVATED`.

## Activate only on the replacement VPS

Do not run this against the current `167.172.69.16` server. The orchestrator
blocks that IP unless explicitly overridden and refuses a non-empty target.

Provision a clean x86_64 systemd host first. On Ubuntu/Debian the minimum tools
are `python3`, `python3-venv`, `tar`, `curl`, `nginx`, `certbot`,
`python3-certbot-nginx`, and `openssl`. Before any archive transfer, the
orchestrator now runs `scripts/replacement_vps_preflight.py` remotely and fails
closed unless all of the following pass: root execution, clean target paths,
no colliding service units, Python runtime modules, systemd, loopback ports
18000/18501/18601, at least 1 GiB available memory, restore/install disk
headroom, edge tools, and a simple HTTPS `/radar` URL. The durable result is
saved as `reports/replacement_vps_preflight_latest.json`.

```powershell
.\scripts\restore_migration_to_vps.ps1 `
  -SshHost root@NEW_VPS_IP `
  -IdentityFile "C:\path\to\new-vps-key" `
  -EncryptedArchive ".\server_migration_backup\20260719T045536Z\finance-radar-migration-20260719T045536Z.tgz.aesgcm" `
  -PassphraseFile "C:\Users\MR\Documents\FinanceRadar-Recovery\finance-radar-backup-passphrase.txt" `
  -ExpectedRelease 20260719T044852Z `
  -ExpectedSha256 ac3ed8ba2a1ebd0f90eddfff921b39f683f17a397bfa9b2d6e074a467b24c1a5 `
  -PublicWebUrl "https://NEW_DOMAIN/radar" `
  -Activate
```

Only a preflight `PASS` permits archive transfer. Activation then restores and health-checks the loopback model before API, Web,
Worker, and backup services. Nginx/TLS remains a separate step because the
certificate must match the new IP/domain.

## Cutover gate

Keep the old VPS until all of the following are true:

1. Local model `/health`, API, Web, Worker, and backup services pass on loopback.
2. Nginx and the new certificate pass `nginx -t` and public HTTPS checks.
3. `python scripts/collect_product_acceptance.py` reports all checks true against
   the new endpoint.
4. `python scripts/evaluate_local_evidence_model.py` and
   `python scripts/accept_local_evidence_model.py` both report PASS.
5. A new off-host encrypted backup is pulled from the replacement host and
   passes a full isolated restore.
6. Telegram remains dry-run unless the operator explicitly authorizes sending.

Never copy, modify, start, stop, or restore `/root/ethusdc-pivot-bot` as part of
this migration.
