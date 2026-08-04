# Finance Radar

> Current backup version: `2026.07.19.1`. See
> [BACKUP_INVENTORY.md](BACKUP_INVENTORY.md) for what is stored in Git and in
> the private GitHub Release, and
> [docs/GITHUB_BACKUP_AND_RELEASE_WORKFLOW.md](docs/GITHUB_BACKUP_AND_RELEASE_WORKFLOW.md)
> for the update process.

基于多源证据链、可审计事件账本和实时 Web Situation Room 的金融事件情报系统。系统对新闻做全极性采集，但小模型只负责“重大下行风险优先级”，不会把正面新闻强行解释成做空信号。所有行情能力只读，项目没有下单、仓位、余额或交易执行接口。

## 当前可运行成品

- Schema 12 SQLite WAL 事件账本：原始观测、修订、事件版本、证据边、市场观察、作业和 Telegram outbox。
- FastAPI 只读/受控 API：健康、来源、事件、证据、时间线、trace、模型卡、回放、双人盲标、行情能力矩阵与演示模式；公网 API 每客户端默认 180 次/分钟。
- Streamlit 公网与管理端分离：公网仅提供“态势总览、证据演示、方法与边界”三页只读产品；人工复核、运行与模型、双人盲审位于仅回环监听的独立管理端。事件浏览支持中文摘要、互斥状态漏斗、来源/类别/时间筛选、排序与分页。
- 三模式演示：`LIVE`、`RECENT_CAPTURE`、`REPLAY`。
- 四个固定回放：SEC 破产核验、正面业绩非目标、谣言冲突弃权、SEC 官方更正撤回告警资格；不依赖现场出现随机事件。
- CPU 小模型：word/char TF-IDF + 校准逻辑回归，`RISK_REVIEW / NON_TARGET / ABSTAIN`，仅 shadow mode。
- 结构化 Evidence Agent：`EventClaim / EvidenceEdge / AgentDecision`，精确段落引用、冲突/不足硬门、内容寻址证据对象和人工覆盖审计；VPS 已接入回环 `llama.cpp + Qwen2.5-0.5B Q4_K_M` 真实小模型，但模型只写 advisory summary，claim 判定与最终状态仍由确定性证据图控制。
- V3 双人盲标工作流：已从真实账本建立24条未标注任务；两名审核者互不可见答案，页面隐藏模型结果、旧标签、原始来源身份和事件后行情，只提交重大性/极性/证据状态三个轴；冲突必须第三人裁决，公网写控件默认关闭。
- 常驻 Worker、Telegram 深链、30 天日备份、12 周周快照、每日加密异机拉取和隔离恢复核验；事件账本与内容寻址原文证据没有自动过期删除。
- 两层离线保障：答辩证据包用于无服务复核；可执行离线终端含22条精选真实事件、证据、Replay、影子模型、API和五页Web，启动时强制只允许回环网络，且不打包采集器、Telegram、券商/交易所客户端、密钥或交易能力。
- 12页专业答辩PPT：沿用Calm Institutional视觉系统，使用四张当前公网真图，完整呈现证据链、Replay、模型失败门禁、迁移恢复与剩余真实过程要求；每页含演讲备注并已逐页渲染复核。
- 22 路分类来源：SEC/CFTC/FTC/FDIC/Fed/BLS/FDA/ECB/EIA 等 P0 官方源，NVIDIA 等 P1 发行人源，以及 OpenNews/历史研究等 P2 发现源；来源权威级别与事件极性分开保存。
- 分类行情层：加密资产由 Binance 公共、免认证、仅行情域名持续落库；股票/ETF/外汇和商品代理由 Twelve Data 落库；IBKR TWS 只保留操作者本机只读能力探针。系统以首个真实快照为观察基线调度 T+5m/T+30m/T+1d，超过宽限期就记录 `MISSED_WINDOW`，绝不拿最新报价回填；收益指标只能进入事后审计。Operations 展示提供商、窗口状态与退化原因，Event Workbench 显示报价、币种、采集年龄、三窗口和不可用状态。
- AWS 美国东部 EC2 的 systemd + Nginx 生产拓扑：API、Web、Worker、每日备份均为独立服务；应用和数据使用 release/shared 分离。原新加坡服务器已停止，不再承担生产流量。
- Docker Compose + Caddy 作为可移植备选拓扑（本机无 Docker，因此没有冒充完成容器运行验收）。

当前已接受 VPS 迁移快照（2026-07-19 04:55 UTC）：22 个来源、1194 个事件、3951 个原始观测、2117 个事件版本、2394 条证据、1898 条事件后市场指标；262 个已落库 Worker 周期、35 次可验证在线备份、7 次回放、7 次模型运行、24 条未标注任务和0条人工审核。Worker 每5分钟自动运行，失败后由 systemd 20秒重启；事件与证据不设TTL。快照内证据存档含83个不可变对象，其中81个是官方原始页面快照（80份HTML、1份PDF，共10,936,893字节），另有2份精确引文。注册官方来源的HTTP链接会先安全升级到HTTPS并再次验证跳转域名，每轮最多归档4份；分页扫描可越过已存档或长期失败的头部记录，避免后续证据被卡住；支持HTML/PDF/JSON，保持渐进采集。在线备份保留30个日备份与12个周备份。Windows 异机任务生成服务器一致性快照，经 SSH/SCP、SHA、tar、AES-256-GCM 后做完整隔离恢复。最新已接受异机备份 `20260719T045536Z` 已逐项核验 9,860 个文件清单，恢复后的 Schema 12/3 两套 SQLite 均通过 `quick_check` 与 `integrity_check`，并确认包含当前 release `20260719T044852Z`、五页终端、本机命名Flow、只读Facets、来源筛选、终端命令条、统一中文操作层、官方原始证据存档、跨页全局检索、T+窗口调度与错过保护、双人盲标工作流、本地 Qwen/llama.cpp 模型运行时、冻结评测、换机VPS失败前置检查和在线备份器，不含交易项目与 TLS 私钥；9,861个常规文件/1,559,757,804字节的空白VPS服务树预恢复也已通过。`migration_full_restore_latest.*` 由备份脚本自动同步。事件账本三项硬边界违规均为 0。

持续增长复核（2026-07-19 05:17 UTC）：Worker 已达266周期/20.613小时、最新状态`SUCCESS`，线上证据对象已从已接受快照的83个继续增长到95个，证明部署后采集没有停。

本地 Evidence Agent 模型于 2026-07-18 17:29 UTC 完成真实上线：冻结集 8/8 通过，合同接受、确定性记录保持、引用合规、提示注入抵抗均为 100%，p50/p95 为 5.86/7.24 秒；真实 SEC litigation event 经 API 得到 `llm_used=true / local_llama_cpp / ACCEPTED_ADVISORY_ONLY`，总延迟 6.32 秒。首轮让 0.5B 模型直接判断 claim 的 8/8 失败报告仍保留；职责收窄后的晋级结论仍为 `REMAIN_SHADOW`。详见 [本地小模型说明](docs/LOCAL_EVIDENCE_MODEL.md)。

换机不再只有“备份文件”：最新已接受快照已完成 1,559,757,804 字节的全服务恢复准备演练，9,861 个普通文件再次展开并校验，78 个归档软链接全部跳过后转为受限激活计划，临时明文自动清理。`scripts/restore_migration_to_vps.ps1` 默认只审计；真实新机必须显式 `-Activate`，并先在远端执行失败前置检查，核验 Linux/x86_64、root、磁盘/内存、Python 模块、systemd、端口、工具和简单 HTTPS `/radar` 入口；目标已有 `/opt/finance-radar` 或命中当前 VPS 时拒绝。第二份恢复密钥已保存到仓库外的 `C:\Users\MR\Documents\FinanceRadar-Recovery`，哈希与主副本一致、ACL 继承关闭且仅当前用户有访问规则。详细证据为 `reports/new_vps_encrypted_restore_audit_latest.*`、`reports/migration_service_restore_drill_latest.*` 与 `docs/SERVER_MIGRATION_HANDOFF.md`；Nginx/TLS 仍须在新 IP/域名确定后单独切换。

## 在线成品

- 公网只读终端：<https://radar.18-208-34-152.sslip.io:8443/radar/>
- 管理端：仅通过服务器回环端口与 SSH 隧道按需开启，不设公网路由。
- API：仅监听服务器 `127.0.0.1:18000`；公网 `/finance-radar-api/`、管理页面和内部路由统一返回 `404`。

直接 HTTPS 入口使用自动续期证书。Nginx 只代理公开 Streamlit 页面；现有 VPN、Xray 与 WireGuard 不属于本项目部署范围。

## 本地启动

```powershell
python -m pip install -r requirements-dev.txt
python scripts/train_risk_router.py
python scripts/build_external_blind_set.py
python scripts/evaluate_external_blind.py
python scripts/audit_risk_router_shortcuts.py
powershell -ExecutionPolicy Bypass -File scripts/start_product.ps1
```

打开：

- Web: <http://127.0.0.1:8501>
- API docs: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/api/v1/health>

停止：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/stop_product.ps1
```

也可以分别运行：

```powershell
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000
python -m streamlit run app/web/Home.py --server.port 8501
python -m app.workers.continuous --interval 300
python -m app.ops.backup backup
```

Telegram 外部发送永远需要显式 `--send`。本地健康演练不访问外网：

```powershell
python -m app.workers.continuous --once --health-only
python -m app.workers.notifier --once
```

## 验证

```powershell
python -m pytest -q
python -m pytest -q --cov=app --cov=scripts --cov-report=term-missing
python -m compileall -q app scripts tests
python scripts/collect_product_acceptance.py
python scripts/capture_market_capabilities.py
python scripts/run_defense_drills.py
python scripts/seed_adjudication_queue.py --limit 24
python scripts/audit_adjudication_workflow.py
python scripts/verify_public_adjudication.py
python scripts/smoke_load_test.py --requests 60 --concurrency 6
python scripts/audit_migration_restore.py server_migration_backup/20260719T045536Z/finance-radar-migration-20260719T045536Z.tgz.aesgcm --expected-release 20260719T044852Z --expected-sha256 ac3ed8ba2a1ebd0f90eddfff921b39f683f17a397bfa9b2d6e074a467b24c1a5
python scripts/build_defense_evidence_pack.py
python scripts/build_offline_demo.py
python scripts/verify_offline_demo.py --bundle-root artifacts/offline_demo/current --archive artifacts/offline_demo/finance-radar-offline-demo-latest.zip --report-dir reports
python scripts/audit_course_readiness.py
python scripts/audit_course_readiness.py --require-product-ready
python scripts/audit_course_readiness.py --require-ready
```

当前回归结果为 `360 passed, 17 subtests passed`。新增换机VPS资源/端口/工具/HTTPS失败前置门、本地模型输入合同、V2数据清洗/来源留出、v3风险标签一致性、双人盲审/第三人裁决、默认关闭公网写入、本地模型回环限制、严格 JSON 合同、伪造 citation 拒绝、控制边界、提示注入、分类行情提供商隔离、T+窗口错过保护、跨页检索/深链筛选连续性、只读Facets与来源精确筛选、扩展验收报告向前兼容、本机Flow状态边界/localStorage无网络，以及官方原始HTML/PDF/JSON证据快照的域名白名单、安全HTTPS升级、重定向复核、大小上限、MIME、哈希完整性、幂等和跨分页持续采集测试。v3合同将P0/P1/P2仅用于确定性证据通道，要求由内容、重大性、极性和证据状态共同决定`RISK_REVIEW / NON_TARGET / ABSTAIN`；现有877条候选清单因没有双人内容裁决且已提前分割，被机器判定`NOT_READY_FOR_BLIND_V2`，生产模型未改。五个 Streamlit 页面使用 AppTest 运行结构回归。上一版 release 已用真实 Chrome 从公网 HTTPS 完成 1920×1080 答辩大屏、1366×768 桌面及 390×844 移动关键路径矩阵，6/6交互与五页可访问性门禁均通过；当前 `20260719T044852Z` 在此基础上加入可跨分页持续推进的渐进式官方证据归档、Operations可见性、本机命名Flow、事件族/来源联想、首页命令条、统一中文操作层与换机VPS失败前置检查；结构回归、公网产品验收19/19和行情能力验收17/17均已通过，真实浏览器视觉/交互矩阵明确待刷新，不能沿用旧截图冒充当前版本。公网已经真实显示 Binance 的旧 T+5m/T+30m 为 `MISSED_WINDOW`、T+1d为`PENDING`，证明系统没有用同一时刻报价伪造窗口。12页答辩PPT位于`artifacts/defense_deck/`。在线验收快照位于 `reports/product_acceptance_live_latest.json`，官方原始证据采集报告位于`reports/evidence_source_snapshots_latest.*`。6 项无网络故障注入位于 `reports/defense_drills_latest.json`，全部通过。最新离线答辩包位于`artifacts/defense_pack/`；它不包含环境文件、解密密钥、加密服务器备份、Telegram发送能力或交易项目。

人读项目任务书 `financial_event_radar_project_proposal_v5_2_human.docx` 已完成真实 Word 引擎二次渲染验收：10/10 页逐页原尺寸检查，无截断、重叠、表格溢出、缺字、编号错乱或页脚碰撞，a11y high/medium/low 均为 0。新版已写入 release、5分钟采集、每轮4份原始证据、无TTL、30日/12周在线备份与最新换机恢复证据；可复核 PDF、逐页 PNG 与检查记录位于 `reports/docx_qa_v5_2_long_running_20260719T043425Z/`。

风险模型在分组时间留出集上的组合特征覆盖率为 82.7%、覆盖样本准确率为 95.7%；三组消融及漂移阻断阈值位于 `artifacts/risk_router_robustness.json`。真正的 label-first 外部盲测另行冻结 40 条 SEC/CFTC 风险公告与 Fed/NVIDIA 非目标公告，训练标题/ID 重叠为 0；结果为风险召回 100%、正常新闻误报 95%、门槛 FAIL。完整报告位于 `artifacts/risk_router_external_blind_v1_report.json`，捷径诊断位于 `artifacts/risk_router_v1_shortcut_audit.md`。模型因此明确保持 shadow，只可做候选队列高召回路由，不能作为正负新闻总分类器。

新增可执行离线终端位于 `artifacts/offline_demo/`：45个文件、ZIP CRC/哈希/敏感值扫描、Schema 12/3、恢复副本、外网阻断、11个API检查、五页渲染、Replay与无交易路由共11/11通过。它与完整加密迁移备份职责分离：前者用于断网答辩，后者用于生产换机。最新答辩证据包同时纳入该终端和验收报告。

V2 没有停留在建议层：本地从当前 VPS 在线库生成了一致性只读快照，训练候选只使用公司名、发布时标题/摘要、确认事实和精确证据，删除事件族/类型/来源字段及遗留控制词；Apple/Microsoft 官方内容作为开发负样本，ECB/EIA 完全按来源留出，且排除全部旧 blind 精确样本。结果控制词 Top 系数命中降为0，但开发覆盖率仅40.4%、covered accuracy 78.5%，ECB/EIA 留出覆盖4.2%、准确率0%，所以候选被机器门禁标记为 `REJECTED_CANDIDATE_NOT_DEPLOYED`，生产V1未替换。审计同时证明旧 blind 的20条风险输入全是“标题重复、无正文”，不能作为证据阶段内容模型的V2晋级集。证据位于 `reports/risk_router_input_contract_audit_v1.*` 与 `artifacts/risk_router_v2_candidate_report.*`。

## 目录

```text
app/api/       FastAPI 合同
app/storage/   事件账本只读适配器与运维状态库
app/services/  确定性回放、结构化 Evidence Agent 与双人盲标服务
app/models/    影子风险分流器
app/web/       Streamlit Situation Room
app/workers/   常驻采集、通知与备份调度
deployment/    systemd/Nginx 在线部署与 Docker/Compose 备选拓扑
replay/cases/  冻结回放案例
.agent/        架构、合同、测试、AI 使用与禁飞区证据
reports/runtime_evidence/  每15分钟运行证据哈希链与自动PASS报告
artifacts/defense_pack/    无密钥、可离线复核的答辩证据包
artifacts/offline_demo/     无外网也能启动的五页终端、精选账本与恢复副本
```

VPS 部署见 [deployment/README.md](deployment/README.md)，当前完成度与剩余门槛见 [ACCEPTANCE_STATUS.md](ACCEPTANCE_STATUS.md)。
