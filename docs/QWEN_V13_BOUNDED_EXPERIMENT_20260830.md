# Qwen v13 有界改进实验（预注册）

## 目的与结论边界

本实验只回答一个问题：在不读取密封盲测、不伪造人工金标的前提下，重新平衡独立 AI 双盲复核得到的 TRAIN 监督，并从未微调的 Qwen2.5-1.5B 基座重新训练，能否通过既定 DEV 门槛。

- 所有监督均标记为 `AI_REVIEW_NOT_HUMAN_GOLD`。
- DEV 已被此前模型和检查点选择使用，只能用于开发选择，不能称为独立盲测。
- 本轮最多执行一次产生候选模型的完整训练；无候选产物、未读取 DEV 的硬件失败可透明记录后重试。训练后评估所有保存检查点，不根据结果继续改提示词、标签或超参数。
- 只有某个检查点通过全部 DEV 门槛，才允许另行申请开启密封盲测。未通过时保持 `NOT_QUALIFIED`。
- 本实验不修改生产模型、账本、事件状态、公开评级或交易能力，也不部署。

## 冻结输入

| 项目 | 冻结值 |
| --- | --- |
| 基础模型 | `D:\FinanceRadarModels\models\Qwen2.5-1.5B-Instruct` |
| 基础模型完整权重 SHA-256 | `dd924a11b4c220f385b51ffa522daea7c9f3d850e31b162bb5661df483c6d3ee`（`model.safetensors`，3,087,467,144 bytes） |
| 初始化方式 | 全新 LoRA；不加载任何旧 adapter |
| TRAIN unique audit | 729 条；SHA-256 `950047ce880068e71b48d30f7b385c87fddec549280c6679c2c52a862e3f6fae` |
| TRAIN trainable unique | 726 条；两条数表噪声和一条来源字段冲突仅从 trainable 隔离 |
| TRAIN effective | 1,125 条；SHA-256 `03969ac0089e7827c0556d1c63c1ac0efc4fd0cc9cec54328e56ab8e73c2298b` |
| TRAIN manifest | SHA-256 `88e923289239f7d132106b191232cbd22ddf08b6c4dd58ecc10fc4fed7af65ba` |
| 质量隔离合同 | `config/qwen_v13_train_quality_exclusions.json`；SHA-256 `9178779d1c302d16b8ca7c35b45d1a950f27aeee94c6db7d10721f54672e9de8` |
| curriculum policy | SHA-256 `c16ad8e4fa5478cee88d7dd970174c26e4167aa76246f6a922a3f2e8d3f3eff6` |
| prompt | 沿用 v11 `core-v1` / `core-axes-v1` 提示合同，不作修改 |
| DEV | 138 条，优先事件 51 条；只作 adaptive DEV selection |
| 密封集 | 本轮不读取 |

TRAIN 审计视图保留全部 729 个唯一样本。A+B 一致样本 560 条，C 覆盖 169 条整行分歧。质量审计仅从 trainable 视图隔离两条被数表支配的来源和一条 `passage` 与 `summary` 严重冲突、而标签只覆盖正面半边的样本；不删除审计记录、不改写 A/B/C 标签。最终 726 个可训练唯一样本经确定性重采样得到 1,125 条有效记录。有效分布为重大 407、非重大 659、不明确 59；极性为负面 435、混合 97、中性 328、正面 212、不明确 53。

A/B 两轴联合一致率为 76.82%（Cohen's kappa 0.693）；重大性一致率 91.50%（kappa 0.826），极性一致率 79.01%（kappa 0.716）。C 的 169 次仲裁中 115 次完整选择 A、47 次完整选择 B、仅 7 次形成新组合，存在明显 A 侧选择倾向。因此 C 只作为 1x 低权重选项仲裁，不能称为第三份独立盲审，也不能据此把监督升级为真人金标。

## 冻结训练协议

- epochs: `2.0`
- learning rate: `2e-5`
- warmup ratio: `0.08`
- weight decay: `0.1`
- gradient accumulation: `16`
- max length: `2048`
- save steps: `20`
- save total limit: `8`
- logging steps: `5`
- scheduler: `cosine`
- max grad norm: `1.0`
- compute dtype: `float16`
- optimizer（原预注册）: `adamw_torch_fused`
- seed/data seed: `42`

训练驱动必须在优化器创建前将所有可训练 LoRA 参数规范化为 FP32；冻结的 4-bit 基座保持量化。训练前先执行 dry-run，校验数据、manifest、策略哈希、GPU、Tokenizer 长度、模型指纹和输出目录隔离。

### 训练前硬件失败与固定恢复方案

在任何 DEV 评估之前，原预注册优化器发生两次 CUDA 显存不足：第一次在 step 1 后，第二次在 step 74；两次均由原子训练驱动删除临时目录，没有发布候选模型。第二次失败前已保存的进度也没有作为初始化输入复用。

第一次恢复运行保留全部 1,130 条训练记录和 `2048` token 上限，仅把优化器改为驱动预先支持的 `paged_adamw_8bit`。它完整跑完第一轮，但在第二轮切换处再次显存不足，证明问题是 8 GiB GPU 在 epoch 边界的峰值，而不是 AdamW 状态或某条超限输入。该运行同样没有发布候选模型，也没有读取 DEV。

随后恢复协议回到原预注册的 `adamw_torch_fused`，把 epochs 固定为 `1.0`、save steps 固定为 `10`；该运行从未经微调的基座重新开始，但在 step 19 的长文本反向传播峰值再次显存不足。驱动仍未发布候选目录，DEV 仍未读取。

在上述纯硬件失败之后，最终硬件恢复协议在任何 DEV 读取前固定为：LoRA rank/alpha/dropout 保持 `8/32/0.05`，target modules 从 `all-linear` 收窄为 `q_proj,k_proj,v_proj,o_proj`，max length 从 `2048` 收紧为 `1536`。TRAIN-only dry-run 已证明全部 1,125 条记录最长 1,344 tokens，因此该上限不截断任何样本；收窄 target modules 用于减少 MLP LoRA 分支的长序列激活峰值。epochs `1.0`、学习率 `2e-5`、seed、数据、提示合同、量化和其余训练参数不变。该变更只依据训练硬件失败和 TRAIN 长度统计，不依据任何 DEV 分数。

## 冻结 DEV 门槛

检查点只有同时满足下列条件才合格：

- rows >= 120
- priority support >= 20
- parse rate = 1.00
- exact payload accuracy >= 0.75
- materiality Macro-F1 >= 0.70
- polarity Macro-F1 >= 0.65
- priority recall >= 0.80
- non-priority false-positive rate <= 0.08

选择器对全部实际生成的检查点一次性运行。若多个检查点同时通过，按冻结选择规则选择；若没有检查点全部通过，结果为 `NO_DEV_CHECKPOINT_QUALIFIED`。

除总体指标外，结果必须单列 weak 与 hardcase-v3 分层、长短文本差异、各主要申报类型和 `UNCLEAR` 预测情况。AI 复核回执当前没有逐行内容哈希、模型、提示词和会话收据，因此本数据只能作为可追踪的 AI 参考监督，不能升级为人工金标。

## v12 对照

v12 的六个检查点均未通过门槛。最佳 exact 为 checkpoint-60 的 `0.6522`；最佳 materiality Macro-F1 为 `0.5321`；最佳 polarity Macro-F1 为 `0.4759`。所有检查点的 priority recall 均低于 `0.80`，且模型完全没有预测 `UNCLEAR`。v13 的目标不是追求训练损失更低，而是纠正类别坍缩与仲裁样本过度加权。

## 结果

最终硬件恢复运行从未微调基座开始，采用 attention-only LoRA、`1536`
token 上限、`1` epoch，共完成 `71` steps。训练完成后才首次读取冻结 DEV，
依次评估全部 8 个实际检查点；密封集保持关闭。

| checkpoint | 合同有效率 | exact | 重大性 Macro-F1 | 极性 Macro-F1 | 优先事件召回 | 非优先误报 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| 20 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| 30 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| 40 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| 50 | 0.1159 | 0.1014 | 0.1393 | 0.0778 | 0.2745 | 0.0230 |
| 60 | 0.5797 | 0.2536 | 0.3232 | 0.2151 | 0.2745 | 0.0230 |
| 70 | **0.7609** | **0.3261** | **0.3628** | **0.2429** | 0.2745 | 0.0230 |
| 71 | 0.7536 | 0.3188 | 0.3602 | 0.2398 | 0.2745 | 0.0230 |

严格选择结果为 `NO_DEV_CHECKPOINT_QUALIFIED`，通过检查点为 `0`，没有选择
候选，也没有打开密封集。严格选择文件 SHA-256 为
`4ae5edea12f4b5c48696ff226f2257566aa08a44b7dade2813826212f9fa35de`；
weak / hardcase-v3 分层诊断 SHA-256 为
`e0d4ca69a66ad50132c7592a6b534ba38c5cc2e5803427391e14684cb3d1bb67`。

checkpoint-70 虽然是本轮相对最好点，但仍有约四分之一输出未通过完整合同，
两轴 F1 与优先事件召回也远低于冻结门槛。该实验结论为开发失败，不得发布、
不得称为合格风险评级模型，也不得据此打开密封集。
