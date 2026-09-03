# 最新内容安全审计（PR #22 / `codex/event-quality-recovery`）— 2026-08-23

## 审计契约

- 类型：**只读审计**。未对生产主机、数据库、安全组、Release 资产或 git 历史做任何写入。
  按 `CURRENT_STATE.md` 的既有约定，本报告**不构成写入授权**。
- 审计对象：仓库当前**最新内容**，即草稿 PR #22 分支
  `codex/event-quality-recovery` @ `ee53a38`（`fix: isolate overview from live
  database writes`）。
- 对照基线：`main` @ `10208ce`（`2026.08.19.1`），即
  [2026-08-22 公网面审计](PUBLIC_SURFACE_SECURITY_AUDIT_2026-08-22.md) 的基线。
- 本地回归：在该分支上 `python -m pytest -q` → **1000 passed, 33 subtests
  passed**（Python 3.11 环境，非 CI 结果）。仓库自带的 CI 安全门禁（秘密模式 +
  交易写路由扫描）在该分支上原样运行通过。

### 覆盖边界

审计环境只放行 443 出网，`:8443` 仍无法访问（上一份报告已用已知开放的非 443
端口做过对照实验确认）。因此**无法确认公网当前实际运行的是 `main` 还是本分支**。
下文所有部署相关结论均来自分支内的模板与安装脚本，不是现场抓包。

---

## 0. 版本事实分叉（先看这个）

| 事实 | 值 |
|---|---|
| 最新 git tag | `v2026.08.19.1` |
| `main` 的 `VERSION` | `2026.08.19.1` |
| 本分支的 `VERSION` | **`2026.08.22.13`** |
| 本分支相对 main | 28 提交 / 155 文件 / **+34,402 −438** |
| 合并状态 | 草稿 PR #22，自 2026-08-20 起未合并 |

分支提交历史里包含 `Release Finance Radar 2026.08.22.1` 和 `2026.08.22.2` 两个
发布提交，但仓库中**没有对应 tag**。README 已经声明「GitHub 最新已标记的恢复版本、
当前源码分支和生产运行状态是三种不同事实」；本次审计确认这三者的距离比之前更大：
带标签的发布停在 08.19.1，`main` 停在 08.19.1，而实际最新代码（含一个全新的
外部 LLM 依赖、一个新 systemd 服务和定时器）只存在于一个草稿分支上。

这不是漏洞，但它决定了「审计了什么」和「线上跑的是什么」之间的可追溯性。
**建议**：要么把本分支合并并打标签，要么在 `CURRENT_STATE.md` 里显式记录
「生产运行版本 = 某分支某提交」，不要让发布事实只存在于分支内的 `VERSION` 文件。

---

## 1. 新增的主要攻击面：DeepSeek 外部 LLM

本分支引入 `app/services/deepseek_capture_interpretation.py`、
`app/services/capture_interpretation.py`、
`deployment/systemd/finance-radar-capture-interpretation.{service,timer}`，
把外部大模型（`deepseek-v4-flash`）接入「采集内容解读」链路，并把结果
**展示给公开读者**。

按四条主线逐项核对，**均已堵住**：

### 1.1 密钥能否外泄 —— 否

- `DeepSeekCaptureInterpretationProvider.__post_init__` 强制
  `base_url.rstrip("/") == "https://api.deepseek.com"`，否则抛错。
  因此可配的 `FINANCE_RADAR_CAPTURE_LLM_BASE_URL` **无法把 Bearer 令牌
  指向其他主机**。模型名同样锁定为已批准的最便宜模型，`max_tokens` 限定 128–1200。
- 密钥经 systemd `LoadCredential=deepseek_api_key:/etc/finance-radar/deepseek-api-key`
  注入，**不进入进程环境**，也不出现在 `systemctl show` 中。
- 单元带 `ConditionPathExists`，密钥缺失时静默不运行。
- HTTP 错误路径**从不回显 provider 响应体**，只返回形如 `DEEPSEEK_HTTP_429`
  的收敛错误码（代码注释明确指出上游网关可能回显请求内容）。响应读取上限 2 MiB。

### 1.2 提示注入能否得手 —— 否

- `llm_assisted_interpretation()` 在源文本命中 `PROMPT_INJECTION_RE` 时
  **直接抛出 `SOURCE_PROMPT_INJECTION_DETECTED` 拒绝生成**，而不是仅打标记；
  该捕获回落到确定性路径，并把 `prompt_injection_suspected` 置真。
- 真正的防线是**结构性约束而非提示词信任**：
  - 每条引文必须是所提供标题/摘要的**精确子串**（`quote in source_text`）；
  - 角色限于 `{ACTOR, ASSET, CONTEXT}`，情态限于封闭词表，越界一律降级为 `UNCLEAR`；
  - 中文叙述里出现的数字必须在原文出现，否则替换为无数字的边界表述；
  - `affected_assets` **由服务端正则重算**，模型给出的资产列表被直接丢弃。

### 1.3 能否影响确定性状态门 —— 否

- `llm_assisted_interpretation` 中 `**validated` 只注入 `MODEL_OUTPUT_FIELDS`；
  收据哈希、处置文案、持久化状态和全部安全标志均为服务端所有。
- 结果内硬编码
  `safety: {formal_status_mutated: False, used_as_event_truth: False,
  used_as_model_feature: False, price_used_as_truth: False, no_trading: True}`。
- 结果写入 operations 库的 `capture_interpretation_runs` 表，与账本的
  canonical 事件状态分离。
- 系统提示明确禁止推断正式事件状态、禁止交易建议、禁止编造主体/日期/金额/引文/URL。

### 1.4 公开渲染是否安全 —— 是

- `public_capture_interpretation()` 是**显式字段白名单**，不含 provider、
  模型名、token 用量、费用、重试次数或错误码。
- `/api/v1/events/{event_id}/source-interpretations` 在**每次返回前用当前合同
  重新校验缓存输出**（`validate_interpretation_result`），不通过则回落到确定性
  解读并标记 `FAILED_VALIDATION`。即使存量行被篡改或合同演进，也不会照原样吐出。
- `app/web/Home.py` 对每一个 LLM 派生字段（`one_line_zh`、`text_zh`、`quote`、
  `missing_to_change_state_zh`、`what_source_does_not_prove_zh`、
  `affected_assets`、`why_current_state_zh`、`coverage`、`status`）都调用了
  `escape()`。**未发现 XSS。**

---

## 2. 本分支的新发现

| 编号 | 严重度 | 标题 |
|---|---|---|
| N-1 | 中 | 新 systemd 单元是防护最弱的一个，却持有第三方凭据并出网 |
| N-2 | 中 | 安装脚本完全不管 DeepSeek 密钥文件的属主与权限 |
| N-3 | 中 | 外部调用的花费与次数上限默认关闭 |
| N-4 | 低 | 公开页每次交互的上游开销翻倍，而限流仍是单一全局桶 |

### N-1 · 新单元防护最弱

`finance-radar-capture-interpretation.service` 缺少 `UMask`、
`CapabilityBoundingSet`、`RestrictAddressFamilies`、`ProtectProc`：

| 单元 | UMask | CapabilityBoundingSet | RestrictAddressFamilies | ProtectProc |
|---|:--:|:--:|:--:|:--:|
| `finance-radar-web` | ✓ | ✓ | ✓ | ✓ |
| `finance-radar-api` | ✓ | | | |
| `finance-radar-worker` | ✓ | | | |
| `finance-radar-capture-interpretation` | | | | |

偏偏它是**唯一持有第三方 API 凭据并主动向公网发起请求**的单元。此外：

- `EnvironmentFile=/etc/finance-radar.env` 让它拿到 admin/reviewer/operator
  全部内部令牌，而它只需要 DeepSeek 密钥；
- `ReadWritePaths=/opt/finance-radar/shared` 比 API 单元的
  `/shared/data /shared/reports` 更宽。

**建议**：至少补齐 `UMask=0077`、`CapabilityBoundingSet=`、
`RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`、`ProtectProc=invisible`；
把环境文件收敛为只含该 worker 所需变量；`ReadWritePaths` 收敛到实际写入的子路径。

### N-2 · 安装脚本不管密钥文件

同一份 `install_remote.sh` 里对其他凭据是有处理的：

```bash
chown root:root /etc/finance-radar-reviewer-principals.json
chmod 0600      /etc/finance-radar-reviewer-principals.json      # 且拒绝符号链接
install -m 0600 -o finance-radar-web -g finance-radar-web /dev/null \
                /etc/finance-radar-public.env
```

而对 `/etc/finance-radar/deepseek-api-key`：**不创建、不设权限、不查属主、
不防符号链接**。单元只用 `ConditionPathExists` 检查「存在」。

**建议**：比照 reviewer-principals 的写法，在安装器里加属主/权限/符号链接校验，
并在安装后校验步骤里加一条可读性断言。

### N-3 · 花费上限默认关闭

```python
capture_llm_daily_usd_cap: float = 0.0
capture_llm_daily_cny_cap: float = 0.0
capture_llm_daily_request_cap: int = 0
# A zero daily cap means unlimited.
```

`app/storage/operations.py` 的判断是 `if daily_request_cap > 0 and ...` /
`if daily_cny_cap > 0 and ...`，`.env.example` 发布的默认值也是 `0`。

定时器为 `OnUnitActiveSec=5min`，`ExecStart` 为
`--limit 20 --scan-limit 100000 --workers 3`。因此在功能启用而上限未设时，
天花板约为 **20 × 288 ≈ 5,760 次外部调用/天**，且实际用量由外部新闻源的
洪峰决定 —— 上游一次异常放量会直接变成第三方账单。

预算核算本身写得很谨慎（按 2026-08-21 峰值价上限计价、cache miss 从贵计、
计数器不一致时把未归类 token 全按贵档），护栏是有的，只是**默认关着**。

**建议**：给 `capture_llm_daily_cny_cap` 与 `capture_llm_daily_request_cap`
设置非零的保守默认值，或在 `capture_llm_enabled=1` 且两者皆为 0 时拒绝启动。
对一个把「低成本」写进产品目标的个人项目，默认无上限与该目标相悖。

### N-4 · 公开页开销翻倍，限流仍是全局桶

公开 `Home.py` 打开事件预览时的**未缓存** `api_request` 调用：

| | main | 本分支 |
|---|--:|--:|
| 每次预览渲染 | 3 | **6** |

新增 `/knowledge`、`/sources`、`/source-interpretations` 三个调用。
与此同时，上一份报告的 M-1 在本分支**没有修复**：`api_request()` 仍不发送
`X-Real-IP`，`_rate_limit_client_key` 的逻辑也未改动，所有公网访客继续共用
`127.0.0.1` 这一个桶。

结果是全站共享的交互天花板从约 60 次/分钟降到约 **30 次/分钟**。

**建议**：与 M-1 一并修（公开 UI 转发 `st.context.ip_address` 为 `X-Real-IP`），
并给新增的三个调用套上 `cached_api_get` 的短 TTL。

---

## 3. 上一份报告的发现在本分支的状态

| 编号 | 状态 |
|---|---|
| M-1 公网限流退化成单一全局桶 | **未修**，且被 N-4 放大 |
| M-2 生产 Nginx 缺 `X-Frame-Options` / CSP `frame-ancestors` | **未修**（`nginx-radar-direct.conf` 仍只有 HSTS / nosniff / Referrer-Policy） |
| H-1 公开仓库中的生产基础设施细节 | **未修**，与 main 计数完全一致（见下表）——未恶化，也未清理 |
| H-2 公开 Release 上的 828 MiB 恢复快照 | 本次**未重新核查**，不作断言 |

H-1 标记在两个分支上的文件数完全相同：

| 标记 | main | 本分支 |
|---|--:|--:|
| `18.208.34.152` | 7 | 7 |
| `i-0fa9bfafa5eab00bf` | 6 | 6 |
| `sgr-0…` | 3 | 3 |
| `vol-0ee52134d18962a6c` | 1 | 1 |
| `C:\Users\MR` | 29 | 29 |

---

## 4. 复核为「无问题」的部分

列出来是为了避免后续重复怀疑：

**路由鉴权。** 本分支 30 条路由逐条核对，**每一条写路由都带鉴权依赖**，
没有未认证的变更接口。相比 main 还收紧了：`GET /api/v1/demo/mode` 从公开
改为需要 operator。

**`internal_reader_access`（新增依赖）。** 无凭据或凭据无效时**回落到公开视图
而非报错**，刻意避免把安全读变成认证预言机；全部比较用 `secrets.compare_digest`；
该依赖从不授予任何写权限。行为 fail-closed，正确。

**`app/api/snapshot.py`（新增）。** 状态锁与刷新锁分离，`refresh` 用
非阻塞 `acquire(blocking=False)` 避免刷新堆叠，失败时保留上一份已知良好快照并
标记 `STALE_AFTER_REFRESH_ERROR`，`read()` 返回 deepcopy。暴露的错误信息只有
异常**类名**，不含消息内容。

**`app/models/evidence_policy.py` 不是重复实现。** 它是指向
`app/evidence_policy.py` 的向后兼容再导出壳（`"""Backward-compatible imports
for the canonical evidence policy."""`），属于有意的收敛，不是分叉。

**新增代码中无凭据。** 仓库自带 CI 安全门禁在本分支原样运行通过；分支 diff 中
唯一的 `api_key=` 字面量是测试桩 `"unit-test-secret"`。

---

## 5. 建议处理顺序

1. **N-2、N-1**：改动小、纯加固，且都围绕同一个新引入的第三方凭据。
2. **N-3**：设一个保守的非零默认上限，或在启用且无上限时拒绝启动。
3. **M-1 + N-4 一起修**：服务端支持早已就位，缺的只是 UI 转发访客 IP，
   外加给三个新调用加短缓存。
4. **M-2**：两行 `add_header`，注意必须同步加进
   `location = /radar/release.json`，否则该路径会因 nginx 继承规则丢头。
5. **版本事实分叉（第 0 节）**：合并并打标签，或在 `CURRENT_STATE.md` 中
   显式记录生产运行的分支与提交。
6. **H-1**：与仓库已公开这一事实一并处理，重心在轮换与收敛入站，而非删文件。
