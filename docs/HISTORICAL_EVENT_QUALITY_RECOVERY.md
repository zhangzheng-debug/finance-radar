# 历史事件质量恢复合同

历史数据不得因为新规则上线就被批量改写为“错误”或直接删除。恢复工作分为四个不可合并的阶段：

1. **只读分桶**：冻结当前事件版本、事实哈希与证据指纹，形成互斥且穷尽的库存清单。
2. **补证或重解析**：从原始观察重新构造发现线索、主体—动作—阶段事实与证据关系；不得改变旧正式状态。只有 facts 已保存同一 evidence_id、observation_id、内容哈希和 `event-admission-v3` 指纹，且现行确定性抽取器能从当前证据段落逐字重放同一槽位、公开摘要和收据哈希的非正式记录，才可机器重建缺失的关系行。`event-admission-v1/v2` 只可读取，不能机器恢复。
3. **人工/规则复核**：输出建议结果与冲突；正式状态仍不写入。
4. **单独授权写入**：仅对版本、事实哈希和证据指纹仍一致的记录执行 CAS；保存变更前快照和追加式审计记录。

运行只读计划：

```powershell
python scripts/build_event_quality_recovery_plan.py `
  --ledger D:\FinanceRadarBackups\finance_radar.sqlite3 `
  --output D:\FinanceRadarReviewKits\event-quality-recovery
```

计划目录会额外生成 `authorization_template.json`。先运行默认 dry-run：

```powershell
python scripts/apply_event_quality_recovery.py `
  --ledger D:\FinanceRadarBackups\finance_radar.sqlite3 `
  --plan-dir D:\FinanceRadarReviewKits\event-quality-recovery
```

真正写入前必须：

1. 先按现行迁移流程将目标和备份升级到 Schema 15；执行器不会在旧 Schema 上绕过迁移建表；
2. 使用 SQLite online backup 或项目现行备份流程生成独立备份；禁止只复制主 `.sqlite3` 文件而遗漏 `-wal`，也禁止把目标账本的硬链接伪装成不同路径的备份；
3. 填写模板中的动作级授权、失效时间、备份路径和 SHA-256；模板同时绑定目标账本的解析后绝对路径、文件身份和计划时逻辑快照，授权不可拿去操作第二个克隆账本；
4. 使用独立、全新的审计输出目录；
5. 显式添加 `--apply`：

```powershell
python scripts/apply_event_quality_recovery.py `
  --ledger D:\FinanceRadarData\finance_radar.sqlite3 `
  --plan-dir D:\FinanceRadarReviewKits\event-quality-recovery `
  --authorization D:\FinanceRadarReviewKits\event-quality-recovery\authorization.json `
  --audit-output D:\FinanceRadarReviewKits\event-quality-recovery-audit-001 `
  --apply
```

执行器先对备份运行 `quick_check`、`integrity_check`，再核对全库逻辑转储哈希、全量事件快照和事件数；因此漏拷 WAL 的裸主文件即使能打开也不能冒充完整回滚备份。随后逐条复验 event_version、事件证据指纹、facts/relations/workflow 哈希和安全子集判据，任意漂移会使整批回滚。若原始观察已删除，计划与复验明确返回 `SOURCE_REVISION_DELETED`；若最新 `edit` 不能同时证明内容哈希和证据段落收据仍一致，则返回 `SOURCE_REVISION_CHANGED`。它只插入 `event_evidence_relations` 与 `event_fact_workflow`，不会修改 `canonical_events`、`event_versions`、状态或标签。

关系与 workflow 写入时，执行器在同一个 SQLite `BEGIN IMMEDIATE` 事务中追加 `event_quality_recovery_audit` 的 `DB_COMMITTED` 收据；该表通过触发器禁止更新和删除。因此数据库提交成功后，即使磁盘上的 `apply_result.json` 写入失败，也可以从数据库按 `durable_audit_id` 找回完整结果。审计目录先落 `apply_intent.json`，成功后追加 `apply_result.json`；失败则追加 `apply_error.json`。成功和失败目录都会以独占创建的 `SHA256SUMS.txt` 封存，避免把一个未封口目录误当成完整审计件。

`READER_READY_CURRENT` 不需要恢复。`LEGACY_FORMAL_REVIEW_REQUIRED` 优先级最高，但“优先”不表示可自动降级；它只表示旧正式结论尚未满足当前语义证据合同。所有旧 `verified/rejected` 都明确排除在机器写入范围外，仍需人工批次。`GENERIC_SEC_DISCOVERY` 和 `NON_DECISION_EVIDENCE_ONLY` 应回到发现层，而不是伪装成已发生的正式事件。

机器恢复默认 fail closed：槽位合同过期、证据段落变化、槽位或摘要不能逐字重放、收据/关系指纹不一致、主体只是在同句出现但不是动作主语、动作被否定/拒绝/否认，或官方来源已有删除/无法证明等价的更新修订，任一情况都退出机器安全子集。恢复写入的关系和 workflow 使用现行事件 admission 语义合同；`event-quality-recovery-apply-*` 仅标识这次操作及审计，不得覆盖关系的语义合同。

`recovery_plan.jsonl` 只是冻结输入合同，不是执行授权；授权文件即使标记 approved，也必须再由命令行显式 `--apply` 才会产生写入。
