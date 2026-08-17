# Backup inventory

> Historical recovery inventory for `2026.07.22.2`. Its former “private
> GitHub Release” assumption is superseded: this repository is public, so no
> new production migration archive may be uploaded to its Releases. See
> `SECURITY.md` and `docs/GITHUB_BACKUP_AND_RELEASE_WORKFLOW.md`.

- Backup version: `2026.07.22.2`
- Application release: `20260722T084500Z`
- Accepted migration snapshot: `20260722T084527Z`
- Restored ledger proof: `1,872` events and `3,101` evidence rows
- Shadow model: `risk-router-v4-c82cfde20465` (`QUALIFIED_SHADOW`, no trading)

## Stored in Git

- application, API, Web terminal, workers, storage, model-routing, and replay code
- tests and GitHub Actions CI
- systemd/Nginx and Docker/Compose deployment definitions
- human-readable and AI-readable project plans
- operational runbooks, taskbooks, current acceptance state, and audit reports
- reproducible model metadata and evaluation reports (not generated model binaries)

## Historically stored in the GitHub Release

The release `v2026.07.22.2` historically received an encrypted AWS recovery
asset and the exact qualified SHADOW model binary. Because the repository is
public, this is a legacy exception rather than an approved storage pattern; do
not upload a replacement production snapshot there.
Its encrypted and authenticated-plaintext SHA-256 hashes are recorded in
`release/backup-20260722.json`. The preceding `v2026.07.22.1` remains the
prior AWS snapshot; `v2026.07.19.1` continues to carry the offline demo and
reviewer evidence bundle.

1. Current encrypted AWS migration snapshot: application releases, event
   history, evidence objects, reports, pinned local-model runtime/model, and
   Finance Radar service configuration. This is the production-disaster-recovery
   asset; it excludes the trading project, SSH material, and TLS private keys.
2. The prior release's offline five-page demonstration and reviewer-facing
   evidence bundles remain valid supporting artifacts.
3. The isolated restore audit verifies the blind-v3 report, model card,
   declared SHA-256, and recovered `risk_router.joblib` as one matching chain.

## Deliberately local only

- encryption passphrase and recovery-key copies
- `.env`, Telegram sessions/channel list, SSH keys, and provider credentials
- plaintext SQLite databases and transient caches
- older duplicate deployment, defense, demo, and migration archives

The duplicate archives are not independent recovery points: the accepted
encrypted migration snapshot contains the complete server state and all prior
Finance Radar releases. Keeping duplicates out of Git makes cloning and future
updates predictable.

## Restore priority

For a server replacement, follow `docs/SERVER_MIGRATION_HANDOFF.md`. For a
presentation without a server, use the offline-demo Release asset. For normal
development, clone the repository, copy `.env.example` to `.env`, install
`requirements-dev.txt`, and run the commands in `README.md`.
