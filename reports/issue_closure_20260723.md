# Finance Radar 证据、影子模型与终端问题闭环记录

日期：2026-07-23  
生产环境：AWS `us-east-1`，只读终端  
生产版本：`/opt/finance-radar/releases/20260723T063259Z`  
上一可回滚版本：`/opt/finance-radar/releases/20260722T084500Z`

## 1. 本次解决的问题

### 1.1 SEC 首页套话被误判为“非事件”

- 原因：旧逻辑只抓主文件，可能只读到 8-K 封面；未找到预设风险词时便把事件永久拒绝。
- 修复：
  - 同时选择主文件及决策相关的 `EX-2`、`EX-4`、`EX-10`、`EX-99` 附件；
  - 保存版本化文档清单、哈希、缺失附件和覆盖状态；
  - 无明确语义匹配不再等于“非事件”，改为非终态证据复核；
  - 只重新开放具有精确旧错误原因、且未被人工审核的历史事件；
  - 纠错任务优先于普通新文件，另提供按 `event_id` 定向补抓能力，避免旧事件被新文件长期挤压。

生产证据：

- EMAT 事件 `FR-LIVE-33acb79fba9792c0d20d2b877ed991d8` 已由 `REJECTED` 纠正为 `CANDIDATE`；
- 当前版本为 `v3`，变更原因为 `sec_primary_semantic_inconclusive_reopened`；
- 已完成定向 SEC 重抓；
- 当前任务为 `PENDING_EVIDENCE_REVIEW`，原因是
  `sec_semantic_inconclusive:no_scoped_event_match`；
- 系统没有把不完整材料伪装成确定结论。

### 1.2 “模型 100%”实际是规则门，却被界面当成模型置信度

- 所有路由输出现在显式记录：
  - `decision_source`
  - `semantic_model_invoked`
  - `confidence_applicable`
- 影子运行被持久化到审计库，并区分：
  - `MODEL_EXECUTED`
  - `GATED_BEFORE_MODEL`
  - `POLICY_GATE_DECISION`
  - `FALLBACK`
- EMAT 的实际轨迹是：
  - `decision_source=DETERMINISTIC_EVIDENCE_GATE`
  - `execution_status=GATED_BEFORE_MODEL`
  - `semantic_model_invoked=false`
  - `confidence_applicable=false`
  - 路由结果 `ABSTAIN`
- 网页现在显示“未调用 · 证据门”，不再显示虚假的“模型 100%”。

### 1.3 影子结果只有临时计算，没有历史记录

- 新增幂等、限量的生产影子批处理；
- 每次记录输入哈希、事件版本、证据 ID、模型版本、执行路径和延迟；
- Worker 每轮为尚无当前结果的事件补记影子运行；
- Evidence Agent 只在证据准备后由后台自动运行，公开页面没有假按钮。

### 1.4 搜索只搜索已加载的少量事件，零结果仍显示旧详情

- 顶部搜索已接入服务器 `/api/v1/events?q=...`；
- 可按公司、Ticker、事件类型和 Event ID 检索；
- 零结果时清空详情区，明确显示“没有检索结果”；
- 生产浏览器已验证精确 Event ID 返回 1 条 EMAT 事件；
- 不存在的查询返回 0 条，且不会残留 EMAT 详情。

### 1.5 备份数字含义错误

旧界面的“54 个备份”把历史运行次数当成当前保留文件数。现在拆分为：

- 历史备份运行：`55`
- 已验证 / 失败：`55 / 0`
- 当前保留文件：`25 日备 + 2 周备`
- 保留上限：`30 日 + 12 周`
- 最新服务器备份：`20260723T063853Z`
- 异机恢复审计：`PASS`

### 1.6 盲标页面用途不清

- 页面改名为“模型人工验收”；
- 明确说明：两名审核者看不到模型答案，独立标注重大性、极性和证据状态；
- 一致结果成为人类评测基准，冲突交第三人裁决；
- 此页面用于检验模型，不审核或改变实时事件；
- 当前仍为 `0 / 24`，这是需要人工完成的工作，不由程序伪造。

### 1.7 模型页一直展示过期的 V1 失败

- 生产页动态读取当前模型状态；
- 当前版本：`risk-router-v4-c82cfde20465`
- 当前外部盲测门：`PASS`
- 治理结论：`QUALIFIED_SHADOW`
- 历史 V1 失败仍保留为审计档案，但不再冒充当前状态；
- 模型继续保持 `SHADOW`，不自动改变事件状态，也没有交易能力。

## 2. AWS 发布与恢复保护

- 发布前已完成 SQLite 在线备份并通过 `quick_check`；
- 新版本先完成 Python 编译、模块导入和 UI 标记预检；
- 通过原子软链接切换后重启：
  - `finance-radar-api`
  - `finance-radar-web`
  - `finance-radar-worker`
- `finance-radar-evidence-llm` 与 `nginx` 同时保持运行；
- 当前五个服务均为 `active`；
- 生产健康为 `status=ok`、账本 `quick_check=ok`；
- 当前 UI SHA-256：
  `c659e09deee483cbf46343ad02f6c9ec77729ffa4495e4b0855d5b72f916465a`
- 上一版本和上线前 UI 均保留，可独立回滚。

## 3. 验证结果

- Python 全套测试：`401 passed`
- SEC 专项与新队列测试：通过
- 前端 JavaScript 语法：通过
- Git 差异检查：通过
- 浏览器桌面验收：
  - 无横向溢出；
  - 无禁用的误导按钮；
  - 服务器搜索成功；
  - 零结果清空旧详情；
  - 模型当前状态、备份含义和盲标用途均按新口径展示。

## 4. 边界与尚需人工的事项

- EMAT 目前是“证据不足后的诚实弃权”，不是已确认风险事件；
- Evidence Agent 未在该事件上运行是预期行为：没有决策级证据时不应让模型补全事实；
- `0 / 24` 双人盲标必须由真实人员完成；
- 本次没有修改、启动或调用任何交易程序，也没有修改 VPN；
- 系统仍是情报与人工复核工具：无下单、无仓位、无余额、无自动交易。
