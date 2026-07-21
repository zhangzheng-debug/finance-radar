# Backup inventory

- Backup version: `2026.07.22.1`
- Application release: `20260721T184054Z`
- Accepted migration snapshot: `20260721T185507Z`

## Stored in Git

- application, API, Web terminal, workers, storage, model-routing, and replay code
- tests and GitHub Actions CI
- systemd/Nginx and Docker/Compose deployment definitions
- human-readable and AI-readable project plans
- operational runbooks, taskbooks, current acceptance state, and audit reports
- reproducible model metadata and evaluation reports (not generated model binaries)

## Stored in the private GitHub Release

The release `v2026.07.22.1` carries the current encrypted AWS recovery asset.
Its encrypted and authenticated-plaintext SHA-256 hashes are recorded in
`release/backup-20260722.json`. The preceding `v2026.07.19.1` release
continues to carry the offline demo and reviewer evidence bundle.

1. Current encrypted AWS migration snapshot: application releases, event
   history, evidence objects, reports, pinned local-model runtime/model, and
   Finance Radar service configuration. This is the production-disaster-recovery
   asset; it excludes the trading project, SSH material, and TLS private keys.
2. The prior release's offline five-page demonstration and reviewer-facing
   evidence bundles remain valid supporting artifacts.

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
