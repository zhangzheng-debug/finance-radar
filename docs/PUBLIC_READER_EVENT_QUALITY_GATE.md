# Public event visibility and citation quality contract

Status: active repository contract; deployment still requires separate live verification
Decision date: 2026-08-23

## User and task

The primary user is the personal researcher, followed by the public reader. The
task is to let the reader browse the complete canonical event ledger while
answering three separate questions:

1. what did the source say;
2. how strong is the current evidence; and
3. how does the short-risk router assess review priority.

Internal processing progress is not an event fact and must not replace any of
those answers.

## Public visibility contract

Every canonical event is browseable in the Public feed. Visibility does not
depend on `canonical.status`, `public_state`, rough review, light verification,
dual-human review, or `citation_ready`.

Records that have not met the formal citation contract remain visible with a
truthful, bounded source-capture excerpt and explicit uncertainty. Public must
not expose unrestricted `raw_json`, internal IDs, reviewer identities,
authorization receipts, prompts, traces, secrets, or provider trading signals.
Exceptional legal, security or deletion handling may redact unsafe payloads,
but must not silently recast an event as verified or erase its audit history.

`public_state` remains a compatibility disposition only. Labels such as
“待核验”“粗审”“待补证”“已核验” are internal workflow language; they are not the
primary Public status, filter or badge.

The public API 1.x still carries `status`, `public_state`, and `reviewed_at` for
compatibility. New clients must not use them as reader trust labels. They are
deprecated for the next major public projection, where removal requires a
versioned migration rather than an in-place breaking change.

## Automatically derived citation contract

`citation_ready` is a deterministic, current-version projection. It is not a
manual approval, canonical status, or visibility gate. It is true only when all
three conditions are true:

1. `company_name` or `ticker_at_event` identifies the subject;
2. the current event version contains a concrete `public_fact_summary` produced
   by a current machine fact-slot receipt or a current dual-human fact claim;
3. at least one current-version evidence relation binds the named subject and
   event predicate to a date-coherent P0/P1 passage.  The evidence status must
   be supportive; `non_decision`, `no_keyword`, `link_only`, and incomplete
   attachments fail closed even when their text is long.

The legacy API field `reader_ready` may remain as an alias while clients migrate,
but the Public contract names the property `citation_ready`. A false value
forbids the UI from presenting a synthesized summary as a formal fact; it does
not remove the event from the feed.

The same boundary applies to the public API, not only to the bundled UI. When
`citation_ready=false`, `public_fact_summary`, `claim_subject`, `claim_action`,
`claim_stage`, and `known_at` are null and `current_version.facts` is empty.
Only a bounded `unverified_capture_excerpt` may remain, with
`summary_basis=UNVERIFIED_CAPTURE_EXCERPT` (or `NO_PUBLIC_SUMMARY`). A dormant
historical fact slot without a current qualifying relation must not cross this
boundary. When the gate passes, `summary_basis=CITATION_READY_FACT`.

Formal claims remain fail-closed. A source title, URL, filing accession, long
document, model interpretation, risk score, reviewer progress state, or
canonical `verified` label is not by itself sufficient to set
`citation_ready=true`.

## Public evidence posture

Every Public event exposes one machine-derived `evidence_posture`, independent
of workflow and risk assessment:

| Value | Public meaning |
|---|---|
| `PRIMARY_SUPPORTED` | A current-version primary passage supports the concrete fact claim; `citation_ready=true`. |
| `PRIMARY_SOURCE_AVAILABLE` | A primary source is present, but the current subject/fact/passage relation is incomplete. |
| `SOURCE_CAPTURED` | The system retained a source capture, but no qualifying primary support is currently bound. |
| `NO_SOURCE` | No displayable source capture is currently available. |

`evidence_gap_codes` explains the gap without pretending to know more than the
ledger proves. The initial codes are `MISSING_SUBJECT`,
`MISSING_FACT_SUMMARY`, `MISSING_CITABLE_EVIDENCE`, and
`NO_CAPTURED_SOURCE`. A later vocabulary extension requires a versioned
contract and tests.

## Public risk assessment

Risk assessment is a separate optional object. When current, it may expose
`route`, `confidence`, `confidence_applicable`, `model_version`,
`decision_source`, `evidence_state`, `evaluated_at`, `shadow`, and `current`.

- `route` is limited to `RISK_REVIEW`, `NON_TARGET`, or `ABSTAIN`;
- confidence is shown only when `confidence_applicable=true`; otherwise it is
  null/hidden;
- absent, stale, unapproved or undecidable assessment creates no Public badge,
  placeholder or explanation block; it is never projected as `NON_TARGET`;
- risk routing never changes evidence posture, citation readiness, canonical
  facts, alerts, or trading permissions.

## Reader-facing behavior

- All canonical records contribute to Public event and facet counts.
- Event detail exposes the evidence posture and keeps it semantically separate
  from risk assessment. The feed omits the repetitive `SOURCE_CAPTURED` chip
  while retaining distinct posture chips and concrete source attribution.
- An old direct link to a non-citation-ready record remains readable using its
  captured source text or neutral record title; internal gap inventories do not
  become repetitive Public warning copy.
- `PRIMARY_SUPPORTED` may render the concrete fact summary; all other postures
  render source-attributed, uncertainty-preserving copy rather than a confirmed
  fact.
- The former subject-plus-category fallback is forbidden because text such as
  “ICX has a listing-status lead” does not identify the actual listing action or
  stage.
- Internal workflow states stay in Reviewer/Operator surfaces. Public does not
  make the reader decode how far the team has processed an event.

## Measurement and acceptance

The following must remain separately measurable:

- total canonical records;
- publicly browseable records (expected to equal total canonical records);
- citation-ready records and `non_citation_ready_inventory`, plus the full
  evidence-posture partition; the deprecated `reader_hidden_inventory` and
  `discovery_backlog` aliases never mean an event is hidden from Public;
- current, stale and absent risk assessments by route;
- non-exclusive missing-subject, missing-fact-summary, and missing-citable-
  evidence counts, plus missing-capture counts;
- internal review queues, without using them as Public feed admission counts.

Acceptance requires every canonical event to remain browseable, consistent
facets, deterministic evidence posture and citation readiness, truthful
direct-link fallback copy, workflow/risk/evidence namespace separation, and full
regression coverage.

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
machine-citation-ready. They remain visible with their derived evidence posture
and available for internal review.

## Dual-human review version closure

Dual-human work has two distinct uses. Its primary product-governance use is to
create gold labels for training, evaluation, calibration, drift sampling and
exception analysis. It is not a per-event publication queue and its absence
does not hide an event. When an authorized consensus additionally mutates a
formal fact, the strict version/evidence closure below applies.

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
their audit meaning. A v1 confirmation is never citation-ready and is reported
as `V1_CONFIRM_REQUIRES_FACT_CLAIM_ADDENDUM`; it must be reissued as a v2
independent addendum rather than silently upgraded.

The dual-human evidence status is reader-supportive only together with that
current-version strict relation and a matching immutable selected-evidence
receipt.  The reader recomputes the canonical claim SHA-256, public-summary
SHA-256 and selected-receipt SHA-256 rather than trusting mutually copied hash
fields.  The receipt freezes the URL, exact passage and passage digest, source
ID, source content SHA-256, authority tier, observation state, latest revision
number/kind, and pre-application evidence fingerprint. A later official-source
revision therefore makes the old formal claim citation-ineligible until the
current relation is rebuilt or reviewed again, even when a caller retries the
old consensus. The event itself remains browseable under its downgraded evidence
posture. Human consensus is not a shortcut around the relation gate.

Deletion is globally citation-ineligible. For any later edit, every formal-claim
path fails closed unless the current content still proves the selected passage
and the branch-specific current receipt remains bound to that source revision.
Public may retain a safe tombstone or capture history with explicit uncertainty.
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
