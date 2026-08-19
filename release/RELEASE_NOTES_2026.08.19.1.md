# Finance Radar 2026.08.19.1

This release closes the governance and historical-integrity findings that were
safe to implement without changing canonical facts. It adds no trading,
brokerage, order, position, balance, automatic formal verification, model
promotion or external-message authority.

## Human-review and freeze integrity

- Reviewer and Arbiter identities are derived from separate server-side
  credentials and persisted as stable principal hashes. Client aliases, shared
  credentials and cross-role impersonation cannot manufacture independent
  reviews or arbitration.
- Blind-set freezing requires an action-scoped authorization and produces a
  hash-bound receipt. Exact and near duplicates, issuer/event-chain overlap and
  normalized source-family coverage fail closed before any frozen set exists.
- The current authentic-human review count remains zero. The existing model is
  still advisory `QUALIFIED_SHADOW`; this release claims no model promotion or
  formally human-validated accuracy.

## Historical-integrity observability

- Fact-integrity history audit v2 separates model decisions, evidence edges,
  light-verification reviews and legacy configured adjudications instead of
  adding unlike units into one total.
- The audit records current-contract status and reason codes, exact event
  manifests and legacy canonical-verified counts. Its default and intended
  production use is read-only.
- Student-facing and governance material now treats versions, service state,
  test counts and recovery facts as volatile evidence that must be refreshed
  from `CURRENT_STATE.md` or the documented reproducible command.

## Verification before release

- Full local suite: `724 passed, 5 skipped`
- Git source diff/whitespace gate: `PASS`
- Dependency-lock verification: `PASS`
- Shell and PowerShell syntax gates: `PASS`
- GitHub Actions: required before tag and deployment

## Deployment and post-deployment boundary

The transactional installer must verify the tracked-source archive, release
manifest, dependency locks, pre-cutover backup and public live probe before the
current symlink changes. After activation, audit v2 will be run against the
production ledger and operations databases to produce an exact manifest and
SHA-256 receipt.

That audit does not authorize deleting, downgrading or rewriting any canonical
event. Any historical repair batch requires a separate manifest-specific,
action-scoped authorization after the production counts are reviewed.
