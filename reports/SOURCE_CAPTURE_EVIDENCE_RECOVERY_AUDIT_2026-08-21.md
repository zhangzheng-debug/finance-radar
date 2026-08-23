# 来源采集与零证据恢复审计（2026-08-21）

## 结论

页面中的“0 条证据”主要不是 API 原始输入为空，而是系统只把 `event_evidence` 当作证据，却没有在 Public 页面展示仍保留在 `raw_observations/source_revisions` 的发现载荷。修复后，这两层有明确且不可混淆的产品合同：

- 采集来源记录解释“系统当时收到了什么”；
- 可引用支持证据解释“什么材料支持当前版本的具体事实”；
- 前者永远不能自动升级为后者。

## 只读生产快照复核

数据来源：`finance_radar_production_20260821T023300Z.sqlite3`，是 2026-08-21 02:33 UTC 的只读导出，不代表部署后的实时数字。

| 指标 | 数量 |
|---|---:|
| canonical events | 14,755 |
| event_evidence rows | 15,155 |
| event_evidence=0 的事件 | 994 |
| raw observations | 26,493 |
| 未绑定事件投影但仍保留的 observations | 9,304 |

新恢复工具对 994 条零证据事件给出互斥分桶：

| 分桶 | 数量 | 含义 |
|---|---:|---|
| `SEC_OVERSIZE_REFETCH_READY` | 381 | SEC 来源和 URL 都在，历史 enrichment 因 5 MB 文档上限失败 |
| `OFFICIAL_REFETCH_READY` | 4 | 其他 P0/P1 官方来源可定点重拉 |
| `P2_CAPTURE_ONLY` | 477 | 有聚合/发现来源链接，应保留并另找权威原文 |
| `NO_URL_RAW_ONLY` | 132 | 有本地 title/summary/raw receipt，但没有可用链接 |
| `SOURCE_DELETED` | 0 | 当前快照没有仅剩删除修订的事件 |
| `NO_CAPTURE` | 0 | 994 条全部至少保留一条采集记录 |

另有 9,304 条 orphan observations。它们是采集记录，不是事件，不能与 14,755 个 canonical events 相加；只能重新通过当前 discovery admission，不能直接恢复成正式事件。

恢复产物：`D:\FinanceRadarReviewKits\source-observation-recovery-20260821`。

- 逻辑快照：`c0a7703d9bc5ff51fc8643402d835f50b36edf07f1c47b00e4ad1091990d030b`
- `recovery_records.jsonl` SHA-256：`45c9d54a520e15b9e40d03160897e1da878e0b4d1c8005784478a070636d8885`
- 分桶完整：`true`
- 网络请求：`0`
- canonical mutation：`0`

## GOLD 精确案例

事件 `FR-LIVE-60f9b6af7df61903778b203383148b44` 的 OpenNews 载荷仍在：

- provider item：`macro:news:3637286`
- 原链接：`https://x.com/FirstSquawk/status/2089870834508927268`
- 语义内容哈希：`386a649b4097ffa49478622fae19b67ae945220407f35197b8f6aa4fa1fcc958`
- raw payload 哈希：`46886978b3090dc0341abc1b915c34906479b6cabac8a0dd9de28b03e8734197`
- 分桶：`P2_CAPTURE_ONLY`

标题描述中东紧张、油价和黄金走势，并说市场在等待 Fed 会议纪要寻找未来利率线索。它不是“Fed 已经作出政策行动”。因此：

1. 保留这条来源记录有审计价值；
2. 不把它当作货币政策事实证据是正确的；
3. GOLD 是受影响资产，不是政策行为主体；
4. provider 的 score/grade/signal 不应进入事实判断或 Public 页面。

## 已修复

1. 新增 captured sources 读模型和 `/api/v1/events/{event_id}/sources`。
2. Public 只返回限长标题、摘要、HTTP(S) 链接、时间、修订号和采集收据哈希；不返回 `raw_json`、score、grade 或 signal。
3. 默认主事件流仍只展示 reader-ready；显式“已排除”档案可以解释有保留来源记录的 rejected 事件。
4. UI 分别显示“采集来源记录”与“可引用支持证据”，并新增“原始线索（未核验、非证据）”卡。
5. OpenNews 语义修订会重新排队；最新 revision 的 URL/发布时间进入读取投影，且视图只在旧合同时迁移一次。
6. OpenNews 分类读取标题和 provider summary，但不读取评分或方向信号。
7. 宏观、宏观数据、地缘事件禁止资产标签充当主体；资产只进入 `affected_assets`。
8. 新增只读、幂等、带 SHA-256 的零证据恢复计划工具。
9. SEC 选中文档改为逐文件隔离失败：超大 primary 不再阻断较小 exhibit，但部分成功仍保持 `INCOMPLETE`，不能自动晋级。

## 验证

- 捕获解读、公开 API、UI、修订、配置和迁移定向组合：60 passed。
- 当前完整工作树：946 passed, 5 skipped；另有 15 项 Windows Git Bash 包装器测试因沙箱无法创建 signal pipe（统一 `Win32 error 5`）未能执行，单独复跑结果相同，不是业务断言失败。
- `python -m compileall -q app scripts tests`：通过。
- `git diff --check`：通过。
- 本地专用预览副本迁移至 Ledger Schema 14，`PRAGMA quick_check=ok`；原始只读导出未原地迁移。

本报告未声称代码已部署。生产发布仍需要独立 release、备份、迁移、服务重启与公网验收流程。
