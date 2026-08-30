# Qwen v14 自适应开发实验（训练前登记）

## 结论边界

v13 已读取冻结 DEV，且最终 checkpoint-71 的格式通过率仅为
`0.7536`、联合准确率 `0.3188`、重大性 / 极性 Macro-F1 分别为
`0.3602 / 0.2398`。因此 v13 只能记为开发失败，不能发布，也不能开启
密封测试集。

v14 是基于该 DEV 结果设计的**自适应开发实验**，不是独立盲测。所有监督仍为
`AI_REVIEW_NOT_HUMAN_GOLD`；不把组员文件、AI 仲裁或 DEV 结果表述为真人金标。
本实验不读取密封测试集，不修改生产模型、账本、页面或服务器。

## 固定输入

- 基座：`D:\FinanceRadarModels\models\Qwen2.5-1.5B-Instruct`
- 基座 `model.safetensors` 完整 SHA-256：
  `dd924a11b4c220f385b51ffa522daea7c9f3d850e31b162bb5661df483c6d3ee`
- TRAIN unique audit：729 条，SHA-256
  `0c7eed402ccdf266277ab06d197dc7a2502e3d214a702ef9f38b22f0d9f799f0`
- TRAIN trainable unique：723 条；2 条数表结构隔离、1 条来源字段冲突隔离和
  3 条标签无关的长度硬件隔离均只作用于 trainable 视图
- TRAIN effective：1,122 条，SHA-256
  `b29eb0f5363c85609c9a9bec76252e1ccb53a1ae102200c608e59c4a77065045`
- TRAIN manifest：SHA-256
  `d331f39e0edbc6420700475c062eb634d7b7ea54497956452a7860089a13ab7b`
- v14 质量隔离输入：SHA-256
  `865d133d32307b9a81412bc1d287d0cfd91bdce617c48b6b7827c612d626169e`
- prompt / 输出合同：继续使用 `qwen-core-axes-prompt-v11` / `core-axes-v1`
- DEV：138 条，仅用于自适应开发选择
- 密封集：保持关闭

## 训练协议

v13 的注意力层-only LoRA 明显欠拟合。v14 恢复 Qwen 的全部线性层 LoRA，
并使用 8-bit paged optimizer。首次硬件失败后的标签无关长度审计把最终
上限固定为 `1280`，并证明本轮 trainable 视图不存在截断。

为避免第二轮显存失败抹掉第一轮候选，训练在**不读取 DEV 的情况下**连续执行
两个固定阶段：

1. 阶段 A：从未微调基座新建 all-linear LoRA，1 epoch，学习率 `2e-5`；
2. 阶段 B：直接从阶段 A 的 final adapter 继续 1 epoch，学习率 `1e-5`。

两个阶段均使用：

- LoRA rank / alpha / dropout：`8 / 32 / 0.05`
- target modules：`q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj`
- optimizer：`paged_adamw_8bit`
- warmup ratio：`0.08`
- weight decay：`0.1`
- gradient accumulation：`16`
- max length：`1280`（由下述 TRAIN-only 硬件恢复固定）
- scheduler：`cosine`
- max grad norm：`1.0`
- compute dtype：`float16`
- seed / data seed：`42`
- save steps：`10`

阶段 A 完成后不得先读 DEV 再改变阶段 B。若阶段 B 发生纯硬件失败，则保留并
评估已经原子发布的阶段 A；不得从失败临时目录恢复或据 DEV 改写标签。

### 阶段 A 的 TRAIN-only 硬件恢复

首次阶段 A 在 step 61 遇到 all-linear MLP LoRA 的 CUDA 显存峰值。驱动原子
清理了临时目录，没有发布 checkpoint，也没有在该次训练中读取 DEV。失败前的
训练损失已降至约 `0.08`，因此不缩减 LoRA 容量。

失败后对 TRAIN 做了与标签无关的 token 长度审计：仅 3 个唯一样本超过
`1280` tokens，长度分别为 `1344 / 1321 / 1313`；下一长样本为 `1203`。
固定恢复方案是在审计视图中保留这 3 条，但通过版本化质量合同
`config/qwen_v14_train_quality_exclusions.json` 将其从本轮 trainable 视图隔离，
并把 max length 固定为 `1280`。不查看三条标签决定是否隔离，不截断 completion，
其余超参数与两阶段协议不变。

## 评估和停止规则

训练完成后，对阶段 A、阶段 B 的实际保存检查点统一使用与 v13 相同的冻结 DEV
和严格选择器。合格条件保持不变：parse `1.00`、exact `>=0.75`、重大性
Macro-F1 `>=0.70`、极性 Macro-F1 `>=0.65`、优先事件召回 `>=0.80`、
非优先误报 `<=0.08`，并满足既有样本量门槛。

若没有检查点同时通过，停止训练并记为 `NO_DEV_CHECKPOINT_QUALIFIED`；不再用
同一 DEV 继续试提示词、别名映射或超参数。只有严格通过后，才可另行决定是否
开启密封测试；生产发布仍需独立授权。

## 结果

两个固定训练阶段均已完成，且在阶段 B 启动前没有读取 DEV：

| 阶段 | steps | train loss | training manifest SHA-256 | final adapter fingerprint |
| --- | ---: | ---: | --- | --- |
| A | 71 | 0.347179 | `58ad44861fefbead17bb7219119aa6abbc66527c3a9679ec4d6f4d29d69dae95` | `8524e959db27911cffc82eaffd13f66705c2da91167245b996716ef49cc4185e` |
| B | 71 | 0.067285 | `fa3a0d814c39dce786b049ceb255db057b682557b97155d35c783acb014d1d44` | `57d0c2cac8fd89c690ff01075e750516a8a21e1a8bfa4ebff1cf8a5df1084ba6` |

阶段 B 的训练合同精确绑定阶段 A 的最终 adapter fingerprint；两个 manifest
sidecar 均通过复算，且都记录 `eval_dataset_read=false`、
`sealed_benchmark_read=false`。阶段 A / B 的最终 adapter 权重 SHA-256 分别为
`e195b3e4c054ba306e4488c88feb910679594c766c976f7f9255704db995f564` 与
`485f5f525f762e5907987a63778cd39aca21821e0ba4723100ed5e04fca6be0b`。

### 训练后合同审计

已执行的 A/B 训练继续由历史 v1 质量隔离文件及其 SHA
`865d133d32307b9a81412bc1d287d0cfd91bdce617c48b6b7827c612d626169e`
绑定；该文件没有被事后改写。代码审计指出，v1 对三条硬件长度隔离只保存了
自由文本，不能单靠 manifest 机器复核 token 数、阈值和训练硬件计划。因此该
缺口作为 v14 的已知可复现性限制保留，不能用事后新合同冒充运行时输入。

后续运行已新增 `qwen-train-quality-exclusions-v2` 和
`qwen-train-membership-commitment-v2`：v2 机器绑定原始成员、排除补集、token
测量、`1280` 阈值、原始行、基座权重、Tokenizer、chat template、LoRA 目标层、
硬件计划与审计回执；v1 明确拒绝硬件隔离。对应 v2 输入 SHA-256 为
`413b2a9b1a8a8a89e16e63653cdbb9f6b7d1c140eca19d6bb24c555947f2ca60`。
该 v2 文件只用于后续 fail-closed 运行，不改变本次已完成模型的历史身份。

训练结束后才首次读取冻结 DEV。对两个阶段的全部 16 个实际检查点执行同一
评估器和同一冻结门槛，结果如下：

| 阶段 | checkpoint | 合同有效率 | exact | 重大性 Macro-F1 | 极性 Macro-F1 | 优先事件召回 | 非优先误报 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 10 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 | 0.0000 |
| A | 20 | 0.4203 | 0.2174 | 0.3249 | 0.1547 | 0.4314 | 0.0575 |
| A | 30 | 1.0000 | 0.5072 | 0.4656 | 0.3170 | 0.4314 | 0.0230 |
| A | 40 | 1.0000 | 0.5652 | 0.5181 | 0.3682 | 0.6078 | 0.0575 |
| A | 50 | 1.0000 | 0.5942 | 0.5273 | 0.3887 | 0.6667 | 0.0805 |
| A | 60 | 1.0000 | 0.6159 | 0.5167 | 0.4047 | 0.7451 | 0.1724 |
| A | 70 | 1.0000 | 0.6087 | 0.5321 | 0.3944 | 0.6667 | 0.0690 |
| A | 71 | 1.0000 | 0.6087 | 0.5321 | 0.3944 | 0.6667 | 0.0690 |
| B | 10 | 1.0000 | 0.5942 | 0.5177 | 0.3812 | 0.6667 | 0.1034 |
| B | 20 | 1.0000 | 0.6087 | 0.5321 | 0.3817 | 0.6667 | 0.0690 |
| B | 30 | 1.0000 | 0.6232 | 0.5242 | **0.4247** | 0.6078 | **0.0345** |
| B | 40 | 1.0000 | 0.6159 | 0.5242 | 0.3960 | 0.6078 | **0.0345** |
| B | 50 | 1.0000 | 0.6087 | 0.5296 | 0.3952 | 0.6863 | 0.0805 |
| B | 60 | 1.0000 | 0.6159 | 0.5109 | 0.4024 | 0.7255 | 0.1724 |
| B | 70 | 1.0000 | **0.6449** | 0.5308 | 0.4201 | **0.7255** | 0.1149 |
| B | 71 | 1.0000 | 0.6377 | 0.5308 | 0.4151 | **0.7255** | 0.1149 |

Stage A 与 Stage B 的权威严格选择均为
`NO_DEV_CHECKPOINT_QUALIFIED`，通过数均为 `0`，`selected_checkpoint=null`。
Stage A / B 选择文件 SHA-256 分别为
`ad7613bf939ebc735de809f263d4d4353b05dce62885375e1d82f685d93826ab` 与
`5e3a3e3775cf5040f0d231757de3371ac364d47ef38b4f9e6eee8bfb4556908c`；
跨阶段只读对照 SHA-256 为
`d8013c736c3e1e8bd32e6e6509f580ebf18088273ce415c25e3c0ecce4b60c7a`。
weak / hardcase-v3 分层诊断 SHA-256 分别为
`3b8c4f10ec6d7f41cca9603b56e4e99fe411e36457adda6f8792c7dcf5be7c9a`
和 `cc189ff487f9a3b72acd1f2985b7730d7689ecaa69f5d35209de197bdc2e4058`。

B-70 的 exact 与召回相对最高，但其误报率 `0.1149` 已超过上限；B-30 的
误报和极性组合相对较好，但重大性 F1 与召回明显不足。不存在可用单项亮点绕过
全部门槛的检查点。v14 因此停止在开发失败状态，不打开密封集、不发布、不替换
生产模型；下一轮不得继续使用同一 DEV 调超参数。
