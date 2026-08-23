# 采集来源与证据恢复合同

## 1. 两种材料必须分开

Finance Radar 同时保存两层材料：

- **采集到的来源记录**：API、RSS、公告索引或聚合源当时返回的标题、摘要、链接、时间与不可变哈希。它只证明“系统收到了什么”。
- **可引用支持证据**：能够定位到主体、具体动作、阶段和日期的 P0/P1 原始段落，并绑定当前事件版本与关系收据。它才可能支持正式事实结论。

`0 条可引用支持证据` 不等于 API 输入为空。Public 事件流可以显示经过限长和脱敏的来源记录，但必须同时标明其证据姿态；不得把来源捕获摘要写成正式事实，也不得公开聚合源的评分、等级、多空信号或完整 `raw_json`。

## 2. Public 行为

- 所有 canonical 事件进入同一个可浏览事件流；`canonical.status`、`public_state`、粗审、轻核、双盲人审和 `citation_ready` 都不得充当可见性门。
- `citation_ready` 是当前版本的自动派生属性，只控制一段话能否作为正式事实引用；它不控制事件是否出现。
- 页面分别显示 `evidence_posture` 和 `risk_assessment`。前者只回答证据强弱，后者只回答下行风险复核优先级，二者不得互相写入。
- 初始证据姿态为 `PRIMARY_SUPPORTED`、`PRIMARY_SOURCE_AVAILABLE`、`SOURCE_CAPTURED`、`NO_SOURCE`；具体缺口由 `evidence_gap_codes` 说明。
- “待核验”“粗审”“待补证”“已核验”等词只描述内部 Reviewer/Operator 工作进度，不作为 Public 主徽标、主筛选或事件事实。
- 页面分别显示“采集来源记录 N”与“可引用支持证据 M”；来源为空、解析失败和证据关系缺失不得混为一类。
- 被标记为 `filtered_aggregated_noise` 的边可以用于解释捕获历史，但不会进入事实摘要、风险路由、行情特征或正式结论。
- 采集来源修订、删除和重新抓取不会改写最初接收时间；`recovered_at` 与原始 `known_at` 必须分离。

## 3. OpenNews 修订和主体规则

- 语义内容哈希变化会把已有 observation job 重新置为 `PENDING`，使更新后的标题/摘要重新经过当前分类与主体门。
- 最新 OpenNews revision 中的 URL 和发布时间进入读取投影；首次采集记录仍保持不可变。
- 分类可以读取 provider 提供的 `summary_zh/summary_en`，但不得读取 `score/grade/signal` 形成事件判断。
- 宏观、宏观数据和地缘事件必须有央行、政府、机构、国家或组织等行为主体。GOLD、BTC、指数和商品代码只能作为 `affected_assets`，不能代替 `claim_subject`。
- “市场等待会议纪要”“寻找利率线索”等预期或评论不能晋级为央行已经采取行动。

## 4. 只读恢复清单

在生产账本的只读快照上运行：

```powershell
python scripts/build_source_observation_recovery.py `
  --ledger D:\FinanceRadarReviewKits\production-ledger.sqlite3 `
  --output D:\FinanceRadarReviewKits\source-observation-recovery
```

工具不联网、不创建 `event_evidence`、不改变 canonical 状态。它按互斥分桶输出：

- `SEC_OVERSIZE_REFETCH_READY`
- `OFFICIAL_REFETCH_READY`
- `P2_CAPTURE_ONLY`
- `NO_URL_RAW_ONLY`
- `SOURCE_DELETED`
- `NO_CAPTURE`
- `ORPHAN_CAPTURE_REBUILD_DISCOVERY`

输出含 `manifest.json`、`recovery_records.jsonl`、`README.md` 和 `SHA256SUMS.txt`。只有另行取得可定位 P0/P1 段落并通过主体、动作、日期、版本和来源修订检查后，才能进入证据表。

## 5. SEC 大文件

5 MB 上限仍是单文档安全边界，不通过简单无限增大内存解除。若 primary 10-Q/10-K 超限，采集器继续尝试选中的较小 EX-99、EX-10 等文件，并记录逐文档失败。只要任一选中文档缺失，manifest 就保持 `INCOMPLETE_FETCH_ERROR`，材料可以保留用于后续复核，但不得自动晋级事件。

## 6. GOLD 验收样例

类似“地缘紧张推动避险、黄金上涨、市场等待 Fed 会议纪要”的记录应满足：

1. 保留 provider 标题、摘要、链接、发布时间、接收时间和采集收据哈希；
2. 在主事件流公开显示，`evidence_posture=SOURCE_CAPTURED`，并使用“仅捕获来源，不是正式事实”等明确措辞；
3. `citable_evidence_count=0`，canonical rejected 状态不变；
4. GOLD 只作为受影响资产，不作为宏观政策行为主体；
5. 不把 provider 的 `long`、分数或等级送入事实判断、模型或页面。

## 7. API 捕获内容解读

Public 可以在 `SOURCE_CAPTURED` 的来源卡中显示一段独立解读，帮助用户理解英文或压缩后的 API 文本，并明确标注“仅来源捕获，不是正式事实”。该解读与证据和正式事实严格分层：

- 无缓存时提供零费用确定性预览；已配置的后台任务可以生成收据绑定的 DeepSeek 辅助解读，但 Public 浏览本身绝不触发外部调用；
- 普通浏览不会触发外部调用或账单；
- 模型输入必须经过白名单和限长，且来源文本一律视为不可信输入；
- 所有逐字引用必须能在当前捕获文本中找到；
- 结果绑定当前来源 revision、内容哈希、采集收据和提示词哈希；
- 解读不得改变 canonical、不得进入模型特征、不得影响价格判断或交易。

完整合同见 `docs/CAPTURE_INTERPRETATION_CONTRACT.md`。
