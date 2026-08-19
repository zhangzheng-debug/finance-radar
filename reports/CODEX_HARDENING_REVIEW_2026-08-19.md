# Codex 加固分支独立复核

- 复核日期：2026-08-19
- 被复核对象：PR #17 / `codex/claude-reaudit-hardening` @ `735a20f`
- 基线：`main@d31b00f`（`2026.08.18.3`）
- 变更规模：25 文件 / +1,609 −70
- 上一轮：[`repository_audit_20260818.md`](repository_audit_20260818.md)

本文件不修改 Codex 的对账记录
（[`CLAUDE_REAUDIT_RECONCILIATION_2026-08-19.md`](CLAUDE_REAUDIT_RECONCILIATION_2026-08-19.md)）。
所有结论均独立执行验证，不采信自述。

## 结论

**建议合并**，但合并顺序有两个前置条件，见第四节。

本轮的价值不在补丁数量，而在于它证明了上一轮复核的一个盲区：
「未发现新缺陷」只对被扫描的攻击面成立，不能外推到当时尚未成型的人审／冻结链。
Codex 在该链上找出三条 P1，其中两条直接推翻了本审计线此前的产出。

## 一、独立验证结果

| 项 | 自述 | 独立测量 | 结论 |
|---|---|---|---|
| 测试 | `723 passed, 5 skipped` | `728 passed, 21 subtests`（Python 3.11，无 Telethon） | 同一批数，吻合 |
| 危险原语 | 未声明 | `eval`/`exec`/`pickle.loads`/`shell=True`/`yaml.load`/`verify=False`/`os.system` 全零 | 干净 |
| 明文凭证 | 未声明 | 新增行全扫无命中 | 干净 |
| SQL 拼接 | 未声明 | 无 f-string 拼接 | 干净 |
| operations schema 7 | 「schema 7」 | `CREATE TABLE IF NOT EXISTS` + `PRAGMA table_info` 条件式 `ALTER` | 加性迁移，对既有库安全 |

## 二、三条 P1 逐条核实

### FR-RA-001 · 真人凭据没有生产供给路径 —— 成立，且影响被低估

`app/config.py` 此前只有运行时解析器，systemd、Compose 和恢复路径都没有供给
`reviewer-principals.json`。后果不是「配置不便」，而是**生产 API 的
`require_bound_reviewer_principal` 会稳定返回 503**——真人双审在生产上物理上无法开始。

此前所有「基础设施已就绪、只差人去标」的表述，在这条修复之前都不成立。

核实到的实现：

- `deployment/systemd/finance-radar-api.service:13` 使用
  `LoadCredential=reviewer-principals.json:/etc/finance-radar-reviewer-principals.json`；
- `app/config.py:29-30` 环境 JSON 与 systemd 凭据**互斥**，同时出现直接抛错；
- `app/config.py:32-33` 凭据文件 64 KiB 上限；
- `scripts/generate_reviewer_principals.py` 强制 ≥2 名 Reviewer 与恰好 1 名 Arbiter、
  ID 大小写归一后唯一、`os.O_EXCL` 独占创建、`0o600`、`secrets_printed: false`
  且控制台不打印 token。

### FR-RA-002 · CLI 自报即授权 —— 成立，且推翻了本审计线的前一次产出

上一轮本审计线为该脚本补了契约测试，但**把当时的合同当作正确的钉住了**：
彼时 `--actor owner` 这类 CLI 字符串即可 `--apply`。Codex 的判断正确——
自报身份不构成授权，测试覆盖率不等于合同正确。

核实到的新合同：

- `--apply` 必须提供 `--authorization-file`，缺失即拒绝（`freeze_human_blind_v3.py:305-306`）；
- 授权文件必须精确绑定候选的 freeze_id、数据集哈希、样本范围与来源范围，
  任一不符即 `authorization contract does not bind the candidate {field}`；
- 必须显式 `approved`，且 `authorization_id`、外部 actor 身份、purpose 长度均有门；
- dry-run 阶段生成授权模板，供所有者在仓库之外填写与批准；
- `PREPARED → COMMITTED`：`app/storage/operations.py` 新增 `adjudication_freezes` 表，
  保存 dataset/sample_ids/authorization 三组哈希与 receipt，
  与样本 `FROZEN` 状态**同一事务**提交；
- 完全相同的重试幂等返回 `idempotent: true`，任一要素不符则
  `freeze retry conflicts with the committed receipt`。

同时修掉了上一轮标记但刻意未钉死的写入顺序问题。当前注释为：
*No artifact is written until every apply-time authorization and holdout gate has passed.*

### FR-RA-003 · 近重复只是精确重复 —— 成立，且含一条中文失效缺陷

旧 `_near_duplicate_key` 是规范化文本的 SHA-256，任何一处词级编辑即可绕过；
它是精确重复检测，冠了近重复的名字。

更严重的是中文：旧实现用 `normalized.split()` 分词，而规范化只把非
`[a-z0-9一-鿿]` 折成空格，因此**连续中文会退化为单个巨型 token**，
近重复检测对中文语料完全失效。

核实到的新实现：`_duplicate_tokens` 改为 `[a-z0-9]+|[一-鿿]`
（拉丁词保持完整、CJK 按字切分）；`_is_near_duplicate` 采用
token 重合系数 ≥ 0.90（要求较小集合 ≥12 token）与 5-gram Jaccard ≥ 0.85 双判据；
对历史语料与盲集内部双向检查；附中英文反例测试。

### 另外两条

- **FR-RA-004**：「来源留出」此前只是文档措辞，现为 apply 门——至少需要一个
  在全部历史语料中完全未出现的来源家族，无此家族时诚实 `BLOCKED`。
- **FR-RA-005**：上一轮本审计线的测试无条件断言 `S_IMODE == 0o600`。
  NTFS 上 `chmod` 不产生 owner-only ACL，该断言构成**虚假安全证明**。
  现 POSIX 断言 0600，Windows 在清单写入 `WINDOWS_CALLER_ACL_NOT_PROVEN_BY_CHMOD`。

## 三、治理登记的处理优于建议

上一轮建议为 `FR-BAK-006` 的已知违规「登记一条带期限的例外」。
`docs/OPEN_GOVERNANCE_VIOLATIONS.md` 的处理更严格：它明确拒绝把违规称为例外
（*must not be normalized as policy or silently called an exception*），
记录资产名、字节数与 GitHub 摘要，写明「公开仓库无法把单个 Release 资产转私有」
这一不可协商的理由，给出五步关闭顺序，并声明本次变更**不执行第 2–5 步、
不隐含删除授权**。

## 四、合并前置条件

### 1. 不要合并 `claude/internship-position-report-0aozcd@65f79d2`

该提交与本分支修改同一文件。本分支的
`tests/test_freeze_human_blind_v3.py` 是其**严格超集**——21 个测试名逐字保留，
另加精确重试对账、清单写失败后的持久收据、模糊近重复与精确哈希的区分、
以及小改动可捕获／无关文本不误捕四项。同时合并会产生冲突，并把已被推翻的
弱授权合同重新钉回。

### 2. 新 `.gitignore` 规则对已跟踪文件无效

本分支新增 `docs/team_*_*.html` 与 `docs/Finance_Radar_*_*.pdf` 忽略规则，
方向正确（保留 Markdown 源、渲染产物走 Release）。但 `.gitignore` 不影响
已跟踪文件，而 `claude/internship-position-report-0aozcd` 上已提交五个匹配文件：

```
docs/team_briefing_2026-08-16.html
docs/team_handbook_2026-08-16.html
docs/team_role_readiness_2026-08-16.html
docs/Finance_Radar_组内工作说明_2026-08-16.pdf
docs/Finance_Radar_组内说明书_2026-08-16.pdf
```

该分支若合并，这五个文件会绕过新规则继续留在库中。需在合并时
`git rm --cached`，或不合并这些渲染产物。

## 五、对上一轮审计的更正

`repository_audit_20260818.md` 第四节列出「`/radar/offhost-status.json`
没有防回归契约」。**该条不成立，予以撤回。** 实际有三处契约：

- `tests/test_nginx_streamlit_route_contract.py` 断言两份 Nginx 配置中该
  location 块内含 `return 404;` 且不含 `alias`；
- `tests/test_pull_server_migration_backup_contract.py`；
- `tests/test_migration_local_storage_contract.py`。

原判断基于目录级检索而未读断言内容，属复核方法疏漏。

## 六、本分支未覆盖、仍然开放的事项

| 事项 | 状态 |
|---|---|
| 历史债：6,499 条证据边／4,275 条决策／2,729 条轻核验／105 条旧配置 | 本分支未触及，对账文档亦未提及；四类各需一个明确处置决定 |
| `GOV-2026-08-19-01` 第 2–5 步 | 需所有者单独授权；本分支正确地未自行授权 |
| 第二轮审计分支 `claude/repository-audit-q77ut2` 未合并 | 不变 |
| `codex/sec-shadow-evidence-clarity`、`codex/release-archive-lock-portability` 残留引用 | 不变 |
| 课程侧：`app/core/` 三个禁飞区、证据清单、三份学生材料 | **完全为零**，六个提交与本分支均未触及 |

## 七、建议的下一步

FR-RA-001 修复之后，真人双盲标注第一次真正可执行。建议路径：

**合并 → 部署 → `generate_reviewer_principals.py` 生成 2 名 Reviewer + 1 名 Arbiter
凭据 → 完成 80 条标注（RISK_REVIEW 30 / NON_TARGET 30 / ABSTAIN 20）**

该动作同时解三个锁：使 v4 具备真正脱离 `QUALIFIED_SHADOW` 的前提；
产出无法由单人代劳的协作证据；为 4,275 条待重跑决策提供人工基线。

合并本身不触发模型晋级、真人冻结、AWS 部署或公共资产删除——这一点与本分支
自述一致，核实无误。
