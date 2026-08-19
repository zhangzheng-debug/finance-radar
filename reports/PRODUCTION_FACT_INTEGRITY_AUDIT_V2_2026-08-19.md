# Production fact-integrity history audit v2 — 2026-08-19

## Outcome

`fact-integrity-history-audit-v2` was rerun against the live US production ledger and
operations database after the transactional deployment of `v2026.08.19.1`. The run
completed successfully at `2026-08-19T06:57:03.200960+00:00` and explicitly reported:

- `read_only=true`
- `canonical_mutation_attempted=false`
- no model promotion, trading action, OS update, or reboot

This report is an aggregate, repository-safe receipt. Exact event IDs, passages, and
affected-row manifests remain on the restricted production host and are committed by
cryptographic hashes below.

## Production input boundary

- instance: `i-0fa9bfafa5eab00bf` (`us-vpn-news-1`, `us-east-1`)
- release: `20260819T062521Z-fb9b61fb0aa0` / `2026.08.19.1`
- ledger: `/opt/finance-radar/shared/data/finance_radar.sqlite3`
- operations: `/opt/finance-radar/shared/data/finance_radar_operations.sqlite3`
- legacy review config:
  `/opt/finance-radar/current/config/live_primary_adjudications.json`
- contract: `fact-integrity-history-audit-v2`

## Aggregate findings

| Class | Current classification | Count | Meaning |
|---|---|---:|---|
| Evidence Agent decision | `CURRENT_CONTRACT` | 271 | Compatible with the current evidence contract |
| Evidence Agent decision | `STALE_CONTRACT_REQUIRES_RERUN` | 4,275 | Must be rerun under the current contract before reliance |
| Evidence Agent decision | `EDGE_REJECTED_BY_CURRENT_RELEVANCE_GATE` | 6,499 | Contains an old evidence edge rejected by the current relevance gate |
| Rejected old edge | current relevance gate rejection | 6,499 | Exact rejected-edge count, not an estimate |
| Light formalization | `CURRENT_GATE_REQUIRES_REVIEW` | 2,729 | All have contract-version mismatch and current-gate-not-supported reasons |
| Legacy review config | `LEGACY_REVIEW_CONFIG_UNPROVEN_PROVENANCE` | 105 | All 105 are unproven and currently map to canonical `verified` |

The exact affected-manifest sizes are 10,774 Evidence Agent decisions, 2,729 light
formalizations, and 105 legacy-unproven canonical verified rows. These sets are not
safe to add together as if they were one unit: they describe decisions, formalization
records, and canonical rows respectively.

## Exact receipts

- JSON manifest:
  `/opt/finance-radar/shared/reports/fact_integrity_history_audit_v2_20260819.json`
  - bytes: `9,604,616`
  - file SHA-256:
    `a30ee6776eea0f8d2b09f5f80f4e91d6db472055b59d439abf380aa4d9d78e1f`
- Markdown receipt:
  `/opt/finance-radar/shared/reports/fact_integrity_history_audit_v2_20260819.md`
  - bytes: `1,857`
  - file SHA-256:
    `c590afd0b19db1ec48e6cc6d60b1e2380e9de1781af99ea20dabfdc669555f25`
- normalized JSON payload hash recorded by the audit:
  `d6b755a7ece36975bf0108b5c5303c877fe446dc7bf44a15cf8e5d9948f6cd91`

## Authority boundary and next action

This audit authorizes no canonical mutation. The next implementation step must start
from the exact production manifests above, separate each historical-debt class, and
produce bounded dry-run proposals. Any write requires a new authorization that is:

1. action-specific and tied to the normalized payload hash;
2. scoped to an exact manifest and maximum row budget;
3. time-limited, fail-closed, and protected by current-version/evidence-fingerprint
   compare-and-swap checks;
4. followed by an independent post-write reconciliation and fresh recovery point.

Until then, public conclusions affected by these manifests must not be presented as
newly reverified under the current contract, and none of these rows may be promoted to
training truth.

## Access closure

After all production evidence was captured, the temporary EC2 Instance Connect rule
`sgr-0813614bd680afd8c` (TCP 22 from AWS managed prefix list
`pl-0e4bcff02b13bef1e`) was deleted with explicit action-time authorization. The AWS
console confirmed success and the inbound-rule count changed from 7 to 6. Existing SSH
rule `sgr-018f725a61dfbd882 / 159.89.226.240/32` and all non-target rules were preserved.
