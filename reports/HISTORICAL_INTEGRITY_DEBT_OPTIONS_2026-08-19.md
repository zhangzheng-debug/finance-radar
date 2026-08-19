# 历史事实完整性债务：纠正后的处置选项

- 日期：2026-08-19
- 生产基线：2026-08-18 的只读历史审计，原报告 SHA-256
  `ac75b5715b99c46dddcc4ee8a66848c8da70166e768141e1e30d02941ff0078f`
- 新审计合同：`fact-integrity-history-audit-v2`
- 性质：**决策支持文件和复跑说明，不是处置授权**

## 先纠正旧报告的两个口径错误

1. `EDGE_REJECTED_BY_CURRENT_RELEVANCE_GATE = 6,499` 的分类单位是 **Agent decision**，
   不是证据边。它表示 6,499 个决策各自至少有一条旧边被当前相关性门拒绝。
   被拒边的真实总数必须读取 v2 报告的 `rejected_edge_total`，不能用 6,499 代替。
2. `CURRENT_GATE_REQUIRES_REVIEW = 2,729` 是互斥的记录分类，不等于“2,729 条全部被
   当前语义门拒绝”。v2 会把原因拆为 `CURRENT_GATE_NOT_SUPPORTED`、
   `CONTRACT_VERSION_MISMATCH` 和 `EVENT_EVOLVED_AFTER_FORMALIZATION`；同一记录可有多个原因。

因此，2026-08-18 生产快照能安全表达的是：

| 分类 | 单位 | 生产快照 | 已知边界 |
|---|---|---:|---|
| `EDGE_REJECTED_BY_CURRENT_RELEVANCE_GATE` | Agent decision | 6,499 | 至少一条旧边被拒；边总数待 v2 复跑 |
| `STALE_CONTRACT_REQUIRES_RERUN` | Agent decision | 4,275 | 没有先落入 rejected 分类的旧合同决策 |
| 两类 Agent decision 合计 | Agent decision | 10,774 | 与恢复账本中的决策总数对齐，不可再称为边数 |
| `CURRENT_GATE_REQUIRES_REVIEW` | 轻量正式化记录 | 2,729 | 原因待 v2 拆分，不能先宣称全部失败 |
| `LEGACY_REVIEW_CONFIG_UNPROVEN_PROVENANCE` | 旧配置行 | 105 | 本地核验快照中 105/105 当前为 canonical `verified`；生产动作前须重跑 |

这些类别的单位和权限面不同，不能相加成一个“约一万三千条事实错误”的数字。

## v2 只读清单现在能证明什么

`python scripts/audit_fact_integrity_history.py` 只以 SQLite `mode=ro` 打开两库，不修改
canonical、证据、Agent 历史或轻量核验历史。相邻 JSON 报告新增三个精确清单：

- `affected_manifests.agent_decisions`：决策 ID、事件 ID、分类和被拒边数；
- `affected_manifests.light_formalizations`：事件、正式化版本、当前版本、分类和复核原因；
- `affected_manifests.legacy_unproven_canonical_verified`：来源不可证且当前仍为 `verified`
  的事件、版本与问题列表。

生产数据更新后先复跑并保存报告 SHA-256，再讨论任何写动作。旧 v1 数字只能作为带日期
的历史基线，不能直接生成批量写入范围。

本分支已对工作区本地数据库做一次 v2 只读预演，报告 SHA-256 为
`a2aedaf67487373e3dcabb67419f131e053cf78720fc1fa8d1f10ceb24a5becf`：本地 operations／
轻量历史为空，因此不能替代生产的 A/B/C 清单；旧配置为 105 条，其中 105 条来源不可证且
当前 canonical 均为 `verified`。这既验证了 D 清单生成能力，也明确了生产复跑仍是前置门。

## A · 含被拒旧边的 Agent decisions

旧 `_propose_edges()` 曾可能把零相关段落挂到 claim；当前相关性门要求发行人身份锚与
事件语义锚。这里需要处理的是**咨询式决策历史和证据图**，不是 canonical 事实状态。

| 选项 | 做法 | 后果 |
|---|---|---|
| A1 按活跃需求重跑 | 仅对仍在活跃队列／公开详情中使用的事件按当前合同重建 | 成本有界；未使用历史继续明确标 stale |
| A2 作废旧边 | 保留审计历史，旧边标记被现合同取代 | 不制造伪支撑；相应证据图会诚实变为空或不足 |
| A3 保持可用 | 继续把旧边当当前支撑 | 不建议，违反精确证据与当前相关性门 |

**推荐 A1 + A2。** 先以 v2 决策清单分批，任何重跑失败都保持 `INSUFFICIENT`／
`HUMAN_REVIEW`，不得把失败改写成成功。

## B · 仅因旧合同过期的 Agent decisions

这 4,275 条是在互斥分类中未先命中 rejected-edge 的旧合同决策。它们同样只是 advisory
历史，不应继续显示为当前结论。

**推荐按需重跑。** 活跃队列和用户打开的事件优先；其余保留历史收据并显示过期。
不建议为了追求“债务数字归零”做无界全量模型调用。

## C · 轻量正式化历史

这类记录曾进入 canonical，但 v1 报告没有回答“因合同版本、事件演化，还是当前门不支持”。
在 v2 原因清单生成前，不应直接把 2,729 条全部回退，也不应继续宣称它们全部可靠。

建议顺序：

1. 生产只读复跑，按 `review_reasons` 拆分；
2. 优先审查 `CURRENT_GATE_NOT_SUPPORTED` 且仍影响公开结论的记录；
3. `EVENT_EVOLVED_AFTER_FORMALIZATION` 单独判断后续版本是否已经纠正，不机械回滚；
4. `CONTRACT_VERSION_MISMATCH` 但当前门仍支持的记录可进入低优先级人工确认；
5. 任何 canonical 写入都必须使用限额、过期、逐事件版本与证据指纹绑定的动作授权。

## D · 105 条来源不可证且当前正式展示的旧配置

旧配置缺少 reviewer、reviewed_at、action authorization、事件版本、证据指纹和源对象哈希。
常驻 worker 已失去把它写入 canonical 的权限，但“未来不再写”不会自动消除已经存在的
`verified` 状态。本地核验快照显示 105/105 仍为 canonical `verified`；生产处置前必须用
v2 清单再次精确确认。

**推荐先标记为 legacy-unproven 并排入人工复核，而不是静默删除或假装补齐来源。**
它们不得用于真实人审准确率、训练真值或模型晋级。是否回退 canonical 是单独的破坏性／
正式事实动作，本轮没有授权也没有执行。

## 执行门

1. 在生产两库只读复跑 v2，保存 JSON、Markdown 和 SHA-256；
2. 对每一类分别生成限额批次，不能把 A/B/C/D 打包成“一键清理”；
3. 先审 manifest，再给动作级、过期、最大条数、版本/CAS 绑定授权；
4. 写前备份、写后复跑完整性审计并记录公开计数变化；
5. 任一失败即停，不以降低门禁完成批次。

## 本轮明确未做

未修改任何 canonical 事件、证据边、Agent decision、轻量核验历史或旧配置；未部署 AWS；
未删除远端分支或公共 Release 恢复资产。
