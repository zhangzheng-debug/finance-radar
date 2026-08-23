# 后端实测走查与休眠功能盘点 — 2026-08-23

本文档回答两个问题：**这套后端是怎么实现的、跑起来是什么效果**，以及
**哪些已建成的功能当前没有在用**。

所有运行结果都来自在审计环境里**真实启动服务**取得，不是读代码推断。
使用空数据库 + 仓库内冻结 Replay 案例，**未接触生产主机、生产数据库或任何
生产凭据**。代码基线：`main` @ `10208ce`（`2026.08.19.1`）。

---

## 第一部分 · 后端是怎么实现的

### 1.1 进程拓扑：七个互相隔离的单元，不是一个服务

```
公网 :8443 ── Nginx ──> Streamlit Public (127.0.0.1:18501)   独立 UID，零令牌
                             │ 服务端调用
                             ▼
                        FastAPI (127.0.0.1:18000)   唯一接触数据库的进程
                             ▲
   SSH 隧道 ──> Admin :18502 / Reviewer :18503 / Operator :18504
                （按需启动、三者互斥、只绑回环；Nginx 对它们一律 404）

   Worker（continuous，间隔 300s）· Backup（每日 timer）· Evidence LLM（可选）
```

各 systemd 单元的实际 `ExecStart`：

| 单元 | 入口 | 备注 |
|---|---|---|
| `finance-radar-api` | `uvicorn app.api.main:app --port 18000 --workers 1` | 单 worker |
| `finance-radar-web` | `streamlit run app/web/Home.py --port 18501 --baseUrlPath radar` | `UI_ROLE=public` |
| `finance-radar-admin` | `streamlit run app/web/Admin.py --port 18502` | 手动启停 |
| `finance-radar-reviewer` | `streamlit run app/web/Reviewer.py --port 18503` | 手动启停 |
| `finance-radar-operator` | `streamlit run app/web/Operator.py --port 18504` | 手动启停 |
| `finance-radar-worker` | `python -m app.workers.continuous --interval 300 **--no-light-verify**` | 见 2.1 |
| `finance-radar-backup` | `run_backup_quiesced.sh`（timer：开机 10min，其后每 24h） | |
| `finance-radar-evidence-llm` | `llama-server --model qwen2.5-0.5b-instruct-q4_k_m.gguf --port 18601` | 默认不启用 |

### 1.2 一个不常见的设计：浏览器从不直接跟 API 通信

公开页是**服务端渲染**的 Streamlit —— 是服务器上的 Streamlit 进程替访客去调
FastAPI，浏览器只跟 Streamlit 的 websocket 说话。因此：

- API 没有面向浏览器的 CORS 暴露面；
- 公开 Web 进程可以做到**一个令牌都不持有**。生产的
  `/etc/finance-radar-public.env` 只有三行（API 地址、`UI_ROLE=public`、
  `SHOW_DEBUG=0`），单元还额外 `UnsetEnvironment=FINANCE_RADAR_ADMIN_TOKEN`，
  并以独立 UID `finance-radar-web` 运行。

代价见 [2026-08-22 审计](../PUBLIC_SURFACE_SECURITY_AUDIT_2026-08-22.md) 的 M-1：
所有公网访客在 API 侧共用同一个限流桶。

### 1.3 存储：两个 SQLite，事实与过程分离

冷启动实测：

| 库 | 表数 | 存什么 |
|---|--:|---|
| `finance_radar.sqlite3`（账本） | **28** | `canonical_events`、`event_evidence`、`event_versions`、`sources`、`source_revisions`、`raw_observations`、`market_snapshots`、`alert_outbox` 等 |
| `finance_radar_operations.sqlite3`（运维） | **15** | `model_runs`、`human_overrides`、`adjudication_samples/reviews/freezes`、`replay_runs`、`backup_runs`、`worker_cycles`、`formal_mutation_audits`、`light_verification_runs` |

账本库的 schema 由 `scripts/event_ledger.py` 的 DDL 建立；运维库由
`OperationsRepository` 自建（首次连接即建表）。

### 1.4 判断链路：ML 模型在最末端，而且经常根本不运行

三层逐层收窄：`scope gate` → **`evidence gate`（确定性）** → `semantic router v4`（ML，SHADOW）。

对同一段 Chapter 11 文本，只改变证据状态的实测结果：

| 证据状态 | 证据门判定 | 语义模型是否运行 | 标签 |
|---|---|:--:|---|
| 无证据 | `INSUFFICIENT` (`no_decision_grade_primary_passage`) | ✗ | ABSTAIN |
| 仅发现源 P2 | `DISCOVERY_ONLY` (`discovery_only_evidence`) | ✗ | ABSTAIN |
| 确认主证据 **+ 冲突证据** | `CONFLICTED` (`contradictory_primary_evidence`) | ✗ | ABSTAIN |
| **人工确认主证据** | `PRIMARY_SUPPORTED_REVIEWED` | **✓** | **RISK_REVIEW（0.949）** |

两点值得记住：

1. **只有人工确认过的主证据才能触发 ML 模型。** 其余情况由确定性门直接短路，
   `semantic_model_invoked=False`、`decision_source=DETERMINISTIC_EVIDENCE_GATE`、
   `confidence_applicable=False`。
2. **冲突即弃权是代码短路，不是文档口号。** 即使已有确认主证据，再加一条
   `contradicted_by_primary` 就会把门翻成 `CONFLICTED`，模型不运行。

模型自身状态：`risk-router-v4-c82cfde20465`，`status=ready`，`shadow=true`，
`no_trading=true`，三道门（`risk-scope-gate-v2`、`structured-evidence-gate-v1`、
`semantic-policy-gate-v1`）均 enforced。

### 1.5 鉴权边界实测

本地启动 API 后，同一批接口带/不带令牌的真实响应码：

| 请求 | 无令牌 | 带令牌 |
|---|:--:|:--:|
| `GET /api/v1/health` | 200 | — |
| `GET /api/v1/events` | 200 | — |
| `GET /api/v1/model/status` | **403** | 200（operator） |
| `GET /api/v1/evidence/archive` | **403** | 200（admin） |
| `POST /api/v1/demo/mode/REPLAY` | **403** | 200（operator） |

403 的响应体是结构化错误码（如
`{"code": "OPERATOR_TOKEN_REQUIRED", "message": "valid X-Operator-Token required"}`），
不泄露内部细节。注意 `/docs` 在回环上是 200，但公网 Nginx 对
`/finance-radar-api/*`、`/docs`、`/openapi.json` 一律 404，因此不构成暴露。

### 1.6 纵深防御实测

用 `UI_ROLE=public` 启动的进程去访问内部页 `/radar/Operations_and_Model`，
页面只渲染出：

> 此页面仅限内部管理环境。
> 公开界面不会开放复核写入、运行控制、模型治理或盲审工具；复核、运维和管理员工作面也彼此隔离。

**一行业务数据都没有渲染** —— `require_ui_role()` 在任何 API 调用之前就
`st.stop()` 了。侧边栏对公开角色只列 3 个入口，Admin 角色列 6 个。

这印证了 2026-08-22 审计 L-1 的结论：Nginx 的 `$arg__page` 守卫可绕过，但
**真正生效的访问控制在应用层**，而且它是有效的。

### 1.7 界面实物

同目录下四张截图（本地真实渲染，非生产数据）：

| 文件 | 内容 |
|---|---|
| `admin.png` | Admin 内部入口 —— 三个工作面的分发页，标注 `ADMIN · LOOPBACK ONLY` |
| `ops.png` | 运行与模型 —— API/账本/Worker/备份/模型状态条、运行模式受控切换（带二次确认勾选）、事件源/行情能力/证据存档/Worker/备份恢复/模型卡/硬边界审计 七个标签页 |
| `public.png` | 公开态势总览 —— 采集状态、优先级、事件流 |
| `public_blocked.png` | 公开进程访问内部页被拦的实际画面 |

⚠️ 截图中的 `API DEGRADED` / `账本 UNKNOWN` / `Worker NO DATA` 是**本地空库**的
状态，不代表生产健康度。

---

## 第二部分 · 跑起来才发现的问题

### R-1 · Replay 调用形状过时，导致公开「证据演示」演示的是弃权

`app/services/replay.py:80`：

```python
model = self.router.predict(combined_text)          # 没有 evidence_context
```

对比 `app/api/main.py:790`：

```python
evidence_context = derive_evidence_context(evidence)
data["model_shadow_output"] = router.predict(text, evidence_context=evidence_context)
```

v4 的结构化证据门对前一种调用形状直接判
`state=NOT_PROVIDED / reason=legacy_call_without_structured_evidence` 并短路。

四个冻结案例的实测结果：

| 案例 | 期望标签 | 实际标签 | `expectation_met` |
|---|---|---|:--:|
| `sec_bankruptcy_verified` | RISK_REVIEW | ABSTAIN | **false** |
| `positive_earnings_non_target` | NON_TARGET | ABSTAIN | **false** |
| `rumor_correction_abstain` | ABSTAIN | ABSTAIN | true |
| `sec_filing_corrected_abstain` | ABSTAIN | ABSTAIN | true |

**影响**：公开页的「证据演示」（Replay Lab）正是由这四个案例驱动的，也是
对外演示与答辩使用的页面。目前四个案例中有两个无法达到其冻结时的预期标签。

这不是安全问题，是 router 升级到 v4 时 replay 这条链路没有同步更新调用契约。
**本次未修复**（属于行为变更，需所有者决定）。修复方向是让 `replay.py` 与 API
使用同一个调用契约，即传入 `evidence_context=derive_evidence_context(...)`。

---

## 第三部分 · 已建成但当前没在用的功能

### 3.1 被配置关掉的运行期能力

这些代码都在，只是默认值让它们不工作：

| 能力 | 关闭方式 | 打开后会发生什么 |
|---|---|---|
| **双人盲审（真人金标）** | `FINANCE_RADAR_REVIEWER_PRINCIPALS_JSON=[]` | 现在 `/api/v1/adjudication/queue` 与提交接口返回 503 `BOUND_REVIEWER_PRINCIPALS_DISABLED`。需要为每个真人配独立 24+ 字符令牌 |
| **Evidence Agent（本地 LLM）** | `FINANCE_RADAR_EVIDENCE_LLM_URL=` 为空 | 回环 Qwen2.5-0.5B，只产出结构化建议，不改状态门。单元 `finance-radar-evidence-llm.service` 已写好 |
| **Telegram 外发** | 默认 dry-run，需装 `finance-radar-worker-send.conf` drop-in 才加 `--send` | outbox、幂等、深链、投递租约、重试都已实现 |
| **Telegram MTProto 个人频道采集** | `TELEGRAM_API_ID` / `API_HASH` 为空 | `scripts/telegram_mtproto_listener.py` 已实现 |
| **Light verification（轻核验）** | Worker 启动参数写死 `--no-light-verify` | 代码注释明确：这是刻意的，正式轻核验必须是"单独调用、限时限量的批次"，不能挂常驻授权 |
| **每周备份保留** | `FINANCE_RADAR_WEEKLY_BACKUP_RETENTION=0` | 当前只保留 1 份最新的每日已验证恢复包 |
| **行情/宏观数据源** | `BLS/BEA/FRED/MARKETAUX/TWELVE_DATA/APCA` 全部为空 | 六个 provider 的接入代码都在 |
| **IBKR 只读行情** | 需本机 TWS 开 Read-Only API | `scripts/ibkr_readonly_probe.py` |
| **Binance 只读中继** | `BINANCE_REMOTE_SSH_HOST=` 为空 | 仅 Gate-0 诊断用，非常驻路径 |

### 3.2 整块可能被遗忘的子系统

| 子系统 | 位置 | 状态 |
|---|---|---|
| **离线演示包** | `deployment/offline/` | 完整可执行快照：真实账本事件 + 精确引文 + 冻结 Replay + 五页 Web + 只读 API + 模型。**自带进程级网络守卫，只允许回环**。为"公网或外网不可用时做演示"而建 |
| **防御演练 / 证据包** | `scripts/run_defense_drills.py`、`build_defense_evidence_pack.py` | 答辩用 |
| **可访问性审计** | `scripts/audit_public_accessibility.js`、`verify_public_ui_interactions.js`、`capture_public_ui_qa.js` | Playwright 驱动的公开 UI 自动检查 |
| **claudeUI 设计工作区** | `claudeUI/`（12 文件） | **已归档，明确不得用于生产**。含设计令牌、样式补丁、原型页与静态 nginx 配置 |
| **课程/验收物料** | `scripts/collect_product_acceptance.py`、`audit_course_readiness.py`、8 份 docx 项目书 | |

### 3.3 脚本层：94 个脚本，只有一小部分在自动运行

交叉引用（排除脚本自身与 `reports/`）：

| 分类 | 数量 |
|---|--:|
| 被代码 / systemd / CI 引用 | 31 |
| 仅文档提及（运维手动执行） | 20 |
| 仅被测试覆盖，无任何调用方或文档 | 6 |
| **完全无引用** | **14** |

**真正由 systemd / worker 自动执行的只有 4 个**：
`run_live_cycle.py`（Worker 每 300s）、`telegram_alert_outbox.py`（notifier）、
`light_verify.py`（**被 `--no-light-verify` 关闭**）、
`verify_dependency_locks.py` + `release_audit.py`（CI / 发布）。

完全无引用的 14 个：

```
audit_risk_router_shortcuts.py        build_risk_router_v4_dataset.py
build_finance_radar_plan_v2/v4/v5/v6.py   evaluate_external_blind_v2.py
evaluate_external_blind_v3.py         evaluate_risk_router_robustness.py
promote_risk_router_v4.py             qa_finance_radar_plan_v2.py
recluster_official_candidates.py      register_runtime_evidence_task.ps1
smoke_load_test.py
```

其中多数是**模型训练/盲测/晋级工具链**（`train_*`、`evaluate_external_blind_*`、
`promote_risk_router_v4`、`build_*_dataset`）与**历史项目书生成器**
（`build_finance_radar_plan_v2/v4/v5/v6`）。前者是模型治理的完整工具链，
一次性用过之后没有再跑；后者是已完成的一次性产物生成器。

`smoke_load_test.py`（负载冒烟）从未被引用，考虑到审计发现的限流问题，
它可能比想象中有用。

### 3.4 最新分支上你几乎肯定没用过的新能力

草稿 PR #22（`codex/event-quality-recovery`，未合并、未打标签）新增
**24 个脚本 + 14 个服务模块 + 3 份配置**，其中值得注意的：

| 新模块 | 作用 |
|---|---|
| `deepseek_capture_interpretation.py` | 外部 DeepSeek LLM 把采集内容翻译成"一句话看懂"，结果展示给公开读者 |
| `capture_interpretation.py` | 上述能力的合同层：封闭词表、引文必须是原文精确子串、注入检测直接拒绝 |
| `ai_event_census.py` | 对每个 canonical 事件做只读、provider 中立的 AI 普查 |
| `event_admission.py` | 把"发现线索"与"已准入事实"分离的准入闸 |
| `event_fact_review.py` | 离线人工事实复核批次，带确定性校验与合并 |
| `event_quality_recovery.py` | 规划并窄范围恢复机器可重建的历史证据链接 |
| `financial_knowledge.py` | 版本化金融知识检索 + 可追溯的确定性计算器 |
| `human_gold_review.py` / `human_blind_candidate_sampler.py` / `human_gold_freeze.py` | 抗泄漏的真人金标双盲流程（`human-blind-v3.1`） |
| `source_observation_recovery.py` | 为"无可引用证据"的事件建只读恢复清单 |
| `subjectless_event_cleanup.py` | 过滤并安全清除无主体事件 |
| `event_playbook.py` / `event_taxonomy.py` | 各事件族的证据阅读规则 + 采集器/知识卡/审计共享的统一分类法 |

详见 [2026-08-23 最新内容审计](../LATEST_BRANCH_SECURITY_AUDIT_2026-08-23.md)。

---

## 附：本次走查的复现方式

```bash
# 1. 建库
python -c "from scripts.event_ledger import open_ledger; \
           open_ledger(__import__('pathlib').Path('data/finance_radar.sqlite3'))"

# 2. 起 API
python -m uvicorn app.api.main:app --host 127.0.0.1 --port 18000

# 3. 起内部 Admin 界面（生产上必须经 SSH 隧道）
FINANCE_RADAR_UI_ROLE=admin python -m streamlit run app/web/Admin.py \
  --server.address 127.0.0.1 --server.port 18502 --server.baseUrlPath radar-admin

# 4. 起公开界面（不传任何令牌）
FINANCE_RADAR_UI_ROLE=public python -m streamlit run app/web/Home.py \
  --server.address 127.0.0.1 --server.port 18501 --server.baseUrlPath radar
```

本次走查未修改任何代码、未写入生产、未触发任何外发。
