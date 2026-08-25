# Changelog

This project uses date-based versions: `YYYY.MM.DD.N`. Git tags and GitHub
Releases use the same version prefixed by `v`.

## Unreleased

- Restrict the public DeepSeek capture explanation to one explicit boundary:
  the event must have zero evidence relations, no P0/P1 URL awaiting refetch,
  and readable P2/raw capture text. Evidence arrival hides cached AI output and
  prevents new calls. Public requests are cache-only, load after the event core,
  and never render the old deterministic preview as if it were external AI.
- Bind successful interpretation terminal keys and public cache validation to
  the current event version, capture receipt, source revision/content hash,
  contract, prompt and fixed model generation. Run the bounded background timer
  every minute and expose 24-hour queue-wait/provider-latency percentiles.
- Scope event evidence revision expansion to the requested event before ranking
  source history, removing a whole-ledger scan from the event detail path.
- Add the independent human-gold-trained Qwen risk-semantic pipeline. Partial
  A/B drafts remain provisional; training, one-time blind evaluation and the
  fail-closed runtime manifest require completed dual review, arbitration and
  a frozen 420 / 120 / 180 dataset. Qwen scores polarity, materiality and
  adverse strength only; it cannot change evidence state or trigger trading.
  Progress reports now state the real critical path: both reviewers must each
  finish all 720 rows, and one-sided union coverage is not counted as gold.
- Make the 720-row freeze feasible without weakening its source holdout. The
  original globally chronological split put SEC in both validation and blind,
  so the mandatory unseen-source gate could never pass. The v2 freeze contract
  now selects one complete source family from pre-label metadata only, keeps
  the remaining core chronological and records the exact policy and bounds.
  Replace unattainable class-balance quotas with natural-distribution viability
  floors, and require at least 20 adverse-priority blind cases before a model
  can pass the production gate. Preserve the unique TRAIN export while using a
  capped, deterministic TRAIN-only priority resample for SFT; validation and
  blind distributions remain natural and untouched.
- Add a D-drive Windows bootstrap for pinned CUDA PyTorch, bitsandbytes and
  ms-swift, including real GPU and SFT argument-contract probes. It does not
  download the model or start training.
- Stop an apposition from binding another issuer's board action to this issuer.
  The new issuer-bound appointment grammar allowed up to 55 free characters
  between the governing-body noun and its genitive `of`, so in `Board of
  Directors of Parent Corp, the parent of <issuer>` the trailing `of` — which
  governs `the parent`, not the board — bound `<issuer>` with
  `EXPLICIT_ISSUER_CONTEXT`. The pre-existing extractor already rejected these
  correctly; the new path overrode it, publishing a citable claim that this
  issuer's board made an appointment another issuer's board made. Short
  appositions (`the parent of`, `sole shareholder of`, `the acquirer of`, `an
  affiliate of`) were affected; longer ones already exceeded the 55-character
  bound. The gap may no longer contain a comma or a second `of`.

- Close a bypass in the code-only fast-path schema guard. The mutation scan
  only matched a bare `CREATE|ALTER|DROP TABLE|INDEX|TRIGGER|VIEW`, so the
  qualified SQLite forms `CREATE UNIQUE INDEX`, `CREATE VIRTUAL TABLE` and
  `CREATE TEMP`/`TEMPORARY TABLE` passed the contract and could ship as a
  code-only release. `CREATE UNIQUE INDEX IF NOT EXISTS` is already an idiom
  in this repository's schema owners, and `app/storage/ledger.py` is fast-path
  eligible. The installer's before/after live schema receipt did not cover the
  gap: it is taken while the Worker is still stopped, so a mutation on a
  worker-only or lazily executed path was observed by neither control. No file
  in the current tree changes eligibility as a result of the tighter pattern.

## 2026.08.24.4

- Keep production backup bundles and the private restore attestation root-only,
  while publishing a bounded, root-owned, non-secret verification receipt that
  the low-privilege API can validate against the operations ledger. A protected
  but freshly verified backup no longer makes `/health` falsely degraded;
  missing, stale, mismatched, writable or symlinked receipts still fail closed.
- Expand the fail-closed code-only deployment path from public Web assets to
  schema-neutral API, Web, Worker, service and script changes. Deployment and
  recovery machinery, dependency locks, schema owners and any changed Python
  containing schema-mutation SQL still require a full release. The installer
  now proves the live SQLite schema is byte-for-byte unchanged across a fast
  cutover before restarting collection.
- Add an authorization-bound historical primary-source re-admission workflow.
  It replays current P0/P1 passages through the current deterministic fact
  extractor, creates immutable candidate/weak versions with scoped evidence
  relations, and never claims human verification, changes labels or enables
  trading. Exact plan, target ledger, independent backup and event scope are
  checked again inside the write transaction.
- Recognize issuer-bound management appointments made by that issuer's board,
  including exact `Board of Directors of <issuer>` and locally defined
  `the Company` grammar, without accepting a bare or unrelated board.
- Make the internal launcher open the read-only Admin owner overview by
  default. Add a one-click Windows entry that stores only SSH connection
  parameters under `D:\\FinanceRadar`; tokens remain process-local.
- Bind the new recovery, admission, hotfix and owner-entry files into the
  release integrity manifest and regression suite.

## 2026.08.24.3

- Scope Shadow-router source and evidence revision work to the already selected
  200-event window. The previous bounded API still expanded and ranked every
  retained source revision twice, causing the continuous Worker to reach its
  ten-minute deadline during `shadow_routing` on the production ledger.
- Add a 5,000-event regression fixture that fails if a bounded Shadow batch
  returns to a whole-history source-revision scan.

## 2026.08.24.2

- Publish a release as `ACTIVATING` during edge cutover and expose it as
  `ACCEPTED` only after the recovery bundle, activation receipt, API, Web,
  Worker and persistent timers have all passed their final checks. Legacy
  marker readers remain pinned to the previous accepted release in between.
- Split shadow routing into alternating recent and durable round-robin lanes.
  New or changed events stay responsive while every older canonical event is
  eventually revisited instead of being starved behind the newest 200 rows.
- Replace the DeepSeek timer's full historical recovery-plan materialization
  with a bounded 500-capture incremental sweep. The scheduler preserves exact
  capture receipts, resumes from a durable cursor and resets on source,
  relation or evidence-generation changes.

## 2026.08.24.1

- Let public-Web-only releases carry their tests, documentation and bounded
  root release notes through the fail-closed `code-only` path while API,
  persistence, collection, deployment, dependency, model and replay trees
  remain byte-identical to the active release.
- Keep collection and capture interpretation running while a `code-only`
  candidate is validated and prepared. The worker now stops only for the short
  atomic activation window, and the fast path no longer recursively changes
  ownership across existing shared data.
- Add ordered public-browse indexes for latest, event-date and subject order,
  plus an event/status job index. Deep numbered pages now walk the canonical
  sort index instead of sorting and discarding every preceding full row.
- Scope captured-source count and preferred-source selection to the already
  bounded public page and materialize that projection once. Deleted captures
  remain excluded from the count, while filtered aggregate noise remains
  ineligible to supply the card excerpt.
- Bound public feed source title/summary reads before they leave SQLite; the UI
  still receives its existing 360-character excerpt, while authenticated and
  repository-default reads retain the complete source text.

## 2026.08.23.6

- Make captured-source dossier reads event-scoped. The previous query ranked
  every source revision in the ledger before applying the event filter; the
  new projection uses the existing observation/revision index and preserves
  OpenNews revision-time and canonical-link normalization.
- Replace one SQLite interpretation lookup per captured source with one
  event-scoped bulk query. External completed interpretations retain their
  existing preference over deterministic fallbacks, and deleted captures stay
  outside the public dossier.
- Add regression coverage for latest edit/delete semantics, capture receipts,
  missing receipts and external-versus-local interpretation selection.

## 2026.08.23.5

- Replace the public workflow funnel with two independent reader contracts:
  deterministic evidence posture and an optional current-version shadow risk
  assessment. Every canonical event remains browseable; citation readiness is
  never a publication gate.
- Fail closed at the public API boundary. Structured fact summaries and claim
  slots are returned only for citation-ready current versions; other records
  expose a bounded, explicitly unverified source-capture excerpt instead.
- Add the operations-schema index and bounded bulk projection needed to read
  current-version risk routes without an event-by-event query. A locked or
  unavailable operations store removes only the optional risk assessment and
  never hides canonical events.
- Bind human overrides to a server-derived personal Reviewer principal. Shared
  Reviewer and Admin credentials cannot submit them, clients cannot self-report
  actor identity, and all privileged credential/principal collisions fail
  closed at startup.
- Make replay use the same evidence-context derivation as production, reject
  lookalike P0/P1 authority strings, safely quote event IDs in public links, and
  propagate only trustworthy public visitor addresses to the API rate limiter.
- Add a loopback-only Windows launcher for exactly one internal Admin, Reviewer
  or Operator surface, plus an owner-facing read-only Admin summary that does
  not invent release IDs, model coverage, queue depth, backup freshness or
  unavailable metrics.
- Isolate capture interpretation from the shared privileged environment and
  load its provider key through a validated root-owned `0600` systemd
  credential. The five-minute timer remains unlimited by daily budget policy,
  while batch, concurrency, timeout, retry and output bounds remain enforced.
- Quiesce capture interpretation across backup, schema migration and symlink
  cutover. An in-flight provider batch is allowed up to eight minutes to finish
  naturally, is never killed mid-request, and its timer is restored only after
  the activation record has committed and verified.
- Harden recovery decryption so unauthenticated GCM plaintext never reaches a
  named destination, and add clickjacking denial headers to both the main
  public route and release marker.

## 2026.08.23.4

- Decouple event visibility from formal citation readiness. Every canonical
  event is now publicly browsable, while the strict subject/fact/P0-P1/current-
  version evidence contract remains visible as a separate `reader_ready`
  quality label rather than an all-or-nothing display gate.
- Page canonical events before evaluating evidence integrity for public browse
  requests. On a 15,080-event synthetic ledger, the first-page repository query
  fell from roughly 315-340 ms to 19-25 ms without changing authenticated
  `reader_ready` or public-state filter semantics.
- Add a single public event-dossier projection so event detail, formal evidence,
  captured sources, source interpretation and knowledge context load in one API
  request instead of five sequential loopback requests.
- Add a fail-closed `code-only` deployment mode for future public-Web-only
  releases. It reuses a root-attested verified daily recovery bundle, refuses
  API/persistence/worker/dependency/service/model changes, and keeps full deployment
  as the default. This release itself must use the full path to bootstrap that
  attestation.

## 2026.08.23.1

- Move the expensive `/api/v1/overview` aggregation into a bounded external
  systemd oneshot. It publishes a hash-verified JSON generation atomically;
  the API only reloads that data file and therefore remains responsive while
  collection writes or a multi-minute overview refresh are in progress.
- Refresh the overview data every five minutes, preserve the last valid
  generation after a failed publication, and require a successfully published
  snapshot in both in-place deployment and disaster-restore activation gates.

## 2026.08.22.13

- Make `/api/v1/overview` a complete in-memory projection: backup state, demo
  mode and worker-cycle metadata now refresh with the ledger snapshot instead
  of reopening the operations database on every public request.
- Configure SQLite WAL when an operations repository is initialized rather
  than on every connection. This removes compounded lock waits while the live
  collection worker is writing without weakening snapshot freshness or the
  reader-only product boundary.

## 2026.08.22.12

- Align deployment and prepared-restore API health gates with the measured
  cold-start cost of the precomputed overview snapshot. Both paths now wait up
  to 90 seconds, preventing a healthy roughly 45-second startup from being
  rolled back while keeping the snapshot ready before public reads begin.

## 2026.08.22.11

- Overlap up to three independently claimed capture-interpretation requests so
  the historical receipt backlog can be drained promptly. Provider usage
  reservations, immutable receipt idempotency, retry state and the prohibition
  on canonical mutation remain enforced per job.
- Keep five-minute source collection ahead of slower local evidence analysis:
  one evidence decision is drained per cycle, and a hard timeout after durable
  source collection is reported as degraded rather than as total interruption.

## 2026.08.22.10

- Made live asset-relation reconciliation tolerate canonical quality deletion:
  obsolete relation definitions are skipped and reported instead of failing the
  entire collection cycle. Removed the currently stale ECB relation entry that
  referenced an event already deleted from the production ledger.

## 2026.08.22.9

- Made capture interpretation a durable historical-to-incremental data layer:
  the worker exhausts every eligible retained receipt once, persists its model
  generation and source watermark, and performs no provider call until a new
  or revised receipt appears. Historical terminal keys are loaded in bulk and
  the bounded service batch was raised to 20.
- Replaced the production shadow router's public-reader/N+1 query path with two
  bounded bulk queries. Live cycles now publish stage checkpoints and use an
  exact child-owned lease token, so a hard timeout preserves the failing stage
  and cannot strand the next five-minute cycle behind an orphan lease.
- Public status now separates completed processing cycles, public-readable
  events and reader-hidden recovery inventory instead of presenting an empty
  reader queue as a completed historical review.

## 2026.08.22.8

- Moved the expensive public overview projection out of the request path. The
  API now computes it before serving traffic, refreshes it every 30 seconds in
  the background, and preserves the last good generation after a refresh
  failure. The public UI first-read timeout is now 20 seconds, preventing a
  healthy cold process from being presented as a full-page outage.

## 2026.08.22.3

- Fixed the five-minute capture-interpretation queue so recovery-plan receipts
  match canonical captured-source receipts. Immutable cache hits no longer
  consume the per-run completion limit, preventing new candidates from being
  starved while preserving cache reuse and zero canonical mutation.

## 2026.08.22.2

- Fixed the production capture-interpretation systemd entry point and hardened
  DeepSeek JSON adaptation with information-reducing normalization for extra
  fields, malformed asset lists and ungrounded numeric prose. The strict quote,
  schema and canonical no-mutation gates remain authoritative; transient model
  contract failures are bounded and retryable.

## 2026.08.22.1

- Closed the captured-source interpretation path with grounded Chinese summaries,
  immutable source references, DeepSeek JSON-contract validation, retry leases,
  failure accounting and an optional bounded systemd worker. Daily request and
  CNY ceilings are intentionally unlimited by owner decision while usage,
  per-batch, timeout, retry and output-token controls remain enforced.
- Added source-observation recovery for API payloads without a usable original
  URL, preserving raw content as reviewable P2 captures without promoting it to
  citable evidence or changing canonical event status.
- Added minute-bar market observations with provider timestamps, a 24-card
  financial knowledge layer with FTS5 retrieval and traceable calculators, and
  a leak-resistant dual-human-gold preparation/training bridge that remains
  shadow-only until authentic labels return.
- Hardened the public overview and event dossier with bounded aggregation caches,
  supported-source selection, financial context, explicit degraded states and
  historical Schema 14 compatibility. Full local regression passed with
  `981 passed, 5 skipped`.

- Added a read-only public-reader quality gate: a canonical record enters the
  public event feed only when it has a named subject, a structured fact summary,
  and a citable URL plus exact source passage. Incomplete candidates remain in
  the canonical ledger and internal review path as a separately counted
  discovery backlog; no canonical status or evidence row is rewritten.
- Replaced the generic subject-plus-category fallback (for example, a ticker
  followed only by “listing status”) with an explicit discovery-only explanation
  that states the missing subject/action/stage or source evidence instead of
  implying that a specific event occurred.

## 2026.08.19.1

- Closed the authentic-human review boundary: Reviewer and Arbiter identities
  are credential-bound server-side principals, the blind-set freeze is
  action-authorized and receipt-bound, and exact/near duplicates plus normalized
  source-family coverage fail closed. Authentic-human label count remains zero;
  the risk router remains advisory `QUALIFIED_SHADOW` and no-trading.
- Added fact-integrity history audit v2 with separate decision and evidence-edge
  units, current-contract light-review reason codes, exact read-only manifests,
  and an explicit count of legacy canonical-verified rows. The audit neither
  rewrites history nor authorizes a canonical status change.
- Removed stale mutable counts from student-facing material, linked volatile
  claims to `CURRENT_STATE.md` and reproducible commands, recovered the second
  repository audit into the main history, and documented bounded choices for
  each historical-integrity debt class.
- Recorded the verified `2026.08.18.3` production activation and the new private
  D-drive recovery point after a complete authenticated-decryption, manifest,
  dual-SQLite and model hash-chain restore audit. The temporary `/32` SSH rule
  was removed after transfer; the legacy public recovery ciphertext remains
  untouched pending a separate destructive-action decision.

## 2026.08.18.3

- Moved the public shell ahead of aggregate API work, added bounded role-scoped
  GET snapshots with explicit stale ages, and moved non-critical 30-day product
  metrics after the event feed. Desktop, keyboard and 390 px browser checks now
  cover the first-use path without allowing cached data to masquerade as fresh.
- Bound every authentic-human review identity and role to a separate server-side
  credential. Client-supplied reviewer aliases and roles are rejected, a shared
  admin/reviewer token cannot impersonate a human, and reviews persist only a
  stable principal hash. The Reviewer UI now requires that personal credential
  in the current Streamlit session and never substitutes its static UI token.
- Added the `human-blind-v3.1` event-time sample contract, primary-evidence
  ordering, issuer/event-chain grouping gates, exact/near-duplicate exclusion,
  balanced deterministic selection and one-way hash-bound freeze tooling.
  Freezing fails closed unless every historical training, development and blind
  manifest is present, and excludes their event, issuer and event-chain groups
  as well as exact and near-duplicate text. The existing 24 legacy OPEN samples
  remain visible as ineligible history; no authentic-human blind set is claimed.
- Added same-page source-first reading continuity: event cards now have stable
  return anchors, filters survive preview/return, the highest-authority source
  is the explicit external jump, and a browser-session snapshot explains status,
  version or evidence changes since the last view.
- Let the Windows local launcher place logs on an explicit D-drive path so UI
  and recovery QA do not consume the constrained C drive.
- Made off-host migration creation reuse and independently reverify a fresh
  full recovery bundle, use the root-backed `/var/tmp` for large SQLite checks,
  and hard-link immutable payloads when possible so the one-copy server does
  not needlessly duplicate several GiB during migration.
- Added explicit local-interface binding to the Windows SSH/SCP recovery path,
  kept all large audit workspaces on D:, and calibrated bounded restore limits
  to the current evidence corpus while preserving path, member-count, manifest,
  database-integrity and per-file hash gates.
- Added the exact qualified SHADOW `risk_router.joblib` to traceable source and
  made the binary, SHA declaration, model card and blind-v3 report mandatory
  release-contract files. A source-only archive can no longer silently deploy
  the keyword fallback while advertising the qualified model.

## 2026.08.18.2

- Added a database-free `/api/v1/live` probe and changed the transactional
  installer to use it for process activation. The full health endpoint remains
  available for database-backed operational assessment without turning a
  five-second deployment probe into a self-amplifying query backlog.
- Hardened the Windows off-host recovery task: it is hidden, non-interactive,
  uses S4U instead of an interactive desktop session, keeps ciphertext on D:,
  keeps the recovery passphrase outside the ciphertext tree, and retains one
  fully restored daily copy only after its successor passes verification.
- Removed public publication of detailed off-host receipts and made
  `/radar/offhost-status.json` an explicit `404`; hashes, backup age, release
  identifiers and ledger counts now remain operator-only recovery metadata.
- Preserved the failed 2026-08-18 candidate's full pre-cutover recovery hold
  after its database-heavy activation probe timed out, while automatically
  restoring the previous release and all required services.

## 2026.08.18.1

- Added a canonical owner-intent and system doctrine that reconstructs the
  product's evolution from the original personal multi-asset evidence radar,
  defines short-research specialization as adverse-risk human-review routing
  rather than a SHORT or trading signal, and separates stable rules from live
  infrastructure facts and superseded decisions.
- Added a machine-readable owner-intent policy and tests that preserve the
  all-polarity evidence layer, advisory-only model boundary, review authority,
  role isolation, one-copy verified daily backup, D-drive artifact policy and
  action-specific authorization gates.
- Corrected the obsolete private-Release assumption after live verification
  showed that the repository is public: future production recovery archives
  belong in a separately controlled private store, while the legacy encrypted
  asset is left untouched pending a separate retention or removal decision.
- Bound Evidence Agent support edges to both the target issuer and a meaningful
  claim relationship; unrelated primary-source text now remains unmatched
  instead of creating an `EVIDENCE_READY` false positive.
- Bound formal light-verification event predicates to the target issuer in the
  same local clause and added customer, vendor and subsidiary counterexamples.
- Retired the continuous worker's legacy config-to-canonical write path. The
  105 historical rows are now reported as unproven review hints rather than
  authentic-human labels or formal write authority.
- Added a read-only history audit that identifies stale agent decisions and
  light-verification records requiring review without rewriting history.

## 2026.08.15.4

- Published the production release at commit
  `ceb9f577b5486f6eac6a6fba5699f9e8131509df` with release identity
  `20260815T051127Z-ceb9f577b548`.
- Verified the public read-only product, isolated runtime roles, continuous
  worker, cgroup memory protection and one-copy daily backup policy on AWS.
- Bound the accepted recovery artifact to its SHA-256, verified full restore
  receipt and exact production activation record.

- 修复实时循环租约心跳通过完整 schema 初始化连接而与主循环写事务竞争的问题；心跳现在使用有界等待的租约专用 SQLite 连接，并在短暂锁冲突后继续续租。
- 将切换前完整备份的数千行运行结果写入候选版本的受限发布记录，而不是直接灌入远程终端；安装器只回传简洁门禁状态，降低长部署因输出通道中断而留下半激活状态的风险。

- 修复隔离的公开 Web 账户无法读取顶层 `VERSION`、导致候选版本在切换前安全终止的问题；安装与恢复路径现在只额外公开这一项运行时版本标记，私密环境和共享数据权限不变。

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
- Made the isolated public Web identity perform a real cwd-based `import app`
  before and after cutover, while private environment/data paths stay unreadable.
- Replaced the three-copy predeploy backup peak with a verified atomic custody
  transfer: the fresh bundle leaves normal retention, superseded daily bundles
  are removed only after revalidation, and failure moves the fresh bundle back.
- Kept `/opt/finance-radar/releases` traversable during a failed pre-cutover
  transaction so rollback cannot strand the public Web unit at `CHDIR`.

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
