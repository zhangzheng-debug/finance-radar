# Finance Radar 第三轮仓库审计

- 审计日期：2026-08-18
- 审计区间：`663fdf5` → `d31b00f`（65 文件 / +4,182 −619 行 / 6 次提交 / 3 个新 tag）
- 发布版本：`2026.08.18.3`
- 上两轮：[`repository_audit_20260814.md`](repository_audit_20260814.md)、
  [`repository_reaudit_20260815.md`](repository_reaudit_20260815.md)（后者位于
  `claude/repository-audit-q77ut2` 分支，尚未合并）

本轮区间此前无人审计。所有结论均经独立执行验证，未直接采信提交信息或
`CURRENT_STATE.md` 的自述。

## 摘要

| 项 | 结果 |
|---|---|
| 新引入安全缺陷 | 0 |
| 真实缺陷修复 | 2（证据边错挂、发布归档漏模型文件） |
| 新增治理机制 | 1（所有者意图细则 + 29 条机器可执行硬门） |
| 测试 | 669 → **698 全过 + 21 subtests**（本机实跑，0 失败） |
| `app/` 覆盖率 | 82% |
| 新发现 | 5 条（1 中、1 中、3 低） |
| 课程侧进展 | **0**（六个提交完全未触及） |

---

## 一、本轮做了什么（逐条独立核实）

### 1. 所有者意图细则：从治理层关闭"事实源互相冲突"

新增 `docs/OWNER_INTENT_AND_SYSTEM_DOCTRINE.md`（357 行）+
`config/owner_intent_policy_v1.json`（29 条硬门）+ `tests/test_owner_intent_policy.py`。

机制上有三点做得好：

- **权威顺序表**：所有者指令 > 细则 > 产品章程 > 专项合同 > 当前状态 > 发布报告 >
  历史项目书，七级明确。冲突时不允许"默默选择方便的一份"。
- **事实分类**：`P` 永久原则 / `C` 易变事实 / `H` 历史背景 / `SUPERSEDED` 已取代决定。
  `FR-GOV-003` 明确区分四种"当前"——GitHub main、最新 tag、生产已安装提交、生产健康。
- **已取代决定登记表**：7 条旧决定（LONG/SHORT 字段、Telegram 主界面、零云成本、
  新加坡拓扑、粗审即正式、五页全公网、多份在线备份）被显式标记，防止无意复活。

**独立核实**：`test_every_mandatory_hard_gate_is_explained_in_doctrine` 强制正文与
机器文件同提交同步；29 条 `FR-*` 规则 ID 在两处均存在。这不是一份会烂掉的文档。

### 2. 证据相关性门：一个真实的正确性缺陷被修掉了

旧 `EvidenceAgent._propose_edges()` 用
`max(claims, key=lambda c: len(claim_tokens[c] & excerpt_tokens))`
挑 claim —— **重合度为 0 时同样返回一个 claim，并挂上一条 `SUPPORTS` 边**。
也就是说：任何一段证据都会被强行绑到某条声明上，哪怕两者毫无关系。

新增 `_claim_evidence_relevance()` 要求同时满足：

- **身份锚**：ticker 精确词边界匹配 ∪ 发行人词命中 ≥ min(2, 词数) ∪ 泛自指
  （且 Jaccard ≥ 0.35 或有意义重合 ≥ 3）
- **语义锚**：扣除发行人词与泛关系词后的有意义重合 ≥ 2

代码注释直接写明设计取向：*"A lexical winner is not evidence … It deliberately
prefers an unattached passage over a false SUPPORTS edge."*

**影响量级**：生产历史审计用新门重算，**6,499 条旧证据边被拒绝**。这不是小数目——
说明历史证据图里长期存在大量无关联的支撑边。

### 3. 历史事实完整性审计：只读、不改 canonical

新增 `scripts/audit_fact_integrity_history.py`（425 行）。设计上刻意只读：
*"It may classify an old decision as stale or in need of review, but it never
changes canonical event state, evidence, or operations history."*

生产读数（报告 SHA-256 `ac75b5715b99…`）：

| 类别 | 数量 | 含义 |
|---|---:|---|
| 旧 Evidence Agent 边被相关性门拒绝 | 6,499 | 需重建 |
| 旧决策需按新合同重跑 | 4,275 | 收据不匹配 |
| 轻量核验需人工复核 | 2,729 | 门禁语义变更 |
| 旧"人工核验配置" provenance 不可证 | 105 | 无法证明来源 |

### 4. 真人盲审身份绑定：堵住"一个人换别名"

`app/config.py` 新增 `_reviewer_principals_from_env()`，`app/api/main.py` 新增
`require_bound_reviewer_principal()`：

- 主体身份与角色**由服务端从凭据推导**（`principal_hash` = SHA-256(命名空间+id)），
  客户端不能自报 `reviewer_id` 或 `role`；
- Admin 与旧共享 Reviewer 令牌**明确不能**提交真人标签；
- `secrets.compare_digest` 常量时间比较；令牌 ≥ 24 字符、ID 与令牌指纹唯一；
- 未配置时返回 503 而非放行——fail closed。

合同升级为 `human-blind-v3.1`，旧 24 个样本被标为 contract-ineligible 并保留为
审计历史，新盲集保持 `NOT_READY`。**当前真人结论仍是 0 条**，系统没有拿 AI 标签
或旧样本凑数。

### 5. 发布合同修复：由一次真实恢复演练发现

异机加密恢复演练发现旧发布归档**漏掉了 `artifacts/risk_router.joblib`**——该文件
被 `.gitignore` 的 `artifacts/*.joblib` 规则吞掉。后果是恢复出的系统会静默退化为
关键词 fallback 而非正式 SHADOW 模型。

处置：`.gitignore` 加 `!artifacts/risk_router.joblib` 例外，模型进入
`scripts/release_audit.py` 的关键文件清单，模型卡/SHA 声明/blind-v3 报告四重哈希
一致后重新部署 `.3`。

**这条值得单独表扬**：门禁没有被放松，失败包没有被标成成功，而是修合同后重做。

### 6. 首页性能与角色隔离缓存

Public 首屏先渲染外壳，概览/筛选走有界短缓存，30 天产品指标移到事件列表之后。
**缓存键包含 `UI_ROLE`**（`key = (UI_ROLE, path, id(api_request))`），不存在跨角色
缓存串读。刷新失败时只允许显示带年龄标注的旧快照。

---

## 二、新缺陷排查（结果：0）

对本轮 +4,182 行重跑与上一轮同类的检查：

| 检查项 | 结果 |
|---|---|
| 危险原语（`eval`/`exec`/`pickle.loads`/`shell=True`/`yaml.load`/`verify=False`/`os.system`） | 干净 |
| 明文凭证（新增行全扫） | 干净 |
| SQL f-string / 拼接 | 干净 |
| 写端点鉴权 | 7/7 有鉴权，0 缺口 |
| 新增 `unsafe_allow_html` | 1 处，参数常量 |
| 缓存跨角色泄漏 | 无（键含角色） |

> 说明：`POST /api/v1/adjudication/samples/{sample_id}/reviews` 在装饰器上没有
> `dependencies=`，初筛报警。实为把 `Depends(require_bound_reviewer_principal)`
> 放在函数签名里以便取用主体身份——**比装饰器写法更强**，因为 `reviewer_id`
> 取自凭据而非客户端负载。非缺陷。

---

## 三、本轮新发现

### F1 · 中 · `scripts/freeze_human_blind_v3.py` 零测试覆盖

```
CoverageWarning: Module scripts.freeze_human_blind_v3 was never imported.
grep -rn "freeze_human_blind_v3" tests/ .github/   →  无匹配
```

193 行，从未被任何测试导入。它的特殊性在于：

- 执行**一次性、不可逆**的哈希冻结（冻结后不可重来）；
- 有 `--apply` 写路径，需 action-scoped 授权 + actor + purpose + expiry；
- 是模型从 `QUALIFIED_SHADOW` 出影子态的**唯一关键路径**；
- 依赖 6 个数据集交叉排除文件，缺一即 `ValueError`。

仓库里同等后果的路径——备份轮换、release audit、迁移恢复、light verification——
**全部有契约测试**，唯独它没有。建议至少补：授权过期拒绝、排除文件缺失拒绝、
最小配额未达拒绝、重复冻结拒绝、以及冻结产物的哈希稳定性。

### F2 · 中 · FR-BAK-006 的自违规，"不能删"的理由已经失效

细则硬门 `FR-BAK-006`：*Production recovery assets stay in private storage and
never in this public repository or its Releases.*

现状（`CURRENT_STATE.md` 自述并经确认）：

- 仓库 `zhangzheng-debug/finance-radar` 为 **PUBLIC**；
- 历史 `v2026.07.22.2` Release 仍含一份加密生产恢复资产；
- 文档已诚实纠正："加密不等于私有可见性"。

原先不删的理由是"可能仍是唯一异机副本"。但 2026-08-18 已在 D 盘生成并完整验证
新的私有异机恢复点（`20260818T083746Z`，54,468 成员 / 51,270 条清单 / 双 SQLite
`quick_check=ok` / 模型四重哈希一致）。**该理由现已不成立**，八步收口报告也承认
"具备安全前置条件"。

同时，细则只在 `FR-GOV-001` 里提了一句"记录例外"，**没有配套的例外登记表**。
一条被标为不可协商的硬门，正处于已知违规状态却没有带日期、理由和到期条件的
登记条目，会削弱整套硬门的可信度。

建议二选一并留决策记录：① 删除旧公共密文；② 在细则新增例外登记节，写明
FR-BAK-006 的当前例外、理由、责任人和复审日期。

### F3 · 低 · 课程侧六个提交一个字未动

| 项 | 状态 |
|---|---|
| `app/core/`（三个禁飞区内核） | **仍不存在** |
| `config/course_evidence_manifest.json` | 全部 `null` / `false` / 空数组 |
| `.agent/teacher_approval_request.md` | 停在 2026-07-19，仍写"误报 95%、`REMAIN_SHADOW`、360 tests" |
| `.agent/student_execution_pack.md` | 同上 |
| `docs/STUDENT_COURSE_HANDOFF.md` | 同上 |
| `README.md` | **已修**（2026-08-18，准确描述 v4 与人审盲集未冻结） |

README 已随主线更新，三份学生材料没跟上——它们是仓库里**仅剩的过期事实源**，
且恰好是要交给教师的那几份。

### F4 · 低 · 第二轮审计分支未合并

`origin/claude/repository-audit-q77ut2` 含 186 行独立复审（13 条发现 11 条关闭、
0 新增缺陷、附运行时实证），未进 `main`。其"剩余事项"第 5 条（AWS 生产验收）
已由本轮完成，其余 3 条仍开放。

### F5 · 低 · 两个已合并分支的残留引用

`codex/sec-shadow-evidence-clarity`、`codex/release-archive-lock-portability`
为 squash 合并后的残留，内容严格落后于 `main`，保留会造成"尚有未合并工作"的误解。
（F4 那份审计已提出，仍未处理。）

---

## 四、还能做什么

按"能否解锁被卡住的东西"排序，不按工作量。

### 工程侧

| # | 事项 | 为什么值得做 |
|---|---|---|
| 1 | 给 `freeze_human_blind_v3.py` 补契约测试 | 它是不可逆的一次性动作，又是模型出 shadow 的唯一路径。现在跑它等于裸奔 |
| 2 | 决定 6,499 / 4,275 / 2,729 / 105 这批历史债怎么处置 | 审计只分类不修复。四类各需要一个明确决定：重建、重跑、人工复核、还是标记作废 |
| 3 | FR-BAK-006：删旧公共密文，或登记带期限的例外 | 硬门处于已知违规且理由已失效，拖着会侵蚀整套门禁的可信度 |
| 4 | 细则补一个例外登记节 | 目前只有"记录例外"四个字，没有登记位置和字段 |
| 5 | 合并第二轮审计分支，删两个残留分支 | 零风险，消除误解 |
| 6 | 把 `/radar/offhost-status.json` 的历史过期问题写成回归测试 | 该路径曾公开 2026-07-22 的过期 `VERIFIED` JSON，现已 404，但没看到防回归契约 |

### 课程侧（决定成绩的部分）

| # | 事项 | 现状 |
|---|---|---|
| 7 | 写 `app/core/` 三个禁飞区内核 | 一行未写。这是唯一不能靠现有代码顶过去的部分 |
| 8 | 填 `config/course_evidence_manifest.json` | 全空，机器门禁 11 项全 `false` |
| 9 | 更新三份学生材料 | 仓库里仅剩的过期事实源 |
| 10 | **做真人双盲标注** | 见下 |

### 第 10 项值得单独说

本轮之后，真人盲审的**基础设施已经完全就绪**：凭据绑定主体与角色、
`human-blind-v3.1` 事件时点合同、issuer/event-chain 与近重复排除、来源分层、
六个历史数据集交叉排除、一次性哈希冻结门。代码明确拒绝用 AI 或一个人换别名
填满门槛。

**缺的只剩两个真人坐下来标 80 条**（RISK_REVIEW 30 / NON_TARGET 30 / ABSTAIN 20），
分歧由第三人仲裁。

这件事同时满足三个目标：

- **产品**：让 v4 有机会从 `QUALIFIED_SHADOW` 真正晋级，这是项目自己设的最后一道门；
- **课程**：必须两个人独立完成、互不可见、留下可核验记录——正是"团队协作"维度
  最硬的证据，而且它天然无法由一个人代劳；
- **工程**：为第 2 项的 4,275 条待重跑决策提供人工基线。

在补 `app/core` 之外，这是投入产出比最高的一件事。

---

## 五、结论

本轮的性质与前两轮不同：前两轮是**修补审计发现的缺陷**，本轮是**建立防止缺陷
重现的治理层**（所有者意图细则 + 29 条机器可执行硬门 + 强制同步测试），并且
顺带修掉了一个此前无人发现的真实正确性缺陷（零相关证据边）和一个只有真实恢复
演练才能暴露的发布合同缺口（漏打包模型文件）。

两处失败都被诚实保留而不是抹平：候选 `20260818T040331Z` 因健康探针超时自动回滚、
第一份异机归档因缺模型文件被恢复审计拒绝。这与项目自己的"不得把失败包装成成功"
一致。

工程侧唯一的中等风险是 F1（不可逆脚本零测试）；治理侧唯一的中等风险是 F2
（硬门自违规无登记）。**课程侧则完全没有进展**——而这恰恰是决定实训成绩的部分。
