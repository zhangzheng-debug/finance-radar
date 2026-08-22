# 事件分类与价格反应时间合同 V1

本文件是事件知识层、事实准入和事后价格审计之间的当前权威接缝。它不提供交易
建议；所有行情结果仍是 `post_event_audit_only`，不得进入发现排序、事实判断、
风险路由模型特征或人工盲审材料。

## 一套分类，不再各自命名

`config/event_taxonomy_v1.json` 把采集器遗留的 family/type 归入稳定产品类别，
并声明是否属于事实事件、默认时间锚以及可用的知识卡族。分类优先级固定，未知项
进入 `OTHER_UNMAPPED`，绝不猜测。运行：

```powershell
python scripts/audit_event_taxonomy.py `
  --db data/finance_radar.sqlite3 `
  --output reports/event_taxonomy_audit.json
```

覆盖率低于 95% 返回非零退出码。分类只提供产品语义，不会改写 canonical 状态。

## 价格窗口的准入条件

`scripts/observe_live_event_markets.py` 只有在以下条件同时成立时才排价格任务：

1. 事件已经通过结构化准入且当前工作流为 `EVIDENCE_READY`，canonical 可以仍是
   `candidate`、`weak` 或 `verified`；这样能及时观察，又不会把行情当成事实确认；
2. 资产关系明确允许市场观察；
3. 当前事件版本存在 `SCOPED_MATCH` 或 `HUMAN_CONFIRMED` 证据关系；
4. 主体、事件谓词、日期三项关系门均通过；
5. 分类/知识卡声明了锚点种类；
6. 锚点是带时区的精确时间戳，日期级值不能产生分钟窗口；
7. `local_received_at` 可解析，且任何报价不得早于
   `known_at=max(source_published_at, local_received_at)`。

失败时仍写 `market_event_anchors` 的 `UNAVAILABLE` 收据和原因，但不排价格任务。

## 锚点和窗口

允许的事件锚为：

- `source_published`：权威来源正式公开的精确时刻；
- `event_occurred`：结构化事实中记录的实际发生时刻；
- `filing_effective`：结构化事实中记录的生效时刻。

`first_capture` 只保留为历史降级词汇，不再是有效新锚点。

固定窗口为 `initial`、T+5m、T+30m、T+2h、T+1d、T+5d。非加密资产只有在
资产元数据明确给出 `session_timezone`、`regular_close_local`、
`trading_weekdays` 和 `holidays` 时才生成“下个收盘”；缺少交易日历会记为
`unsupported_windows=["next_close"]`，不会自行猜交易所日历。

任务错过宽限期即写 `MISSED_WINDOW`。当前价接口不会被用来回填历史窗口，也不会
把延后的报价继续标成原窗口。

## 可审计收据

- `market_event_anchors`：绑定事件版本、资产、供应商、声明锚、来源发布时间、
  本地接收时间、known_at、精度、延迟和不可支持窗口；
- `market_job_anchor_links`：把每个任务绑定到锚点和精确秒偏移；
- `market_snapshots.raw_json`：保存锚点种类、锚点时间、known_at、合同版本、
  计划时间和实际捕获延迟；
- `event_market_metrics`：只保存事后审计收益，数据库 CHECK 强制禁止模型和排序使用。

审计运行：

```powershell
python scripts/audit_price_windows.py `
  --db data/finance_radar.sqlite3 `
  --json-out reports/price_window_audit.json `
  --markdown-out reports/price_window_audit.md
```

以下任一情况都会得到 `ATTENTION`：旧任务无锚、锚非精确、绑定的事件版本不存在、声明不一致、
任务偏移不一致、报价早于 known_at、未声明锚、精确锚无任务、存在未支持窗口、
错过任务被回填，或事后指标越过隔离边界。

## 当前仍需现场配置的边界

代码已支持交易日历，但仓库中的旧资产元数据多数没有交易所会话字段，因此生产
迁移后审计可能诚实地报告 `next_close` 不可用。应由负责人从资产主数据补齐交易所
时区、收盘时间和节假日；不能根据 ticker 或数据供应商名称静默推断。

已有旧 `market_jobs` 没有锚点链接，保留为历史记录并由审计报出，不能把第一次报价
事后改名为事件时刻。锚定旧版本本身是合法历史事实，只要该版本仍可验证；是否
归档旧任务属于单独的数据迁移决策。
