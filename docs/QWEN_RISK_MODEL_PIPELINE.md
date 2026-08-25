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

仲裁完成后先冻结 420 / 120 / 180。冻结器从来源元数据确定完整来源族留出，
同时保持其余时间核心的先后顺序；它不读取模型输出或标签来挑选留出来源：

```powershell
python scripts/freeze_offline_human_gold.py `
  --annotations D:\FinanceRadarGold\human_gold_annotations_unassigned.jsonl `
  --report D:\FinanceRadarGold\human_gold_freeze_readiness.json `
  --dataset D:\FinanceRadarGold\human_gold_frozen.jsonl
```

冻结报告会记录 `source_holdout_policy.selection_basis=SOURCE_METADATA_ONLY_PRE_LABELS`、
实际留出来源族、行数与各来源分布。若负责人已经在审核完成前声明来源族，可另加
`--holdout-source-family` 固定它；不得在看到标签、价格或模型结果后改选。
类别最低数只是自然分布的可用性底线，不是强行配平目标：TRAIN 至少
40 / 100 / 20，VALIDATION 至少 10 / 20 / 5，HUMAN_BLIND 至少
20 / 30 / 5（顺序均为 RISK_REVIEW / NON_TARGET / ABSTAIN）。冻结器不会
因为标签而移动、删除或重写任何事件。

```powershell
python scripts/prepare_qwen_risk_sft.py `
  --frozen-dataset D:\FinanceRadarGold\human_gold_frozen.jsonl `
  --output-dir D:\FinanceRadarGold\qwen-sft
```

准备器同时保留一份每个 TRAIN 样本只出现一次的审计文件，并生成实际训练使用的
`qwen_risk_sft_train_balanced.jsonl`。只有人工语义标签推导出的
`PRIORITY_REVIEW` 可以在 TRAIN 内重复：目标占比 25%，每条最多出现 4 次，全部
重复实例都绑定原始 `sample_id` 与 repeat index。VALIDATION 和 HUMAN_BLIND
绝不重复；最终指标仍在自然分布上计算，因此训练重采样不能抬高验收分数。

冻结文件必须同目录带有 `human_gold_frozen.jsonl.sha256`。生成器会检查：

1. 双盲冻结标签的三轴一致性；
2. TRAIN 与 VALIDATION 的事件、主体、事件链和内容哈希无交叉；
3. 盲测正文与标签未导出；
4. DeepSeek、旧模型结果与事后价格均未进入训练；
5. 证据状态只进入隔离的审计清单，不出现在 Qwen 的消息、输入或目标中；
6. `DISCOVERY_ONLY / INSUFFICIENT` 等来源文本仍参与极性与重大性训练，避免模型在最需要条件性研判的样本上形成分布盲区。

## 训练前硬门

```powershell
python scripts/plan_qwen_risk_training.py `
  --manifest D:\FinanceRadarGold\qwen-sft\qwen_risk_sft_manifest.json `
  --output-dir D:\FinanceRadarModels\qwen-risk-v1 `
  --plan-out D:\FinanceRadarModels\qwen-risk-v1-plan.json
```

默认只生成计划，不执行训练。至少需要 160 条训练样本、40 条验证样本和非空盲测集；每个生成文件的 SHA-256、行数和语义合同都必须复验。正式训练另加 `--execute`，并要求本机已有 `swift` 命令。

当前硬件目标是 RTX 4060 Laptop 8GB：Qwen2.5-1.5B-Instruct、NF4 4-bit QLoRA、batch 1、梯度累积 16、最大长度 2048。模型选择或超参数发生变化时必须产生新计划和新模型版本，不能覆盖旧适配器。

Windows 本机环境统一放在 D 盘，避免继续占用 C 盘：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap_qwen_training_windows.ps1 -Install
```

该脚本固定使用 `D:\FinanceRadarModels` 存放虚拟环境、pip 临时目录和 Hugging Face 缓存，并只做 CUDA/ms-swift/bitsandbytes 安装与探针，不下载基础模型、不读取盲测标签、不开始训练。去掉 `-Install` 可随时复跑只读探针。

## 一次性盲测与运行包

训练、验证集调参和适配器冻结完成后，才允许对 `HUMAN_BLIND` 做一次评估：

```powershell
python scripts/evaluate_qwen_risk_blind.py `
  --frozen-dataset D:\FinanceRadarGold\human_gold_frozen.jsonl `
  --sft-manifest D:\FinanceRadarGold\qwen-sft\qwen_risk_sft_manifest.json `
  --adapter-model D:\FinanceRadarModels\qwen-risk-v1\adapter_model.safetensors `
  --model-url http://127.0.0.1:18602 `
  --model-name qwen-risk-candidate `
  --output-dir D:\FinanceRadarModels\qwen-risk-v1-blind
```

命令在调用第一条盲样本前先写入 `BLIND_CONSUMED.json`，输出目录存在即拒绝重跑。
默认要求至少 120 条全部成功、其中 `PRIORITY_REVIEW` 至少 20 条、重大性
macro-F1 ≥ 0.65、极性 macro-F1 ≥ 0.55、重大负面召回率 ≥ 0.75。支持数不足
也会永久记为 `FAIL`，不能靠一个极小正例集取得偶然高召回。失败后不能根据这
180 条继续调参再重测；它们已经失去盲测资格。

只有盲测 `PASS` 且模型已合并并转换成固定文件名 `finance-radar-qwen-risk-v1.gguf`，才能生成生产运行清单：

```powershell
python scripts/build_qwen_risk_runtime_manifest.py `
  --gguf D:\FinanceRadarModels\runtime\finance-radar-qwen-risk-v1.gguf `
  --adapter-model D:\FinanceRadarModels\qwen-risk-v1\adapter_model.safetensors `
  --blind-receipt D:\FinanceRadarModels\qwen-risk-v1-blind\qwen_risk_blind_receipt.json `
  --sft-manifest D:\FinanceRadarGold\qwen-sft\qwen_risk_sft_manifest.json `
  --output D:\FinanceRadarModels\runtime\model-manifest.json
```

清单把 GGUF、适配器、冻结数据、SFT 清单和一次性盲测收据的 SHA-256 串成同一条链。任一不一致，服务器上的 `ExecStartPre` 会拒绝启动模型。

## 生产运行原则

- 采集、证据接纳、Qwen 研判、DeepSeek 解释是四条独立队列；任何一条变慢都不能阻塞其他三条。
- 页面先读持久化结果，不在用户打开事件时同步调用模型。
- 没有模型结果时显示“自动语义研判处理中”，但事件与证据仍可立即浏览。
- Qwen 输出必须绑定 `event_id + event_version + input_sha256 + adapter_sha256 + prompt_version`；任一输入版本变化即旧结果失效。
- 只有在正文缺乏决策级证据时，页面才用“若来源表述属实”展示 Qwen 的条件性研判；有当前版本一手证据时才可显示“证据支持下的语义研判”。
- 所有输出只用于情报排序，不触发交易、仓位或外部操作。
- 生产安装会放置 Qwen 单元，但默认保持禁用；只有完整运行包、环境文件和盲测通过收据都存在时才可显式启用。
