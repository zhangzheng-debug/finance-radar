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
2. the current event version contains a concrete `public_fact_summary` produced
   by a current machine fact-slot receipt or a current dual-human fact claim;
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

## Deterministic machine fact-slot closure

Machine-admitted events from **every source**, including `sec_edgar`,
`sec_current_filings`, and non-SEC P0/P1 feeds, use `event-admission-v3`.  The
current event version must retain the exact `deterministic-evidence-fact-slots-v2`
receipt, the current evidence/observation IDs, and the source content revision
that was admitted.  At least one slot must contain a predicate, action, object
and exact evidence sentence, and must bind the action either to the explicit
issuer or to an unambiguous document-issuer pronoun.  Merely mentioning the
issuer in the same sentence is not a subject binding.

The v3 relation fingerprint binds the raw passage digest, the complete fact-slot
receipt and the exact public summary.  Admission re-runs the current extractor;
historical recovery must do the same and compare slots and summary byte for
byte.  A compatible slot must be explicitly marked `event_type_compatible`, its
exact evidence sentence must remain in the selected passage, and its action and
object must remain in the public summary.  `event-admission-v1/v2`, generic
type-only summaries, stale source revisions,
unsupported issuers, denied actions and non-reproducible receipts are not
machine-reader-ready.  They remain available for human review.

## Dual-human review version closure

Applying an authorized two-reviewer consensus is a versioned fact mutation, not
just a status update.  One `BEGIN IMMEDIATE` transaction must write the new
`event_versions` row, advance `canonical_events.current_version`, and write the
matching `event_fact_workflow` receipt.  A verified decision must additionally:

- reject the batch before any write unless the selected item has an exact P0/P1
  authority tier, an HTTP(S) URL, and an exact passage of at least 40 characters;
- bind the reviewers' selected P0/P1 evidence to the new event version with a
  `HUMAN_CONFIRMED` relation;
- retain the frozen pre-application evidence fingerprint on that relation and
  workflow receipt;
- require subject match, predicate support, date coherence and `REALIZED`
  modality; and
- mark only the selected evidence as
  `accepted_dual_human_primary_evidence`.

`CONFIRM_EVENT` uses `event-fact-review-v2` and cannot accept a free-form
summary.  Each reviewer must independently submit the same controlled
`human_fact_claim`: canonical subject, safe subject basis, current event type,
exact contiguous action/object/minimal-clause quotes, realized stage/modality,
and an optional exact date quote.  `EXACT_IN_PASSAGE` is not mere co-occurrence:
the quoted clause must begin with the token-bound canonical company or
`$TICKER`, then at most one controlled auxiliary and one controlled adverb,
then the exact action quote.  The action must occur exactly once.  This rejects
one-letter matches inside unrelated words, background-company mentions,
counterparties and objects being mislabeled as the actor.

`DOCUMENT_ISSUER` is fail-closed for formal confirmation in the first v2
release, even when a SEC URL and CIK match.  A pronoun such as `the Company` or
`we` does not by itself prove which actor controls a later action.  Reviewers
must choose `NEEDS_EVIDENCE` until a separately versioned issuer-pronoun binder
exists; no SQL syntax guess silently upgrades such claims.

The public summary is rendered deterministically as
`subject + ： + exact fact sentence`.  Reviewer A and B must use the same
selected evidence ID and have byte-identical canonical claim SHA-256 and
summary SHA-256.  The consensus, authorization scope, applied facts, selected
evidence receipt, relation, and workflow bind both digests.  Apply revalidates
the current event version, source revision, passage, evidence fingerprint and
all substring/identity rules inside the write transaction.  A separate
fail-closed realized-language gate rejects future, conditional, negative and
epistemic wording (`will`, `may`, `expected`, `planned`, `denied`, and similar)
regardless of the reviewer's selected modality.  Conditional or proposed claims
cannot be published as confirmed events.  To give this rule a finite reviewable
boundary, the action quote must also begin with a versioned allowlist of
affirmative realized verb forms (for example `filed`, `issued`, `completed`,
`appointed`, `resigned`).  An unknown verb fails closed to `NEEDS_EVIDENCE`;
adding it requires a new contract/test change rather than an ad-hoc reviewer
override.

Legacy `event-fact-review-v1` rejection and `NEEDS_EVIDENCE` decisions retain
their audit meaning.  A v1 confirmation is never reader-ready and is reported
as `V1_CONFIRM_REQUIRES_FACT_CLAIM_ADDENDUM`; it must be reissued as a v2
independent addendum rather than silently upgraded.

The dual-human evidence status is reader-supportive only together with that
current-version strict relation and a matching immutable selected-evidence
receipt.  The reader recomputes the canonical claim SHA-256, public-summary
SHA-256 and selected-receipt SHA-256 rather than trusting mutually copied hash
fields.  The receipt freezes the URL, exact passage and passage digest, source
ID, source content SHA-256, authority tier, observation state, latest revision
number/kind, and pre-application evidence fingerprint.  A later official-source
revision therefore makes the event reader-ineligible until it is reviewed again,
even when a caller retries the old consensus.  It is not a shortcut around the
relation gate.

Deletion is globally reader-ineligible.  For any later edit, every reader path
fails closed unless the current content still proves the selected passage and
the branch-specific current receipt remains bound to that source revision.
This rule applies equally to standard machine evidence, SEC evidence and
dual-human evidence.
`NEEDS_EVIDENCE` advances to a current-version `NEEDS_EVIDENCE` workflow without
a supportive relation; rejection advances to `EXCLUDED` on the same basis.

Retries are idempotent only when the current version, facts receipt, exact
consensus hash, selected evidence status, relation and workflow all prove the
same completed application.  A partial, conflicting or foreign next version
fails stale/CAS checks and rolls back the entire authorized batch.

The public event API may expose only a de-identified verification summary:
method/version, application time, selected public evidence ID, independent
review count, and the no-trading boundary. Reviewer identities, rationales,
submission/consensus digests, and authorization details remain internal.

## Data, permission, cost, and rollback

This is a read-only query and presentation gate. It performs no canonical,
evidence, review, model, alert, market, or trading write. It adds no external
service and no monthly cost. The ledger remains the recovery source, so a code
rollback can remove the public filter without data migration or data loss.
