# API 捕获内容解读合同

## 目的

该功能只回答一个问题：**第三方 API 当时返回的标题或摘要，大致在说什么？**

它不是证据补全器，也不是事件真伪、重大性、极性、价格方向或交易判断器。采集记录仍属于 discovery 层；只有经过既有 P0/P1 证据与人工核验流程的材料，才可能改变正式事件状态。

## 当前实现

- `GET /api/v1/events/{event_id}/source-interpretations` 为公开只读接口。
- 页面优先读取与当前 `capture_receipt_sha256` 精确匹配的已缓存结果；没有缓存时，只返回零费用的确定性边界说明。
- `POST /api/v1/events/{event_id}/sources/{observation_id}/interpret` 仅 Operator 可调用，当前只生成并缓存确定性预览。
- 普通页面请求不会排队、不会调用外部服务、不会产生费用。
- 输出与来源 revision、语义内容哈希、事件版本、提示词哈希绑定；任一输入变化，旧结果不再命中。
- Operations Schema 9 分开保存作业与每次供应商尝试，包含原子费用预留、请求计数、租约、退避、失败用量和不可变幂等键。
- 后台单条运行器和有界批处理 Worker 可以使用 DeepSeek 生成 `LLM_ASSISTED` 缓存；两者都不接在 Public 请求路径上。
- 并发 Worker 通过 `BEGIN IMMEDIATE` 在同一事务中完成“记录用量预留、取得租约”；若将来重新设置日上限，也不能由并发任务分别越过该上限。
- 供应商返回了 token 用量但随后合同校验失败时，用量仍会落账；完全拿不到用量时按每次 0.02 元人民币预留值记录估算费用。
- 可重试故障最多 4 次并带退避；过期租约会回收。合同拒绝和不可恢复错误进入失败终态，不会无限烧费。
- 首次启用时，Worker 会遍历全部符合条件的历史采集收据，并把当前模型代际、来源水位、候选数和剩余数持久化到 Operations 数据库。历史收据达到终态后不再逐条查库或调用供应商。
- 历史回填完成后，每次定时运行先比较 `raw_observations/source_revisions` 水位；水位不变时直接返回 `SOURCE_GENERATION_UNCHANGED`。只有新增或修订后产生新 `capture_receipt_sha256` 的内容才会进入新调用。

## DeepSeek 供应商合同（2026-08-21 核验）

- 官方 Base URL：`https://api.deepseek.com`；
- 固定模型：`deepseek-v4-flash`，即当前最便宜的纯文本适用型号；
- 固定 `response_format={"type":"json_object"}`；
- 固定 `thinking={"type":"disabled"}`，避免本任务不需要的思考 token；
- 固定最大输出 700 tokens；
- 当前按产品决策不设日请求和日费用上限：两个配置均为 `0`，明确表示 unlimited；
- “不限预算”不等于无限循环：单批数量、并发租约、超时、最大 4 次重试及最大输出 token 仍保持硬边界；
- 费用按官方高峰价格保守估算：缓存命中输入 0.10 元/百万 tokens、缓存未命中输入 3.00 元/百万 tokens、输出 9.00 元/百万 tokens。空闲时段实际价格更低，但预算门不使用折扣价。

官方参考：[模型与价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)、[JSON Output](https://api-docs.deepseek.com/zh-cn/guides/json_mode/)、[思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)。

## 将来模型可见的输入

只允许以下白名单字段：

- 事件 ID、当前版本、公开处置状态、事件家族与事件类型；
- 来源名称、来源类型、权威层级；
- 限长标题与限长摘要；
- 来源发布时间、本地接收时间、来源 revision；
- 语义内容哈希与采集收据哈希。

不得发送 `raw_json`、API score/grade/signal、内部 trace、模型结论、价格方向、密钥、数据库路径或用户信息。

## 模型只能返回什么

外部模型只能返回固定 JSON：

- 一句话中文解释；
- 原文明确表达的主张，以及逐字引用；
- 原文没有证明什么；
- 行为主体、受影响资产及其原文引用；
- 语气阶段（已发生、已宣布、拟议、条件性、否认、评论或不清楚）；
- 若要改变当前状态还缺什么；
- 是否发现疑似提示词注入。

服务器会拒绝多余字段、虚构引用、未出现在来源中的资产或数值、非法阶段、过长字段和可疑注入文本。正式状态说明、安全标记、事件版本、收据哈希和提示词哈希均由服务器写入，模型无权提供或覆盖。

## 永久边界

解读结果必须始终满足：

- `canonical_mutation_allowed=false`；
- `used_as_event_truth=false`；
- `used_as_model_feature=false`；
- `price_used_as_truth=false`；
- `no_trading=true`。

它不得写入 `event_evidence`，不得重新打开已排除事件，不得进入 RiskRouter、重大性/极性标签、价格审计或交易路径。

## 本地密钥与运行

密钥只允许位于 Git 已忽略的 `.env.local` 中，变量名为 `DEEPSEEK_API_KEY`。不得写入 README、命令历史、测试 fixture、systemd unit、日志或 Git。可使用交互式本机配置脚本，输入会被遮罩：

```powershell
pwsh -NoProfile -File scripts/configure_deepseek_local.ps1
```

单条运行：

```powershell
python scripts/run_capture_interpretation_deepseek.py `
  --event-id <event-id> `
  --observation-id <observation-id>
```

运行器只输出安全元数据与费用估算，不输出密钥、完整上游响应或来源原文。

有界批处理（生产单轮最多处理 20 条；用量仍逐次记录，但当前不设每日金额或请求数上限）：

```powershell
python scripts/run_capture_interpretation_worker.py --limit 20 --scan-limit 100000
```

仓库同时提供 `finance-radar-capture-interpretation.service/.timer`，部署安装器只安装、不启用。生产启用前必须把服务专用密钥放入 root-only 的 `/etc/finance-radar/deepseek-api-key`，由 systemd `LoadCredential=` 注入；不得把通用密钥复制进 `/etc/finance-radar.env`。

历史回填只创建独立、可追踪的解读数据，不会批量覆盖确定性结果。仓库测试证明的是合同与失败路径，不是生产供应商的长期可用性；页面在没有与当前收据精确匹配的合格缓存时，仍明确显示“外部模型待接入”。
