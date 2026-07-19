# Unified Research Quality Snapshot

Generated: `2026-07-16T17:51:28.079689+00:00`

## What matters now

1. **adjudicate_primary_evidence** - Primary evidence exists, but review throughput is below discovery throughput. `historical_review_threads_adjudicated=10/137; live_pending=3`
2. **expand_primary_evidence_coverage** - Candidates without a relevant passage cannot be promoted or rejected safely. `historical_keyword_passage_threads=44/137`
3. **measure_false_positive_controls_by_family** - Mergers, redemptions and stale price-cause matches can look severe but are not equity-death labels. `reviewed_rejected=351/792`

## Live event stream

- Pending manual review: `3`
- Primary text ready: `3` / `3` (100.0%)
- Review score 80+: `2`

## Historical Sharadar + SEC research

- Queue: `150`
- Unique review threads after sibling-detector collapse: `137`
- Review threads with keyword evidence passage: `44` / `137` (32.1%)
- Adjudicated review threads: `10` / `137` (7.3%)
- Verified / rejected: `441` / `351`
- S or A++ labels after review: `107`

## Interpretation

The bottleneck is no longer basic source connectivity. It is converting primary evidence into reviewed, auditable labels while preserving false-positive controls. Discovery should continue in the background, but reviewer throughput and evidence coverage are the gating metrics.

## Safety invariants

- Post-event market outcomes are audit-only and are not ranking inputs.
- No automatic label promotion.
- No trading or order path.
