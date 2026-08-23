# Changelog

This project uses date-based versions: `YYYY.MM.DD.N`. Git tags and GitHub
Releases use the same version prefixed by `v`.

## Unreleased

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
