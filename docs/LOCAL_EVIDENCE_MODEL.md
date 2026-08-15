# 本地 Evidence Agent 小模型

更新时间：2026-08-05

## 结论

Finance Radar 的本地小模型属于可选的咨询能力；它被刻意限制为“证据摘要写手”，
不是事实裁判、风险分类器或交易模型。标准部署和灾难恢复路径均默认保持它禁用，
只有操作者通过独立资源门后才可手动启用。事件最终状态仍由
确定性证据图决定：矛盾强制人工复核、证据不足强制弃权、只有 P0 支持才能
进入 `EVIDENCE_READY`。模型既不能修改这些规则，也不能调用任何交易能力。

## 运行拓扑

- 模型：`Qwen2.5-0.5B-Instruct-GGUF`，`Q4_K_M`，约 491 MB。
- 推理服务：固定版本 `llama.cpp b10068`。
- 监听：`127.0.0.1:18601`，不向公网开放。
- 资源保护：模型 unit 使用 `MemoryHigh=460M`、`MemoryMax=560M`、
  `MemorySwapMax=128M`、`CPUQuota=150%`，并置于总上限为
  `MemoryHigh=600M` / `MemoryMax=700M` 的 `finance-radar.slice` 内。
- API：只有配置了回环 URL 才调用模型；超时、非法 JSON、未知 claim、伪造
  citation、提示注入或越过控制边界时，整次模型输出作废并回退确定性结果。
- 模型职责：把服务器生成的 `authoritative_review_records` 写成简短中性摘要。
  claim verdict 与 citation 列表由证据图生成，而非由模型生成。

模型和运行时均固定 SHA-256。安装脚本在内存、磁盘、架构和文件哈希全部通过
后才允许 `--activate`，健康失败会自动停用模型服务，不影响 API/Web/Worker。

## 为什么不用它直接判断真假

0.5B 模型首轮直接判断冻结案例时，8/8 都未通过严格合同：它会照抄 schema
占位符、漏 claim，并在注入案例产生非法 JSON。这份失败证据保留为
`reports/local_evidence_model_comparison_initial_fail.*`，没有删除。

第二轮把模型职责收窄为摘要，并使用 llama.cpp JSON Schema 在解码阶段锁定输出
结构。8 个冻结案例覆盖 P0/P2、无证据、矛盾、多 claim、双来源、正面事件和
提示注入；最终合同接受、确定性记录保持、引用合规和注入抵抗均为 100%，
但晋级决定仍是 `REMAIN_SHADOW`。

这不是“调提示词把失败藏起来”，而是根据模型能力重新划定责任：弱模型擅长
语言表达，不适合承担可审计事实裁决。这个边界本身就是项目治理亮点。

## 验证

```bash
python scripts/evaluate_local_evidence_model.py --timeout 60 --max-tokens 400
python scripts/accept_local_evidence_model.py
```

关键证据：

- `reports/local_evidence_model_comparison_latest.json`
- `reports/local_evidence_model_comparison_initial_fail.json`
- `reports/local_evidence_model_live_acceptance_latest.json`
- `replay/evidence_agent_comparison/cases.json`
- `tests/test_local_evidence_model.py`

## 迁移

`create_migration_backup.sh` 会把模型能力写入
`config/LOCAL_EVIDENCE_MODEL_CAPABILITY.json`，并仅在运行时完整时归档模型与固定
llama.cpp 运行时。归档元数据明确区分“源主机安装过模型”和“本次归档包含模型”；
模型缺失且元数据声明为可选时，审计和预恢复仍可通过。

恢复脚本始终禁用并停止本地模型，不会为了恢复 Radar 的 API/Web/Worker/备份服务而
自动拉起它。若需要恢复模型，由操作者在确认内存余量、停掉 Worker 与备份任务后，
另行执行 `install_local_evidence_model.sh --activate`。旧归档仍按其原有的严格模型
哈希契约审计，不会被新的可选规则静默放宽。

上游资料：

- <https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF>
- <https://github.com/ggml-org/llama.cpp>
