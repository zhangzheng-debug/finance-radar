# Finance Radar backend stage closeout

Status: local release candidate; production deployment and live verification pending

Target version: `2026.08.23.6`

Branch: `codex/dossier-performance`

## Outcome

This stage turns the existing backend from a technically rich but human-gated
research workflow into an automation-first, publicly browseable evidence
system. It does not weaken the distinction between a captured lead and a
citable fact, and it does not add trading capability.

The core product model is now:

1. every canonical event is browseable;
2. evidence posture is derived automatically from current data;
3. a current shadow risk route is an independent review-priority signal;
4. internal workflow progress stays in loopback Reviewer/Operator surfaces;
5. a structured public fact crosses the API boundary only when its current
   version is citation-ready.

## Completed work

### Public data contract

- Added `PRIMARY_SUPPORTED`, `PRIMARY_SOURCE_AVAILABLE`, `SOURCE_CAPTURED`, and
  `NO_SOURCE` evidence postures plus explicit gap codes.
- Preserved the complete canonical feed instead of hiding non-citation-ready
  records.
- Added a bounded current-version risk projection with three routes:
  `RISK_REVIEW`, `NON_TARGET`, and `ABSTAIN`.
- Removed the per-event risk lookup pattern by adding a version-aware operations
  index and bulk query.
- Made the risk store optional for public browsing: failure removes the optional
  route object, not the event.
- Prevented dormant historical fact slots from leaking through feed, detail, or
  dossier APIs when the current citation relation is absent.

### Privileged review boundary

- Human overrides now require a personal Reviewer principal derived on the
  server; actor identity cannot be supplied by the browser.
- Admin/shared credentials cannot impersonate an individual reviewer.
- Duplicate or cross-role credentials and principal identifiers fail closed at
  configuration load.
- Override audit rows retain a non-secret principal hash, role and credential-
  bound attestation.

### Consistency and request security

- Replay and production now derive the same evidence context.
- P0/P1 recognition rejects prefix lookalikes.
- Public event identifiers are URL-quoted before navigation.
- The public Streamlit proxy forwards a client address only when the request
  metadata proves the trusted Nginx path; internal surfaces never forward it.
- Public framing is denied with CSP `frame-ancestors 'none'` and
  `X-Frame-Options: DENY`, including `release.json`.

### Internal usability

- Added a Windows launcher that requires an explicit host, optionally reads an
  SSH identity from `D:`, proves all three internal UIs inactive, starts exactly
  one loopback service, opens one local tunnel and stops only the service it
  owns on exit.
- Added an Admin “老板总览” based only on existing read APIs. Missing fields
  remain unavailable rather than becoming zero or guessed values.
- Current backup freshness is authoritative; an old verified record cannot
  overwrite a stale current backup status.
- Model cards show real run and frozen external-blind evidence, not a fabricated
  “coverage” percentage.

### Event dossier performance

- Replaced the global source-revision window scan in `captured_sources` with an
  event-scoped latest-revision lookup over the existing composite index.
- Replaced one operations-database connection and query per capture with one
  event-scoped bulk interpretation lookup while preserving external-first,
  newest-update selection.
- Production read-only profiling isolated the previous cold-path cost: event
  detail about 0.17 seconds, evidence about 0.005 seconds, interpretation about
  0.02 seconds, but captured sources about 17.7 seconds for one capture. The
  equivalent event-scoped SQL measured about 0.0012 seconds on its first run
  and below the timer resolution on four immediate repeats. End-to-end cold,
  warm and concurrent dossier acceptance remains a live deployment gate.

### Secret, recovery and deployment safety

- The DeepSeek worker no longer receives the shared privileged environment. It
  uses systemd `LoadCredential` from a root-owned, non-symlink `0600` file with
  bounded size and a restricted service sandbox.
- Daily model spend and request caps remain unlimited by explicit owner policy;
  request batch, concurrency, timeout, retry and output-token controls remain.
- GCM decryption authenticates into an anonymous temporary file before a named
  `0600` staging path can replace the destination. Authentication failure leaves
  an existing destination untouched.
- Deployment stops new interpretation launches before backup/migration/cutover,
  waits up to the real bounded batch window for an in-flight oneshot to finish,
  and restores the timer only after the activation record is committed and
  verified.

## Verification evidence

- Final full repository run after the dossier query fix:
  `1060 passed, 6 skipped`.
- Dossier, source revision, capture interpretation and public-semantics targeted
  run: `33 passed`.
- Security, systemd, backup, configuration, principal and launcher targeted run:
  `95 passed`.
- `python -m compileall -q app scripts tests`: PASS.
- `python scripts/verify_dependency_locks.py`: PASS for four lock files.
- `bash -n deployment/systemd/install_remote.sh`: PASS.
- `git diff --check`: PASS.

A clean-commit release audit, CI, tag/archive identity check and production
acceptance remain mandatory before this candidate may be called deployed.

## Deliberately retained compatibility and boundaries

- Public API 1.x still contains `status`, `public_state`, and `reviewed_at` for
  compatibility. Public UI no longer presents them as trust labels; removal is
  deferred to a versioned API-major change.
- Historical workflow and evidence debt remains auditable data. This release
  changes how it is presented; it does not silently rewrite past conclusions.
- Admin, Reviewer and Operator remain manual, mutually exclusive and loopback-
  only. No public Nginx route is added.
- No order, position, balance, broker or trade-execution capability is present.
- The old public recovery asset and any related key rotation are destructive,
  separately authorized operations. They are not part of this release.

## Production acceptance checklist

The release is complete only when all of the following are observed on the live
host, not inferred from CI:

- immutable tag, manifest, archive and deployed commit agree;
- full deployment transaction and post-cutover recovery verification pass;
- API, Public Web, Worker, overview timer and backup timer are active;
- capture interpretation timer returns to its exact prior enabled/active state;
- provider credential remains `root:root 0600` and absent from shared env/logs;
- public page, health and release marker return 200;
- API and all three internal UIs remain absent from public routing;
- framing-denial headers are present;
- all canonical events remain browseable and non-citation-ready structured facts
  remain redacted at the raw public API boundary;
- backup freshness, disk/memory and latest worker/source state are recorded from
  the live host.
