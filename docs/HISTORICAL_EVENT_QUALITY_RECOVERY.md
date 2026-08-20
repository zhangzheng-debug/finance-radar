# 历史事件质量恢复合同

历史数据不得因为新规则上线就被批量改写为“错误”或直接删除。恢复工作分为四个不可合并的阶段：

1. **只读分桶**：冻结当前事件版本、事实哈希与证据指纹，形成互斥且穷尽的库存清单。
2. **补证或重解析**：从原始观察重新构造发现线索、主体—动作—阶段事实与证据关系；不得改变旧正式状态。
3. **人工/规则复核**：输出建议结果与冲突；正式状态仍不写入。
4. **单独授权写入**：仅对版本、事实哈希和证据指纹仍一致的记录执行 CAS；保存变更前快照和追加式审计记录。

运行只读计划：

```powershell
python scripts/build_event_quality_recovery_plan.py `
  --ledger D:\FinanceRadarBackups\finance_radar.sqlite3 `
  --output D:\FinanceRadarReviewKits\event-quality-recovery
```

`READER_READY_CURRENT` 不需要恢复。`LEGACY_FORMAL_REVIEW_REQUIRED` 优先级最高，但“优先”不表示可自动降级；它只表示旧正式结论尚未满足当前语义证据合同。`GENERIC_SEC_DISCOVERY` 和 `NON_DECISION_EVIDENCE_ONLY` 应回到发现层，而不是伪装成已发生的正式事件。

该工具仅有读取和导出能力，没有 apply 子命令。生成的 `recovery_plan.jsonl` 是未来工作的输入合同，不是执行授权。
