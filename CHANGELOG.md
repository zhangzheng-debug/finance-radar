# Changelog

This project uses date-based versions: `YYYY.MM.DD.N`. Git tags and GitHub
Releases use the same version prefixed by `v`.

## Unreleased

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
