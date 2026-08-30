# Qwen v15 新 DEV / 3B 一次性实验（训练前登记）

## 目的和边界

v14 在已经使用过的 138 条 DEV 上没有任何 checkpoint 通过全部门槛，因此
固定为开发失败。v15 不再使用该 DEV 调参，也不读取 500 条严格外部测试或任何
密封标签。本轮只回答：在同一套已审计 TRAIN 监督上，把基座从 1.5B 提升到
Qwen2.5-3B，能否在一套此前未参与训练或评估的 DeepSeek 多视角 DEV 上通过
预注册门槛。

所有训练和 DEV 标签均标记为 `AI_REVIEW_NOT_HUMAN_GOLD`；本实验不能证明
真人准确率，也不会修改生产模型、账本、页面或服务器。

## 冻结 DEV

- 来源：2026-08-29 已完成的 `deepseek-v4-flash` 隔离双视角 + 仲裁结果；
- 成员资格先于标签复制决定；只保留 `status=completed`，剔除
  `EVENT_LOCAL_FALLBACK`、事后监督文本，以及与旧 TRAIN/DEV、owner720、硬边界
  开发集和严格外部测试在 sample、entity、event-chain 或内容哈希上的重叠；
- 两个预冻结组件按无标签过滤的集合并集组成，共 225 条；
- 数据 SHA-256：
  `0805ddbb477f1cf9ca338a7850d5c312abcb5b49d575ae663367eb9689da5664`；
- 成员 SHA-256：
  `62419478204cb9c9a81eaac61111378ea01a717b151ed79c7cd36769b974cd19`；
- 标签分布仅用于样本量审计：重大不利 38 / 非重大不利 183 / 不清楚 4；
  极性不利 48 / 混合 1 / 中性 141 / 正面 31 / 不清楚 4。

完成训练前不得运行任何 Qwen 候选读取该 DEV。外部 DeepSeek 凭据当前对新请求
返回 HTTP 402，因此本轮不声称补做了新的外部调用；这里只复用已经成功并有输入
哈希绑定的结果。

## 固定 TRAIN 和基座

- TRAIN unique audit：729 条，SHA-256
  `0c7eed402ccdf266277ab06d197dc7a2502e3d214a702ef9f38b22f0d9f799f0`；
- TRAIN effective：1,122 条，SHA-256
  `b29eb0f5363c85609c9a9bec76252e1ccb53a1ae102200c608e59c4a77065045`；
- TRAIN manifest SHA-256：
  `d331f39e0edbc6420700475c062eb634d7b7ea54497956452a7860089a13ab7b`；
- 基座：官方 `Qwen/Qwen2.5-3B-Instruct`，revision
  `aa8e72537993ba99e69dfaafa59ed015b17504d1`；
- 本地路径：`D:\FinanceRadarModels\models\Qwen2.5-3B-Instruct`；
- 输出继续使用 `qwen-core-axes-prompt-v11` / `core-axes-v1`。

## 一次性训练协议

只从未微调 3B 基座运行一个固定阶段，不读取 DEV：

- QLoRA 4-bit NF4 + double quant；
- all-linear LoRA，rank / alpha / dropout = `8 / 32 / 0.05`；
- 1 epoch，learning rate `2e-5`，warmup `0.08`，cosine scheduler；
- `paged_adamw_8bit`，weight decay `0.1`，max grad norm `1.0`；
- batch size `1`，gradient accumulation `16`；
- max length `1280`；
- seed / data seed `42`；每 10 steps 保存，最多保留 8 个 checkpoint。

若 8 GiB GPU 在未读取 DEV 前发生纯硬件失败，只允许降低与标签无关的运行内存
参数（例如 activation checkpointing/offload 或 max length），并必须先证明不会截断
completion；不得依据 DEV 改标签、提示词、采样或类别权重。

## DEV 首次读取前的硬件结果与固定后备方案

上述 Qwen 3B 方案在 **未读取新 DEV** 的前提下完成两次硬件试跑：

1. `max_length=1280`、all-linear LoRA 在 8 GiB GPU 的第 3 个优化步骤发生 OOM；
2. 仅保留 q/v LoRA、`max_length=1024`，并排除 17 条超过 1024 token 的纯硬件
   长样本后，首个优化步骤运行超过 14 分钟仍未完成。该进程被精确停止，未产生
   checkpoint，也未读取 DEV。

因此 3B QLoRA 在当前机器上记为 `LOCAL_HARDWARE_THROUGHPUT_BLOCKED`，而不是
模型质量失败。为在截止时间前得到一个可复算的候选，本轮在首次读取 DEV 前冻结
如下 CPU 后备方案。它复用同一份 AI 审核监督，但不是 Qwen：

- 训练输入：通过 `training_eligibility=true` 的 unique overlay，文件 SHA-256
  `71bf21ff36dbad8cff7e83bfe3302d59ecc378ca230aae11b42944380ac4446c`；
- 表征：word TF-IDF `1-2 gram / min_df=2 / max_features=35000` 与 char-wb
  TF-IDF `3-5 gram / min_df=2 / max_features=30000` 的固定并集；
- 两个相互独立的目标：重大性与极性；各使用显式 `OneVsRestClassifier` 包装
  `liblinear`
  LogisticRegression，`C=2.0`、`class_weight=balanced`、`max_iter=2000`；
  正则使用 scikit-learn 1.8 默认的 L2 等价配置；
- TRAIN 内部审计：按 entity/event-chain 连通分组的 5 折 OOF；分层目标固定为
  重大性，不让稀有的“重大性×极性”组合破坏分组；
- 随后用全部合格 TRAIN 拟合，**只允许一次**读取 225 条新 DEV；门槛沿用下节，
  不因结果修改特征、分类器、阈值或标签。

该后备候选即使通过，也只能成为 `shadow` 候选；不得打开密封测试、替换生产模型、
改事件状态或触发交易。

首次执行时，进程在第一折分类器拟合阶段因 scikit-learn 1.8 已移除
`liblinear` 的隐式多分类支持而终止。DEV 文件已被程序载入，但没有产生预测、指标、
artifact 或输出目录，操作者没有看到任何验证表现。随后只把预登记的一对多策略改为
库所要求的显式 `OneVsRestClassifier` 包装；数据、特征、超参数、门槛和停止规则均未
改变。该兼容重试不用于依据 DEV 调参，并在结果中保留此异常记录。

## 评估和停止规则

训练原子完成后，才首次对 225 条新 DEV 评估全部实际 checkpoint。门槛保持：

- 合同有效率 `1.00`；
- exact `>= 0.75`；
- 重大性 Macro-F1 `>= 0.70`；
- 极性 Macro-F1 `>= 0.65`；
- 优先事件召回 `>= 0.80`；
- 非优先误报 `<= 0.08`；
- DEV 总样本 `>= 200`、优先事件 `>= 30`、非优先事件 `>= 100`。

只有一个 checkpoint 同时通过全部门槛时，才可以另行讨论打开密封测试。若无一
通过，记录 `NO_DEV_CHECKPOINT_QUALIFIED` 并停止；不得继续使用本 DEV 调参。

## 结果

### Qwen 3B

Qwen 3B 本地训练被当前 8 GiB 硬件吞吐阻塞，状态为
`LOCAL_HARDWARE_THROUGHPUT_BLOCKED`。这不是准确率结论，也没有产生可评估的
3B adapter。

### CPU 后备候选

固定候选在 723 条合格 TRAIN 上完成分组 5 折 OOF，并在兼容重试后一次性评估
225 条新 DEV。TRAIN/DEV 在 sample、event、entity、event-chain 和内容哈希上均为
零重叠；sealed benchmark 没有读取。

| 指标 | TRAIN 分组 OOF | 新 DEV | 门槛 | DEV 结果 |
| --- | ---: | ---: | ---: | --- |
| 完整双轴一致率 | 0.6888 | 0.4089 | >= 0.75 | 未通过 |
| 重大性 Macro-F1 | 0.7655 | 0.4911 | >= 0.70 | 未通过 |
| 极性 Macro-F1 | 0.6304 | 0.2909 | >= 0.65 | 未通过 |
| 重大不利召回 | 0.7981 | 0.5000 | >= 0.80 | 未通过 |
| 非重大不利误报率 | 0.0510 | 0.0481 | <= 0.08 | 通过 |

严格裁决为 `REJECTED_ON_FRESH_DEV`。不得把该 artifact 接入影子流量，更不能替换
生产模型。失败不是解析问题：新 DEV 中 38 条重大不利只识别 19 条；极性侧把 141
条中性中的 56 条误判为正面，说明旧 TRAIN 的监督口径/先验不能稳定迁移到新的
DeepSeek 隔离审核分布。错误预测平均置信度仍不低，不能靠隐藏低置信结果解决。

旧报告中的“95%+”属于三分类风险路由器的 `RISK_REVIEW / NON_TARGET / ABSTAIN`
兼容评估，不是本轮 materiality/polarity 双轴准确率，也不能用来宣称 Qwen 已经达到
95%。

### 可复核产物

产物只保留在仓库外：
`D:\FinanceRadarModels\experiments\semantic-axes-router-v15-tfidf-fresh-dev-20260830-v1`。

- `report.json` SHA-256：
  `35a33bc12652e102e8cffebfe17ce207a46a426ca843c3e5f5172035edde3172`；
- `dev_predictions.jsonl` SHA-256：
  `1d045eea42fb8b195b0323d0c4bda6df4a2740eb8d9903d55db96b7c994d748f`；
- `model_card.json` SHA-256：
  `434c91b3d68ed75e96a054dae0a924993c877214b944611fb4988a18368fcd2c`；
- 失败候选 artifact SHA-256：
  `45d1fe619ea8b99d4ebb06b65d131470fe73bddb5253c2487e9c999be72637c9`。

独立代码审计确认上述逐条预测、指标和 artifact 均可复算，但也发现原 runner 只绑定
数据文件 SHA，没有把 TRAIN/DEV manifest、运行环境和统一输出收据写入 artifact。
因此该目录的正式审计状态是：
`REJECTED_ON_AI_REVIEW_DEV / POST_HOC_SPEC_NOT_PROVEN`，只能作为受限失败快照，
不能事后宣称为机器证明的 canonical fresh experiment。

runner 随后已 fail-closed：强制验证 TRAIN/DEV manifest SHA、角色、隔离标志、逐行
prompt/target/provenance/payload 合同和 metadata/assistant 目标一致性；CV 分组增加
event/content 绑定；模型写入 input/runtime/gate 合同，并生成包含四个输出文件哈希的
`output_manifest.json`。这些加固不改变上述失败指标，也不会恢复 DEV 的“未使用”状态。

### 停止与下一轮

本 DEV 已消费，不再用于改特征、类别权重、阈值或规则后重新宣称通过。下一轮若要
继续，必须先把这 225 条转为明确的 TRAIN-only 资料，统一重大性/极性规则与成对
反例，再冻结一批从未用于训练或诊断的新实体/事件链盲集。没有新独立盲集之前，
任何再训练结果只能叫开发候选，不能叫验证通过。
