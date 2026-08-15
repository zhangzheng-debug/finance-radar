---
document_type: ai_execution_spec
project_id: finance-radar
project_name_zh: 金融事件证据雷达
project_name_en: Finance Radar
version: 6.0
updated_at: 2026-07-22
language: zh-CN
target_course: 北京林业大学理学院2026年暑期实训
target_level: standard_95
project_category: 自拟项目 / 多源新闻聚合Agent增强版
lifecycle: deployed_read_only_baseline
public_terminal: https://radar.18-208-34-152.sslip.io:8443/radar/
formal_release: v2026.07.22.2
aws_release_path: /opt/finance-radar/releases/20260722T084500Z
current_state_source: CURRENT_STATE.md
product_charter: docs/PRODUCT_CHARTER.md
execution_mode: read_only
trading_enabled: false
automatic_verification_enabled: false
model_promotion_state: shadow_fail
teacher_approval_required: true
---

# Finance Radar V6.0 — AI 可执行项目书

## 0. 读取规则

后续 AI 在修改项目前必须按以下顺序读取：

1. `docs/PRODUCT_CHARTER.md`，确定稳定目标、用户优先级与边界；
2. `CURRENT_STATE.md`，区分最新 tag、当前源码与最后一次现场证据；
3. 本文件，作为课程与工程执行规格；
4. `.agent/architecture.md`；
5. `.agent/coding_conventions.md`；
6. `.agent/api_contracts.md`；
7. `.agent/data_model.md`；
8. `.agent/test_strategy.md`；
9. `.agent/deployment_runbook.md`；
10. `.agent/forbidden_zones.md`；
11. 当前 Git 状态、当前分支、待改文件及相邻测试；
12. 服务器当前状态，仅在任务明确授权服务器操作时读取。

不得依据旧聊天记录推断当前线上状态；凡是会变化的数据必须现场验证。
旧材料中出现的 `167.172.69.16` 新加坡主机与对应 `sslip.io` 地址只视为迁移历史；当前正式入口以本文件顶部的 AWS 地址和现场健康检查为准。

## 1. 一句话定义

Finance Radar 是一个只读、可追溯、可回放的金融事件情报系统：它从多类公开来源发现事件，保存原文证据和修订关系，把 AI 输出限制在“辅助筛选和解释”，再通过网页终端与 Telegram 提醒帮助人快速核验；它不下单、不自动认定事实，也不承诺收益。

## 2. 给零基础读者的业务模型

```text
官方网页或发现源出现消息
  -> 采集器保存原文与抓取时间
  -> 系统识别公司、主题、事件类型和风险极性
  -> 证据门检查是否有原文、是否冲突、是否足够
  -> 行情适配器补充事件前后的价格窗口
  -> 网页终端展示“结论 + 原文 + 时间线 + 运行证据”
  -> 符合条件时 Telegram 只发提醒和深链
  -> 人作最终判断
```

## 3. 核心问题与价值

### 3.1 问题

- 金融消息散落在监管机构、发行人官网、新闻聚合源和社交平台。
- 同一事件可能被转载、截断、修订，标题不能代替证据。
- 通用大模型可能把猜测说成事实，且难以复现当时依据。
- 实时演示无法保证恰好出现 SEC 重大事件。
- 学生项目容易只有漂亮界面，缺少工程证据、测试与可回放性。

### 3.2 价值

- 从“看见一条消息”升级为“知道哪份原文在什么时间证明了什么”。
- 以事件账本、内容寻址归档和证据边支持审计与复盘。
- 以硬门阻止弱证据、冲突证据或失败模型被包装成确定结论。
- 以回放数据保证答辩可控，同时保留实时链路作为加分项。

## 4. 明确边界

### 4.1 必须做

- 多源事件采集、去重、规范化和版本记录。
- 原文归档、哈希校验、证据边和时间戳。
- 风险事件路由、证据充分性检查和冲突展示。
- 行情窗口补充、网页终端、Telegram 通知。
- 回放、运行状态、模型状态、人工裁决和备份恢复证据。
- 可测试、可部署、可解释、可审查的工程材料。

### 4.2 明确不做

- 不连接下单接口，不提交订单。
- 不读取或展示账户持仓、余额和资金。
- 不输出“必涨、必跌、稳赚”等交易承诺。
- 不把模型分数当事实，不自动升级为 VERIFIED。
- 不将 Telegram 频道、X 帖子或聚合标题当最终事实依据。
- 不绕过网站条款、登录限制、付费墙或反爬措施。
- 不在 AWS 上部署或改动现有量化交易程序与 VPN。

## 5. 当前已验证基线

> 本节是 2026-07-22 验收快照。后续展示前必须重新生成当前报告，不得把本节数字冒充实时值。

| key | verified_value |
|---|---:|
| ledger_schema | 12 |
| operations_schema | 4 |
| events | 1670 |
| verified_events | 546 |
| candidate_events | 773 |
| evidence_edges | 2399 |
| immutable_evidence_objects | 1595 |
| registered_sources | 22 |
| worker_interval_seconds | 300 |
| worker_status_at_acceptance | SUCCESS |
| online_backups | 48 |
| automated_tests | 364 |
| subtests | 17 |
| offhost_restore | PASS |
| trading_violations | 0 |
| auto_verify_violations | 0 |
| secret_leakage_violations | 0 |

当前不足必须如实保留：

- 公开盲测中模型门槛为 `FAIL`，模型只能保持 `SHADOW`。
- 人工双人裁决样本尚未完成，不能虚构完成记录。
- 课程教师尚未对自拟题、三处禁飞区和学生工作量作最终批准。
- 现有工程基线由 AI 辅助构建，不能追溯性地冒充学生在实训现场完成的过程。

## 6. 来源分类

### 6.1 事件源

| priority | role | examples | usage_rule |
|---|---|---|---|
| P0 | 权威确认源 | SEC、CFTC、FTC、FDIC、Federal Reserve、BLS、FDA、ECB、EIA | 可作为事实确认主证据；仍须保存原文和时间 |
| P1 | 官方主体源 | 上市公司投资者关系页、官方新闻稿，如 NVIDIA | 可作为主体声明证据；与监管事实分开标记 |
| P2 | 发现源 | OpenNews、公开聚合页、合规 RSS、公开社交线索 | 仅用于发现与交叉验证；不得单独自动确认 |

规则：来源优先级表达“证据权威程度”，事件极性表达“正面/负面/中性/未知”，两者不得使用同一字段或同一颜色语义。

### 6.2 行情源

| source | coverage | deployment_rule |
|---|---|---|
| Twelve Data | 股票、外汇、指数等 | AWS 可使用受配额限制的公开/免费 API；必须缓存并记录错误 |
| Binance public market data | 加密资产 | 只取公开报价；不使用交易权限与账户接口 |
| IBKR TWS read-only probe | 多资产补充 | 仅本机人工启动、只读验证；不作为 AWS 后端强依赖，不允许下单 |

事件源与行情源必须使用不同适配器、状态字段和故障处理。行情缺失时展示 `MISSED_WINDOW` 或 `UNAVAILABLE`，不得伪造价格。

## 7. 系统架构

```yaml
layers:
  collectors:
    purpose: 定时获取公开事件与发现线索
    outputs: raw_payload, source_id, fetched_at, request_metadata
  normalizer:
    purpose: 统一时间、主体、事件类型、链接和文本
    outputs: canonical_candidate
  ledger:
    purpose: 追加式保存事件、修订、状态和来源
    invariant: 历史记录不可静默覆盖
  evidence_store:
    purpose: 内容寻址保存原文快照及哈希
    invariant: 每个确认结论可回到证据对象
  evidence_graph:
    purpose: 建立事件、证据、来源、修订、冲突之间的边
  risk_router:
    purpose: 高召回筛选潜在负面风险事件
    mode: SHADOW
  hard_gates:
    purpose: 检查证据充分性、冲突、目标范围和模型状态
    invariant: AI分数不能绕过硬门
  market_adapters:
    purpose: 提供事件前后行情窗口
    invariant: 失败必须显式可见
  api:
    purpose: 向只读前端输出事件、证据、运行与回放数据
  web_terminal:
    purpose: 五页证据终端
  telegram:
    purpose: 发送符合条件事件的通知和深链
    invariant: 不是事实数据库，也不是唯一界面
  backup_restore:
    purpose: 在线备份、异地加密恢复包和恢复演练
```

## 8. 数据契约

### 8.1 Event 最小字段

```yaml
Event:
  event_id: stable_string
  canonical_key: stable_string
  title: string
  summary: nullable_string
  occurred_at: nullable_utc_datetime
  first_seen_at: utc_datetime
  last_seen_at: utc_datetime
  entity_ids: [string]
  event_type: enum
  polarity: [positive, negative, neutral, unknown]
  status: [candidate, verified, rejected, superseded]
  risk_score: nullable_float_0_1
  model_version: nullable_string
  evidence_sufficiency: [sufficient, weak, conflict, missing]
  revision_of: nullable_event_id
```

### 8.2 Evidence 最小字段

```yaml
Evidence:
  evidence_id: content_addressed_hash
  source_id: string
  source_priority: [P0, P1, P2]
  source_url: string
  fetched_at: utc_datetime
  published_at: nullable_utc_datetime
  mime_type: string
  content_hash: sha256
  archive_path: string
  excerpt: bounded_string
  supports: [claim_id]
  contradicts: [claim_id]
```

### 8.3 不变量

1. `verified` 事件至少关联一条符合策略的证据边。
2. 原始证据对象以哈希寻址，不允许原地覆盖。
3. 事件修订产生新版本或明确 revision 边，不静默改历史。
4. `SHADOW` 模型输出只能是建议字段，不能自动改变事实状态。
5. 所有时间内部使用 UTC，界面可转换时区但必须标注。
6. 所有公开输出均不得包含令牌、密钥、账户信息或内部恢复口令。

## 9. 产品界面契约

| page | purpose | must_show |
|---|---|---|
| Situation Room | 总览系统是否活着、今天发生什么 | 指标、事件流、来源健康、模式与更新时间 |
| Event Workbench | 单事件核验 | 原文、证据矩阵、修订、冲突、行情窗口、硬门结果 |
| Replay Lab | 回放历史事件 | 模拟时钟、逐步推进、当时可见证据、禁止未来信息泄漏 |
| Ops & Model | 公开运行质量 | worker、采集器、备份、测试、模型门槛和失败状态 |
| Adjudication | 人工盲标与裁决 | 匿名样本、双人标签、分歧、裁决、版本冻结 |

通用 UI 规则：

- 默认深色专业终端；桌面优先，移动端不得横向溢出。
- 全局保持 `NO TRADING` 边界标记，不得出现可执行交易控件。
- 同时使用颜色和形状表达状态，不能只靠颜色。
- `DEMO SNAPSHOT`、`LIVE`、`SHADOW`、`FAIL` 必须醒目标注。
- 所有结论必须能点击到证据；所有空缺必须诚实显示。
- Telegram 提醒必须链接回事件工作台，不复制成第二套事实存储。

## 10. AI 与模型契约

### 10.1 AI 可做

- 生成采集器、解析器、测试、文档和 UI 初稿。
- 对候选事件做高召回风险路由和摘要建议。
- 提议去重、实体识别、证据关联和异常分类。
- 在明确输入与版本号下生成解释性摘要。
- 辅助生成 Bug 注入样例、测试用例和答辩练习问题。

### 10.2 AI 不可做

- 不得自动确认事实或自动发布为 VERIFIED。
- 不得绕过证据硬门、冲突门和模型晋级门。
- 不得在公开盲测 v1 上反复调参后仍称其为盲测。
- 不得删除、改写或隐藏失败、缺失、冲突和人工分歧。
- 不得生成或调用任何交易动作。
- 不得伪造学生提交记录、教师批准、人工标签或测试结果。

### 10.3 风险模型定义

```yaml
risk_router:
  algorithm: TF-IDF + calibrated logistic regression
  intended_use: 高召回识别潜在负面风险事件
  why_negative_first: 负面事件通常更突发、尾部风险更大、需要更快核验
  positive_event_handling: 仍采集与展示；进入正面或中性分类，不套用负面风险结论
  external_blind_set_size: 40
  observed_risk_recall: 1.00
  observed_normal_false_risk_rate: 0.95
  promotion_gate: FAIL
  deployment_state: SHADOW
  promotion_allowed: false
```

解释：100% 风险召回不代表模型可用，因为它把大量普通新闻也判成风险。当前最重要的模型任务是积累独立双人裁决样本、冻结新盲测集，再在不污染盲测的前提下降低误报。

### 10.4 本地小模型

Qwen2.5-0.5B/llama.cpp 仅用于 advisory 摘要或离线演示。它不是事实判定器，也不能因“部署在服务器上”自动获得更高可信度。

## 11. 课程要求映射

```yaml
course_alignment:
  engineering_practice:
    evidence: 真实AWS部署, 定时采集, 数据库迁移, HTTPS终端, 备份恢复
  ai_code_review:
    evidence: AI输出必须经过diff审查, 测试, 失败记录, 模型门槛
  engineering_standards:
    evidence: .agent规范, API契约, 数据模型, 测试策略, 部署文档, 禁飞区
  teamwork:
    evidence: PO/SM/QA角色, Sprint看板, PR审查, 双人裁决
  ai_in_core_business:
    evidence: AI负责候选路由与解释建议, 但受证据硬门控制
  standard_95_fit:
    evidence: 多源新闻聚合Agent + 可审计证据链 + 回放 + 模型治理
```

关键诚信规则：已有工程只能作为“起始基线”。学生在 12 天中必须留下可核验的新增需求、设计、提交、测试、Bug 修复、即兴修改和反思记录，才能计入课程过程分。

## 12. 学生所有权与角色

推荐 3 人配置；2 人时合并 PO 与 SM，QA 仍保持相对独立。

| role | responsibilities | mandatory_human_output |
|---|---|---|
| PO / 产品负责人 | 需求、范围、验收、答辩叙事 | 需求优先级、验收表、演示脚本 |
| SM / 工程负责人 | 架构、接口、部署、Sprint节奏 | 架构图、接口设计、部署记录、代码走查 |
| QA / 质量负责人 | 测试、Bug注入、盲标与审计 | 测试用例、Bug报告、裁决记录、质量报告 |

全员必须提交可解释代码；不能由一人包办，其余人只做 PPT。

## 13. 三处 AI 禁飞区候选

> 必须由教师批准，学生现场手写或独立完成，并能逐行解释。AI 不得替代实现。

```yaml
forbidden_zones:
  - id: FZ1
    name: event_identity_and_revision
    purpose: 决定事件稳定身份、重复合并和修订关系
    why: 错误会污染全部历史与回放
    acceptance: 边界测试通过且学生逐行讲解
  - id: FZ2
    name: evidence_hard_gate
    purpose: 决定证据不足、冲突或来源等级不够时是否拦截
    why: 它是系统诚信边界
    acceptance: 反例测试通过且不能被模型分数绕过
  - id: FZ3
    name: deterministic_replay_clock
    purpose: 保证回放只看到模拟时点之前的证据
    why: 防止未来信息泄漏和伪造预测能力
    acceptance: 固定输入重复运行结果一致
```

## 14. 12 天 Sprint 计划

| day | target | human_evidence | acceptance |
|---|---|---|---|
| 1 | 分组、选题、范围冻结、Git/看板 | 立项记录、角色、首批 issue | 教师确认方向；每人有任务 |
| 2 | 五项人工设计：架构/API/数据/测试/部署 | 设计评审记录 | 能解释每层职责与边界 |
| 3 | 熟悉基线与代码走查 | 模块地图、风险清单 | 每人可从页面追到数据来源 |
| 4 | Sprint 1：来源健康与事件流 | 小提交、测试、AI日志 | 真实源与演示源明确区分 |
| 5 | Sprint 1：证据工作台 | PR、证据边测试 | 结论可回到原文 |
| 6 | Sprint 1：Bug注入与回顾 | 3个Bug、30分钟修复记录 | 定位过程可复述 |
| 7 | Sprint 2：回放与盲标 | 回放测试、匿名标注 | 无未来信息泄漏 |
| 8 | Sprint 2：模型治理与误报分析 | 模型卡、失败案例 | FAIL/SHADOW 诚实展示 |
| 9 | Sprint 2：即兴修改 | 30分钟提交与测试 | 不破坏主链路 |
| 10 | Sprint 3：UI、可访问性、移动端 | 截图、检查表 | 五页清晰、零横向溢出 |
| 11 | 恢复演练、全量测试、代码走查 | 恢复证据、测试报告 | 测试/备份/恢复均可复核 |
| 12 | 现场演示、答辩与复盘 | 演示记录、个人反思 | 实时失败时可无缝回放 |

每个 Sprint 必须产出：计划、细粒度提交、AI 使用日志、测试、3 个 Bug 注入记录、回顾与下一轮调整。

## 15. Bug 注入库

优先使用可复现、可观察、不会损坏生产数据的 Bug：

1. 时区被重复转换，事件排序偏移 8 小时。
2. P2 发现源被错误标记为 P0。
3. 事件修订覆盖旧记录而不是创建 revision 边。
4. 内容哈希错误导致相同证据重复归档。
5. 回放接口泄漏模拟时点之后的证据。
6. 行情 API 429 被误显示为价格为 0。
7. 模型 `FAIL` 时 UI 仍显示“已通过”。
8. Telegram 重试造成重复通知。
9. 前端只用颜色表达正负，色弱用户无法区分。

每次记录字段：`bug_id`、注入提交、症状、假设、定位命令、根因、修复提交、回归测试、耗时、个人反思。

## 16. 即兴修改预案

练习四类 30 分钟变更：

- 新增一种来源状态并贯通数据库/API/UI。
- 给事件工作台增加“只看冲突证据”筛选。
- 给回放页增加时区切换但保持内部 UTC。
- 给 Telegram 消息增加证据等级与网页深链。

执行顺序固定：复述需求 -> 找契约 -> 写最小失败测试 -> 小改动 -> 回归测试 -> 展示 diff -> 说明风险。时间不足时保留正确的小范围实现，不做未经测试的大重构。

## 17. 验收门

```yaml
acceptance_gates:
  G1_scope:
    pass_if: no_trading and no_account_data and read_only
  G2_evidence:
    pass_if: verified_claims_have_traceable_evidence
  G3_replay:
    pass_if: replay_is_deterministic_and_no_future_leakage
  G4_model:
    pass_if: failed_model_remains_shadow_and_failure_is_visible
  G5_sources:
    pass_if: source_priority_and_polarity_are_separate
  G6_resilience:
    pass_if: collector_failures_and_market_gaps_are_explicit
  G7_quality:
    pass_if: tests_pass_and_static_checks_have_no_blocking_issue
  G8_recovery:
    pass_if: encrypted_offhost_restore_is_repeatable
  G9_course_process:
    pass_if: student_commits_reviews_ai_logs_bug_drills_and_reflections_exist
  G10_demo:
    pass_if: live_and_snapshot_modes_are_labeled_and_both_paths_are_prepared
```

任何一门失败，不允许通过改文案把它包装成通过。

## 18. 答辩演示契约

### 18.1 三分钟主路线

1. 20 秒：说明金融消息的“真假、证据、修订、时效”问题。
2. 30 秒：Situation Room 展示系统活性和多源健康。
3. 70 秒：Event Workbench 打开一个事件，沿证据边回到原文，展示冲突与行情窗口。
4. 35 秒：Replay Lab 重放历史事件，解释为什么实时演示不依赖碰运气。
5. 25 秒：Ops & Model 展示 `FAIL / SHADOW`、测试和备份恢复证据。

### 18.2 演示降级顺序

```text
LIVE 正常 -> 展示实时链路
LIVE 无重大事件 -> 展示最新普通事件 + 历史回放
外部源限流 -> 展示来源故障证据 + 本地快照回放
公网不可用 -> 本机快照 + 恢复包清单 + 预录短视频
```

演示数据必须带 `DEMO SNAPSHOT` 标记；禁止假装实时。

## 19. 交付物清单

```yaml
deliverables:
  human_project_proposal: financial_event_radar_project_proposal_v6_0_human.docx
  ai_project_spec: financial_event_radar_project_plan_v6_0_ai.md
  agent_governance_dir: .agent/
  web_terminal: deployed public read-only terminal
  source_registry: versioned source configuration
  evidence_ledger: schema and sample/export with redaction
  model_card: blind result and promotion decision
  test_report: automated and manual evidence
  recovery_pack: encrypted off-host artifact plus sha256 manifest
  student_process:
    - sprint plans and retrospectives
    - granular commits and reviews
    - ai usage logs
    - five human design documents
    - forbidden-zone code and explanations
    - bug injection reports
    - improvised-change record
    - individual reflection
```

## 20. 修改协议

后续 AI 每次执行修改遵守：

```text
1. git status --short + branch
2. 读本文件和相关 .agent 契约
3. 明确任务范围、非目标和受影响层
4. 先找已有测试与现有实现
5. 小步修改，不覆盖用户无关变更
6. 添加或更新测试
7. 执行最小相关测试，再执行全量关键检查
8. 输出事实、失败和未验证项
9. 未经明确授权不提交、不推送、不部署
10. 涉及线上状态时重新验证，不用旧快照代替
```

## 21. AI 停止条件

遇到以下任一条件，AI 必须停止扩大范围并报告：

- 修改可能触达交易、资金、账户权限或订单接口。
- 需要真实密钥但当前没有安全注入渠道。
- 要删除/覆盖生产数据、备份、量化程序或 VPN。
- 现有用户改动与目标文件冲突，无法安全隔离。
- 教师批准、学生身份、人工裁决等必须由人完成的证据缺失。
- 外部条款或授权不明确，继续抓取可能违规。
- 关键验收失败三次且没有新的可验证假设。

## 22. 当前最高优先级

遵循木桶原则，先补最短板：

1. 完成真实双人盲标与裁决，建立不可伪造的人工作业证据。
2. 由教师确认自拟题、难度、三处禁飞区和已有基线的计分方式。
3. 建立学生自己的 Sprint、细粒度 Git、代码走查、AI 日志和个人反思。
4. 准备稳定回放包、实时/离线双路线和 Bug/即兴修改演练。
5. 在不扩充新功能的前提下刷新全链路测试、备份恢复与展示材料。

完成以上事项前，不优先增加新的新闻源、模型或复杂前端框架。

## 23. 成功定义

本项目成功不等于“预测涨跌准确”或“界面像交易终端”。成功定义为：

- 一个零基础观察者能在三分钟内理解事件、证据、时间线和系统边界；
- 一名教师能从提交、测试、文档、Bug 修复和学生解释中确认真实工程能力；
- 一名后续开发者或 AI 能依据契约复现、测试、部署和恢复系统；
- 任何模型失败、数据缺失或来源冲突都被诚实展示；
- 全系统保持只读，不产生交易和资金风险。
