# Finance Radar 2026.08.18.3

This release closes the post-audit product loop and repairs the release and
off-host recovery contracts discovered during a real D-drive restore drill. It
adds no trading, brokerage, order, position, balance, automatic formal
verification or external-message authority.

## Product and human-review integrity

- The public shell renders before aggregate API work, bounded role-scoped
  snapshots expose their age, and non-critical metrics no longer block the
  event feed.
- Authentic-human reviewers are bound to distinct server-side credentials and
  stable principal hashes; client aliases, shared credentials and admin
  impersonation cannot manufacture independent reviews.
- The `human-blind-v3.1` workflow adds event-time samples, true issuer and event
  chain grouping, exact and near-duplicate exclusion, historical-corpus overlap
  gates and a one-way hash-bound freeze. Current authentic-human label count
  remains zero and no model promotion is claimed.
- Event cards retain same-page reading context, stable return anchors, the
  highest-authority primary-source jump and session-local change explanations.

## Release and recovery integrity

- The exact qualified SHADOW router binary is now a tracked, mandatory release
  file, bound to its SHA declaration, model card and external blind-v3 report.
  A release archive that omits the model fails before deployment instead of
  silently degrading to keyword fallback.
- Migration creation can reverify one fresh full recovery bundle, uses the
  root-backed `/var/tmp` for SQLite restore checks, and avoids unnecessary
  same-filesystem copies while retaining complete manifest and hash custody.
- Windows recovery supports an explicit local bind address, keeps large work on
  D:, separates the passphrase from ciphertext and accepts a copy only after
  authenticated-encryption round trip plus isolated full restore verification.
- Restore limits remain finite but now fit the current evidence corpus; safe
  paths, member count, exact archive manifest, recovery-bundle mapping, both
  SQLite integrity checks and every required release hash remain hard gates.

## Verification before release

- Local suite: `693 passed, 5 skipped`
- Focused release/recovery suite: `46 passed`
- Dependency-lock verification: `PASS`
- Source diff/whitespace gate: `PASS`

The failed drill artifacts are not accepted as a recovery point. A new private
D-drive ciphertext must be generated and fully restored after this release is
transactionally deployed, and the temporary `/32` SSH rule must then be
revoked. This document does not claim those post-deployment steps in advance.
