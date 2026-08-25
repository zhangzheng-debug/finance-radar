# 720 条真人金标：部分进度接收规范

## 固定范围

组员只处理批次 `HGR-20260820-720` 的真人双盲金标部分。AI 普查、历史事件清洗、生产事件修改、模型训练、服务器与交易均不属于组员任务。

两名审核员面对同一批 720 条事件，但匿名 token 和顺序不同。只有负责人持有的 `owner_manifest.json` 能把 A/B 答案对齐到同一底层样本。负责人材料不得提交到仓库或发给审核员。

## 为什么增加 progress 命令

审核员可以阶段性导出草稿。草稿中未填写的行仍带有导出时间，所以不能用 `reviewed_at` 判断完成；完成的权威条件是三轴和理由均已填写。

`progress` 命令允许负责人反复接收 A/B 的多个快照，并执行以下检查：

- 绑定原始 assignment 哈希、批次、槽位和匿名审核员凭据；
- 拒绝未知 token、重复 token、半填三轴、非法枚举、短理由和额外字段；
- 用私密 token map 对齐 A/B 的底层样本；
- 统计单人覆盖、双人覆盖、三轴完全一致和待仲裁冲突；
- 分别报告 A/B 剩余量、双人共同覆盖量和双审剩余量；A/B 合并的单边
  覆盖数绝不算金标完成数；
- 记录同一审核员在后续快照中改动答案的修订痕迹；
- 保持 `split=UNASSIGNED`，不派生目标标签，不改变模型、生产事件或交易状态。

示例：

```powershell
python scripts/human_gold_review_kit.py progress `
  --owner-manifest "D:\private\owner_manifest.json" `
  --submission-a "D:\returns\A_阶段1.json" `
  --submission-a "D:\returns\A_阶段2.json" `
  --submission-b "D:\returns\B_阶段1.json" `
  --output "D:\private\human_gold_progress.json"
```

`--submission-a` 和 `--submission-b` 均可重复。快照按 `exported_at` 顺序叠加，新的已填写答案覆盖旧答案，任何内容变化会写入 `revision_audit`。

## 草稿不等于金标

草稿允许 `complete=false` 和六项真人声明为 `false`，但输出必定满足：

- `provisional_only=true`；
- `target_label_derived=false`；
- `freeze_required_before_training_or_blind_evaluation=true`；
- `gold_eligible=false`，直到 A/B 都提交完整且通过严格声明校验、冲突完成第三人仲裁、再经过既有冻结流程。

正式回传仍使用原命令：

```powershell
python scripts/human_gold_review_kit.py validate --assignment assignment_A.json --submission A.gold-review.json
python scripts/human_gold_review_kit.py validate --assignment assignment_B.json --submission B.gold-review.json
```

不得把阶段性一致的答案直接送入训练，也不得用模型或价格结果补齐未完成人工标签。

## GitHub 回传边界

公开仓库分支并不是理想的双盲回传通道：在双方完成前公开答案会增加交叉污染风险。现有公开草稿只作为可恢复的进度快照，最终导出仍必须勾选真人独立、未使用 AI、未查看同伴答案、未查看旧标签、模型输出和事后行情等声明。负责人应保留原始提交哈希和接收时间；如无法证明独立性，该批只能降级为开发用银标，不能进入密封盲测。
