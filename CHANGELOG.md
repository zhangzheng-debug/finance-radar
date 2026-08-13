# Changelog

This project uses date-based versions: `YYYY.MM.DD.N`. Git tags and GitHub
Releases use the same version prefixed by `v`.

## Unreleased

- Consolidated the public product around one read-only Streamlit UI and marked
  the retired static prototype and its deployment records as historical only.
- Hardened evidence-policy reporting, API health payloads, memory-bounded
  systemd services, verified backup rotation, restore receipts, release
  identity, and rollback/cutover gates.
- Added a bounded repository-state record and free CI checks for whitespace,
  systemd shell syntax, high-confidence credential formats, and prohibited
  trading write routes.
- The most recent recorded full local regression for this branch is
  `628 passed, 5 skipped`. The exact commit proposed for merge must run the
  complete suite again in GitHub Actions before this section is released.

## 2026.07.22.2

- Published the last tagged recovery baseline as `v2026.07.22.2`.
- Recorded application release `20260722T084500Z` and accepted encrypted
  migration snapshot `20260722T084527Z` for disaster recovery.
- Published the exact `risk-router-v4-c82cfde20465` artifact after its recovery
  and hermetic CI checks; its governance status remains `QUALIFIED_SHADOW` and
  it has no trading authority.
- Kept credentials, recovery passphrases, plaintext databases, Telegram
  sessions, SSH material, TLS private keys, and trading projects outside Git
  and the tagged recovery asset.

## 2026.07.22.1

- Migrated the complete Finance Radar application and data history to AWS while
  keeping unrelated VPN and trading programs outside the project boundary.
- Deployed Evidence Terminal v2 with live/frozen provenance, source health,
  recovery status, shadow-model governance, and dual-review workflow states.
- Added Operations Schema 4 immutable source snapshots, failure backoff, SEC
  issuer/ticker mapping, verified-event-only market context, and safe Telegram
  delivery cutover.
- Added daily encrypted off-host backups with a complete isolated-restore audit;
  the scheduled workflow was manually executed and returned `0`.
- Kept the external blind model result visibly failed and shadow-only instead
  of training on or concealing the blind set.
- Passed 364 tests and 17 subtests.

## 2026.07.19.1

- Established the first durable GitHub backup of the complete maintainable
  source tree, project plans, deployment definitions, tests, and audit evidence.
- Recorded the accepted production release `20260719T044852Z`.
- Recorded the accepted encrypted migration snapshot `20260719T045536Z`.
- Added an update/release workflow, backup inventory, and security policy.
- Kept generated caches, plaintext databases, credentials, Telegram sessions,
  duplicate archives, and recovery passphrases outside Git history.
