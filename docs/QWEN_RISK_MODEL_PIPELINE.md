# Qwen 做空风险语义模型流水线

## 固定分工

| 模块 | 回答的问题 | 是否改变证据状态 |
|---|---|---|
| 确定性证据门 | 当前版本是否有可定位 P0/P1 原文、是否冲突或被推翻 | 是，完全由规则计算 |
| Qwen 风险语义模型 | 捕获文本表达的正负面、做空重大性和语义优先级 | 否 |
| DeepSeek API | 在完全没有可引用证据时解释捕获文本，或提供摘要/翻译 | 否 |

Qwen 和 DeepSeek 不能互相提供训练标签。价格、成交量、事件后的新闻和任何旧模型输出都不能进入人类金标或 Qwen 训练输入。

## 720 条人类金标的用途

- 组员只填写 `真人双盲金标_严禁使用AI`，不承担全库 AI 粗审。
- A、B 草稿可以持续接收并统计进度，但 `complete=false` 或六项声明未签署时绝不是正式金标。
- 负责人仅在双人一致或完成仲裁后冻结 `TRAIN / VALIDATION / HUMAN_BLIND`。
- `HUMAN_BLIND` 的正文和标签始终封存；训练和调参只能读取 TRAIN 与 VALIDATION。
- 这批标注只有二元做空重大性，因此模型强弱只输出 `HIGH / LOW / NONE / UNCLEAR`，不伪造无人标过的更多档位。

## 训练数据生成

```powershell
python scripts/prepare_qwen_risk_sft.py `
  --frozen-dataset D:\FinanceRadarGold\human_gold_frozen.jsonl `
  --output-dir D:\FinanceRadarGold\qwen-sft
```

冻结文件必须同目录带有 `human_gold_frozen.jsonl.sha256`。生成器会检查：

1. 双盲冻结标签的三轴一致性；
2. TRAIN 与 VALIDATION 的事件、主体、事件链和内容哈希无交叉；
3. 盲测正文与标签未导出；
4. DeepSeek、旧模型结果与事后价格均未进入训练；
5. 证据状态只参与确定性准入，不作为 Qwen 的学习目标。

## 训练前硬门

```powershell
python scripts/plan_qwen_risk_training.py `
  --manifest D:\FinanceRadarGold\qwen-sft\qwen_risk_sft_manifest.json `
  --output-dir D:\FinanceRadarModels\qwen-risk-v1 `
  --plan-out D:\FinanceRadarModels\qwen-risk-v1-plan.json
```

默认只生成计划，不执行训练。至少需要 160 条训练样本、40 条验证样本和非空盲测集；每个生成文件的 SHA-256、行数和语义合同都必须复验。正式训练另加 `--execute`，并要求本机已有 `swift` 命令。

当前硬件目标是 RTX 4060 Laptop 8GB：Qwen2.5-1.5B-Instruct、NF4 4-bit QLoRA、batch 1、梯度累积 16、最大长度 2048。模型选择或超参数发生变化时必须产生新计划和新模型版本，不能覆盖旧适配器。

## 生产运行原则

- 采集、证据接纳、Qwen 研判、DeepSeek 解释是四条独立队列；任何一条变慢都不能阻塞其他三条。
- 页面先读持久化结果，不在用户打开事件时同步调用模型。
- 没有模型结果时显示“自动语义研判处理中”，但事件与证据仍可立即浏览。
- Qwen 输出必须绑定 `event_id + event_version + input_sha256 + adapter_sha256 + prompt_version`；任一输入版本变化即旧结果失效。
- 只有在正文缺乏决策级证据时，页面才用“若来源表述属实”展示 Qwen 的条件性研判；有当前版本一手证据时才可显示“证据支持下的语义研判”。
- 所有输出只用于情报排序，不触发交易、仓位或外部操作。
