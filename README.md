# Finance Radar

> 面向个人研究者的只读金融事件证据雷达：持续发现多源事件，压缩重复报道，把人快速带回原始证据、修订时间线与事后行情；证据不足时明确保留不确定性。

[所有者意图与系统细则](docs/OWNER_INTENT_AND_SYSTEM_DOCTRINE.md) · [当前状态](CURRENT_STATE.md) · [产品章程](docs/PRODUCT_CHARTER.md) · [下一阶段计划](docs/NEXT_PHASE_PLAN_2026-08-15.md) · [Scrum 看板材料](docs/scrum/README.md) · [2026-08-13 目标一致性审计](reports/PROJECT_ALIGNMENT_AUDIT_2026-08-13.md) · [部署说明](deployment/README.md)

Finance Radar 不是交易终端，也不是自动事实裁判。系统收集并核验全极性金融事件；面向做空研究的特化只存在于“重大下行风险人工复核路由”层。模型只回答是否值得优先交给人核验，禁止输出 LONG/SHORT、价格方向、收益、仓位或交易许可。正式结论必须受确定性证据门和人工判断约束；代码中没有订单、持仓、余额或交易执行接口。

## 用户能得到什么

- 多源事件发现：监管机构、宏观机构、发行人、聚合发现源和只读行情源被分级记录。
- 可核验事件：每个结论都能回到来源、原文快照、精确引文、抓取时间和修订关系。
- 诚实的不确定性：弱证据、冲突、来源失败、错过的行情窗口和失败模型不会被包装成确定结果。
- 事件后观察：T+5m、T+30m、T+1d 只记录真实获得的行情；错过窗口即标记 `MISSED_WINDOW`，不以后来的报价回填。
- 可复盘演示：冻结 Replay 可在没有实时重大事件时重现证据门、更正和弃权过程。

## 产品层与后台层

当前实现有四套独立运行面：

| 层 | 面向谁 | 内容 | 暴露方式 |
|---|---|---|---|
| Public | 普通用户与演示观众 | 态势总览、证据演示、方法与边界 | 公网只读 Streamlit |
| Reviewer | 人工复核者 | 事件证据、人工判断、双人盲审 | 仅服务器回环，独立令牌与按需 SSH 隧道 |
| Operator | 运维者 | 来源、Worker、备份、Shadow 模型与运行诊断 | 仅服务器回环，独立令牌与按需 SSH 隧道 |
| Admin | 开发者与紧急管理员 | Reviewer 与 Operator 的全权超集 | 仅服务器回环，独立全权令牌与按需 SSH 隧道 |

公网 Nginx 不暴露 API、管理页面或内部路径。Reviewer、Operator 和 Admin 使用不同入口与 API 能力合同；它们仍是回环、按需启动的轻量角色层，不冒充完整账户系统。

## 关键安全边界

- `NO TRADING`：不连接下单接口，不读取账户、余额、仓位，不生成仓位建议。
- `NO AUTO VERIFY`：模型和 Worker 不得自行把事件升级为正式 `VERIFIED`。
- `NO LEAKAGE`：事件后行情、旧标签和模型输出不得进入盲审输入。
- Public、Reviewer、Operator 与 Admin 使用不同导航、环境令牌和 API 权限；三种内部 UI 互斥启动。
- Telegram 默认只做 dry-run；真实外发必须单独、显式授权并使用 `--send`。
- Evidence Agent 的本地 LLM 是可选、回环、advisory-only 服务；部署和恢复不会默认启用它。
- 本机备份策略为每天一次、仅在新备份完成隔离恢复验证后替换上一份；事件账本和原文证据不按该保留策略删除。

## 当前能力边界

- 风险路由模型保持 `SHADOW`。legacy external-blind-v1/v2 的失败永久保留；后续 v4 AI-rubric blind-v3 即使达到 `QUALIFIED_SHADOW`，也只表示可继续做咨询式复核路由。V3 authentic-human blind-v2 尚未冻结，因此它不是事实裁判、提醒许可或交易模型。
- Evidence Agent 只能生成结构化建议与摘要；claim、证据充分性和最终状态仍由确定性规则和人工复核决定。
- Telegram 的 outbox、幂等和深链已实现，但默认未启用持续外发。当前产品应理解为 **Web-first，Telegram optional/off by default**。
- GitHub 最新已标记的恢复版本、当前源码分支和生产运行状态是三种不同事实。请以带时间戳的 [CURRENT_STATE.md](CURRENT_STATE.md) 为入口，不要从历史报告推断实时状态。

## 本地启动

要求 Python 3.12。先创建虚拟环境并安装开发依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.lock
powershell -ExecutionPolicy Bypass -File scripts/start_product.ps1
```

默认入口：

- Web：<http://127.0.0.1:8501>
- API 文档：<http://127.0.0.1:8000/docs>
- Health：<http://127.0.0.1:8000/api/v1/health>

停止：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/stop_product.ps1
```

也可分别运行：

```powershell
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
python -m streamlit run app/web/Home.py --server.port 8501
python -m app.workers.continuous --interval 300
python -m app.ops.backup backup
```

## 验证

```powershell
python -m compileall -q app scripts tests
python -m pytest -q
python -m pytest -q --cov=app --cov=scripts --cov-report=term-missing
```

本分支的最终回归结果会在 [CURRENT_STATE.md](CURRENT_STATE.md) 与审计报告中绑定到精确提交；本地通过不代表 AWS 当前运行状态，也不能替代新提交的 GitHub Actions 结果。

CI 还会检查编译、测试、秘密模式、交易写路由和部署 Shell 语法。正式发布前必须另外通过 release audit、恢复收据验证和真实 Linux 上线验收。

## 部署

主部署形态是 AWS 上的 systemd + Nginx：API、Public Web、三种按需内部 UI、Worker、备份和可选 Evidence LLM 分离运行，并由 systemd slice 约束总内存与任务数。Docker Compose + Caddy 仅是可移植备选形态。

公网入口不写死在仓库中；部署时必须显式传入 `https://YOUR_DOMAIN[:PORT]/radar/`，当前实际域名与运行状态以部署后现场验收记录为准。

安装、回滚、备份、迁移和恢复流程见 [deployment/README.md](deployment/README.md) 与 [docs/SERVER_MIGRATION_HANDOFF.md](docs/SERVER_MIGRATION_HANDOFF.md)。原新加坡 Finance Radar 实例已经退出产品拓扑，历史报告中的新加坡地址仅作历史证据。

## 目录

```text
app/api/       内部 FastAPI 合同
app/models/    影子风险路由与证据策略
app/services/  回放、Evidence Agent 与人工裁决服务
app/storage/   事件账本和运维状态库
app/web/       Public/Reviewer/Operator/Admin Streamlit 界面
app/workers/   连续采集与通知调度
deployment/    systemd/Nginx 与恢复发布流程
replay/cases/  冻结回放案例
tests/         产品、证据、安全、部署与恢复合同
docs/          当前规则和操作文档
reports/       带日期的历史证据与审计快照
```

`reports/`、旧项目书和答辩物料是审计历史，不是当前状态源。课程和演示交付物仍会保留，但必须服务于核心产品，不能取代个人研究用户的事件发现与核验效率。

## 最初目标与当前偏差

最早可靠的产品基线是 [V2 项目书](financial_event_radar_project_plan_v2_0_ai.md)：个人、多资产、低成本、持续发现、证据核验、市场观察、Telegram 线程、严格只读。当前系统保住了证据和安全内核，但工程重心明显转向课程答辩、模型治理、迁移恢复和工程证明。

这不是另一个项目，却是一项需要纠偏的优先级变化。完整证据、风险分级和整改路线见 [2026-08-13 目标一致性审计](reports/PROJECT_ALIGNMENT_AUDIT_2026-08-13.md)。
