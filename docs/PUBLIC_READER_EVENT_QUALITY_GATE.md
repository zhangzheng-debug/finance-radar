# Public reader event quality gate

Status: implemented locally, not evidence of production deployment
Decision date: 2026-08-20

## User and task

The primary user is the personal researcher, followed by the public reader. The
task is to answer “who did what, at what stage, and which original passage can I
open?” without making a discovery classifier look like a complete event.

## Reader-ready contract

A canonical record enters the public event feed only when all three conditions
are true:

1. `company_name` or `ticker_at_event` identifies the subject;
2. the current event version contains a structured `public_fact_summary`,
   `fact_summary`, or `evidence_summary` with enough content to state the fact;
3. at least one current-version evidence relation binds the named subject and
   event predicate to a date-coherent P0/P1 passage.  The evidence status must
   be supportive; `non_decision`, `no_keyword`, `link_only`, and incomplete
   attachments fail closed even when their text is long.

The API exposes this as `reader_ready`. Public event and facet requests use
`reader_ready=true`. Failed records are not deleted, rejected, downgraded, or
rewritten. They remain in the canonical ledger and internal review path as a
separately measured discovery backlog.

## Reader-facing behavior

- Reader-ready records keep their evidence state and can be opened from the
  public event feed.
- Discovery-only records do not inflate public event or filter counts.
- An old direct link to a discovery-only record says that it is not yet a
  readable event and lists the missing subject, structured fact, or source
  passage.
- The former subject-plus-category fallback is forbidden because text such as
  “ICX has a listing-status lead” does not identify the actual listing action or
  stage.

## Measurement and acceptance

The following must remain separately measurable:

- total canonical records;
- reader-ready records and their public-state partition;
- discovery-only records;
- non-exclusive missing-subject, missing-fact-summary, and missing-citable-
  evidence counts;
- total internal review queue, reader-ready review queue, and discovery backlog.

Acceptance requires zero records returned by the public feed without all three
reader-ready conditions, consistent reader-ready facets, truthful direct-link
fallback copy, and full regression coverage.

## Data, permission, cost, and rollback

This is a read-only query and presentation gate. It performs no canonical,
evidence, review, model, alert, market, or trading write. It adds no external
service and no monthly cost. The ledger remains the recovery source, so a code
rollback can remove the public filter without data migration or data loss.
