# 本地 Evidence Agent 小模型

更新时间：2026-07-19

## 结论

Finance Radar 已在新加坡 VPS 上部署真实本地小模型，但它被刻意限制为
“证据摘要写手”，不是事实裁判、风险分类器或交易模型。事件最终状态仍由
确定性证据图决定：矛盾强制人工复核、证据不足强制弃权、只有 P0 支持才能
进入 `EVIDENCE_READY`。模型既不能修改这些规则，也不能调用任何交易能力。

## 运行拓扑

- 模型：`Qwen2.5-0.5B-Instruct-GGUF`，`Q4_K_M`，约 491 MB。
- 推理服务：固定版本 `llama.cpp b10068`。
- 监听：`127.0.0.1:18601`，不向公网开放。
- 资源保护：`MemoryHigh=900M`、`MemoryMax=1100M`、`CPUQuota=150%`。
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

`create_migration_backup.sh` 会把模型、固定 llama.cpp 运行时、systemd unit、
比较报告和应用配置一起纳入加密异机快照。恢复脚本先启动并健康检查回环模型，
再启动 API；模型恢复失败时激活事务失败，不会静默假装模型在线。

上游资料：

- <https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF>
- <https://github.com/ggml-org/llama.cpp>
