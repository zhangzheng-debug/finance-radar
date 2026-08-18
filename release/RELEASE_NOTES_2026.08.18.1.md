# Finance Radar 2026.08.18.1

This release restores fact-integrity boundaries and records the owner's durable
product intent. It adds no trading, brokerage, order, position, balance or
external-message authority.

## Product contract

- The product remains a personal, read-only, multi-asset evidence radar.
- Downside specialization means prioritizing material adverse events for human
  research. It is not a SHORT signal, price forecast, alert permission or trade.
- Public, Reviewer, Operator and Admin responsibilities remain separated.
- The continuous worker may collect and run advisory shadow analysis, but it
  may not perform formal light verification or mutate canonical truth from a
  standing review config.

## Fact integrity

- Evidence Agent passages must match both the target issuer and the claim;
  zero-relevance and ambiguous passages create no support edge.
- Light verification requires the target issuer and event predicate in the same
  local clause. A customer's, vendor's, affiliate's or subsidiary's event does
  not become the target issuer's event.
- Decisions created under the previous Evidence Agent contract are stale and
  must be rerun against the current event version and evidence receipt.
- The 105 legacy review-config rows are preserved as historical hints with
  unproven provenance. They are not authentic-human labels, training truth or
  continuous-worker write authority.
- A new read-only audit emits sanitized decision/event manifests and never
  changes canonical status or rewrites historical versions.

## Verification before release

- Local suite: `678 passed, 5 skipped`
- GitHub Actions: two independent triggering runs passed
- Dependency locks: `PASS`
- Python compilation: `PASS`
- Whitespace and source-diff gate: `PASS`

The exact production release identity, post-cutover recovery receipt and live
health evidence are recorded only after deployment; this document does not
claim that an uninstalled commit is already running on AWS.
