# 真人双盲金标到模型的交接合同

组员回传后按既有 `validate → merge → conflict arbitration → freeze` 流程形成带 SHA-256 侧车的 frozen JSONL。不得把未完成冲突、AI 审核结果或价格数据塞进真人金标。

冻结成功后运行：

```powershell
python scripts/prepare_human_gold_router_inputs.py `
  --frozen-dataset D:\FinanceRadarReviewKits\human-gold-frozen.jsonl `
  --output-dir D:\FinanceRadarReviewKits\router-inputs
```

输出分三层：

- `human_gold_router_development.jsonl`：仅 TRAIN/VALIDATION 且仅 RISK_REVIEW/NON_TARGET；
- `human_gold_abstain_gate.jsonl`：仅 TRAIN/VALIDATION 的 ABSTAIN，用于验证证据闸门；
- `human_gold_blind_manifest.jsonl`：只含盲测 sample/event/text/content 哈希，不含标签与正文。

脚本逐条检查标签与重大性、极性、证据状态的一致性，拒绝在人工审核时包含模型输出或事后价格的数据，并给每个输出生成内容哈希。它不会训练、读取 HUMAN_BLIND 标签、晋级模型、改写事件或部署服务器。

开发集满足数量门后，可以生成一个**未发布、SHADOW-only** 候选：

```powershell
python scripts/train_risk_router_human_gold.py `
  --development D:\FinanceRadarReviewKits\router-inputs\human_gold_router_development.jsonl `
  --artifact D:\FinanceRadarReviewKits\router-inputs\risk_router_human_gold_candidate.joblib `
  --report D:\FinanceRadarReviewKits\router-inputs\development_report.json `
  --card D:\FinanceRadarReviewKits\router-inputs\model_card.json
```

训练器拒绝 HUMAN_BLIND、ABSTAIN、AI-rubric、事后价格、审核时模型输出以及 TRAIN/VALIDATION 的 issuer/event-chain/text 重叠。它只用 validation 选阈值；通过开发门也仅表示可以进入密封盲测，不表示可以晋级生产。

当前正式模型仍是 AI-rubric 历史基线；在真人回传、冻结和独立盲测完成前，不能把它改称“真人训练模型”。
