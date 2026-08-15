# Changelog

This project uses date-based versions: `YYYY.MM.DD.N`. Git tags and GitHub
Releases use the same version prefixed by `v`.

## Unreleased

- Added reproducible, hash-locked Python 3.12 runtime/development dependencies
  and made CI/deployment verify the lock inputs before installation.
- Separated Public, Reviewer, Operator and Admin navigation, loopback services,
  tokens and API capabilities; internal UIs remain manual and mutually exclusive.
- Added browser-local public research views, Today/Needs attention/Follow-up
  entry points and measured-or-unavailable product quality metrics.
- Fixed collector clock drift, proxy-aware bounded rate limiting, constant-time
  token checks, stale evidence decisions, backup locking/status truthfulness and
  worker lease renewal.
- Reconciled the independent Claude repository audit with the current branch and
  retained the original report as historical evidence.
- Removed only byte-identical duplicate report renders and generated coverage
  files, preserving one representative and complete Git recoverability.
- Replaced executable AWS endpoint and workstation Playwright path constants
  with explicit deployment parameters or environment variables.
- Made dependency-lock digests portable across LF/CRLF checkouts and required
  the extracted systemd candidate to verify both runtime and development locks
  before any backup, package installation or cutover mutation.

- Consolidated the public product around one read-only Streamlit UI and marked
  the retired static prototype and its deployment records as historical only.
- Hardened evidence-policy reporting, API health payloads, memory-bounded
  systemd services, verified backup rotation, restore receipts, release
  identity, and rollback/cutover gates.
- Added a bounded repository-state record and free CI checks for whitespace,
  systemd shell syntax, high-confidence credential formats, and prohibited
  trading write routes.
- The exact commit proposed for merge must run the complete suite again in a
  clean locked environment and in GitHub Actions before this section is released.

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
