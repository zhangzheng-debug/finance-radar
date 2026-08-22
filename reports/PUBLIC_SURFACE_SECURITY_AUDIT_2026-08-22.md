# 公网面与公开仓库安全审计 — 2026-08-22

## 审计契约

- 类型：**只读审计**。本次审计未对生产主机、数据库、安全组、Release 资产或
  git 历史做任何写入或删除。按 `CURRENT_STATE.md` 的既有约定，本报告
  **不构成写入授权**；下文标为「破坏性」的项必须由所有者单独决定。
- 代码基线：`main` @ `10208ce`（`Record 2026-08-19 production audit (#20)`），
  `VERSION = 2026.08.19.1`。
- 目标公网入口：`https://radar.18-208-34-152.sslip.io:8443/radar/`
- 回归基线：`python -m pytest -q` → **729 passed, 21 subtests passed**（本地
  Python 3.11 环境复跑，非 CI 结果）。

### 覆盖范围的诚实边界

审计环境的出网代理**只允许 443 端口**，因此 `:8443` 无法从本次审计环境访问。
这一点已用对照实验确认，避免误判：

| 目标 | 结果 |
|---|---|
| `radar.18-208-34-152.sslip.io:8443` | TLS 握手被 reset / `ECONNREFUSED` |
| `tls-v1-2.badssl.com:1012`（已知开放的对照端口） | **同样失败** |
| `radar.18-208-34-152.sslip.io:443` | 连通，`nginx/1.28.3 (Ubuntu)` |

对照端口同样失败，说明失败源于审计环境的出网限制，**不能据此判断 `:8443`
是否在线**。因此本报告中所有关于 TLS、响应头和线上版本的结论，均来自本次
合并进 `main` 的部署模板，而非现场抓包。上线现场验收仍需在可直连的网络里做。

同一主机 `443` 端口可达，且对 `/`、`/radar/`、`/radar/release.json`、
`/finance-radar-api/`、`/radar-admin/`、`/radar-review/`、`/radar-ops/`、
`/radar/_stcore/health`、`/.env`、`/server-status` 全部返回 `404`，无任何
安全响应头。即它是一个裸的 nginx 默认 server，未暴露 Finance Radar 内容。

---

## 结论摘要

| 编号 | 严重度 | 标题 | 类型 |
|---|---|---|---|
| H-1 | 高 | 公开仓库仍带着「转公开前必须清理」的生产基础设施细节 | 信息泄露 |
| H-2 | 高 | 828 MiB 完整生产恢复快照是全网可下载的 Release 资产 | 数据暴露 |
| M-1 | 中 | 公网 API 限流退化成单一全局桶（已复现） | 可用性 |
| M-2 | 中 | 生产 Nginx 模板缺少点击劫持防护 | 边界加固 |
| L-1 | 低 | Nginx `$arg__page` 守卫可绕过，不应计入访问控制 | 纵深防御 |
| L-2 | 低 | `preview_event_id` 未编码即拼入 API 路径 | 潜在注入 |
| L-3 | 低 | `decrypt_file` 在 GCM 校验前落盘未认证明文 | 加密实现 |
| L-4 | 低 | 未认证的事件详情接口每次请求都跑一次模型推理 | DoS 放大 |
| I-1 | 提示 | CI 秘密扫描没有任何「基础设施泄露」模式 | 流程缺口 |
| I-2 | 提示 | 443 端口裸默认 vhost 回显 nginx 版本 | 指纹 |

---

## H-1 · 公开仓库仍带着生产基础设施细节

**状态：仓库已确认为公开，且已存在 1 个 fork。**

GitHub API 返回 `"private": false` / `"visibility": "public"` / `"forks_count": 1`。

仓库自己的 `reports/repository_audit_20260814.md` 第 C-01 条已经点名过这批内容，
但当时的风险接受写的是：

> 仓库当前为 private，非正在发生的泄露；但转公开或对外交付前必须清理。

**这个前提已经翻转，而清理没有做。** 当前 `main` 仍在追踪：

| 内容 | 位置 |
|---|---|
| EIP `18.208.34.152`、实例 `i-0fa9bfafa5eab00bf`、可用区 `us-east-1c` | `CURRENT_STATE.md:153`、`reports/aws_migration_20260721.md` 等 10+ 文件 |
| 根卷 `vol-0ee52134d18962a6c`，并注明**未加密** | `CURRENT_STATE.md` |
| 长期安全组规则 `sgr-018f725a61dfbd882 / 159.89.226.240/32` | `CURRENT_STATE.md:67`、`reports/PRODUCTION_FACT_INTEGRITY_AUDIT_V2_2026-08-19.md:80` |
| 临时 SSH 规则 `sgr-0f1e0716b5e993b73`（`211.145.54.96/32:22`，运维方源地址） | `CURRENT_STATE.md:128` |
| 备份口令文件绝对路径 `C:\Users\MR\Documents\FinanceRadar-Recovery\finance-radar-backup-passphrase.txt` | `docs/SERVER_MIGRATION_HANDOFF.md:122,150` |
| SSH 私钥路径 `C:\Users\MR\.ssh1\id_ed25519` | `reports/aws_migration_20260721.md:73` 等 7 个文件 |
| 旧 VPS `167.172.69.16` | 58 个文件 |

**影响。** 攻击者无需侦察即可得到：确切主机、确切实例、SSH 入站规则的规则 ID
与放行源地址、以及运维工作站上私钥和备份口令的确切文件名。这本身不是一个可直接
利用的漏洞，但它把「拿下那台 Windows 工作站」从一次盲目行动变成一次有目标的行动，
并与 H-2 直接叠加。

**注意可撤销性。** 已存在 fork，且历史提交中同样包含这些值。删除当前文件
**不能**完成撤回；即使重写历史，fork 与任何已有克隆仍保留副本。因此这些
标识符应当被视为**已公开**，remediation 的重心是**轮换与收紧**，不是删除。

**建议（按可逆性排序）：**
1. 立即、非破坏性：把安全组入站收敛到最小必要来源；确认 `159.89.226.240/32`
   规则是否仍有业务需要，没有就删除。给根卷做加密快照迁移（卷当前未加密这一点
   同样已公开）。
2. 立即：轮换 `C:\Users\MR\.ssh1\id_ed25519` 对应的公钥，并把备份口令换到一个
   路径未被公开记录的位置。
3. 后续：把主机地址、登录名和本地路径改为环境变量注入，脚本默认值留空
   （C-01 原本给出的修复方向）。
4. 由所有者决定：是否重写历史。鉴于 fork 已存在，收益有限，不建议把它当作
   主要手段。

---

## H-2 · 完整生产恢复快照是全网可下载的 Release 资产

Release `v2026.07.22.2` 上仍挂着：

```
finance-radar-migration-20260722T084527Z.tgz.aesgcm
  867,634,922 bytes (828 MiB)
  sha256:9caeec6a73fcbc54eaa575db1417cef4a8aaa23ba9b8b124fdf187d678437f2f
  download_count: 0
  公开可下载（仓库为 public，Release 继承仓库可见性）
```

这与 `SECURITY.md` 的自述一致（"A legacy encrypted migration archive was
published before this visibility contract was corrected"），`CURRENT_STATE.md`
也记录「旧公共 Release 恢复密文仍未删除，等待单独的破坏性动作确认」。
本次审计确认它**至今仍在线**。`download_count: 0` 是目前唯一的好消息。

**密码学实现本身是可靠的。** 审阅 `scripts/backup_crypto.py`：

- AES-256-GCM，16 字节随机 salt、12 字节随机 nonce，header 作为 AAD 绑定；
- scrypt `n=2**15, r=8, p=1`（约 32 MiB），每次加密独立 salt；
- `keygen` 生成 48 字节随机口令（base64，约 384 bit 熵）。

**因此这不是一次即时泄露 —— 前提是该归档确实用 `keygen` 口令加密的。**
风险点在于 `_derive_key` 只强制 `len(passphrase) >= 16`：如果当时用的是人工
输入的口令，那么一份任何人都能永久留存的 828 MiB 密文，就是一个可以离线慢慢
爆破的目标，而且**永远无法通过轮换来补救**。这一条与 H-1 直接叠加：口令文件的
确切路径就写在同一个公开仓库里。

**建议：**
1. 先确认（非破坏性）：该归档当时使用的口令是否来自 `backup_crypto.py keygen`。
   如果不是，应视为「密文已在攻击者手中且口令强度有限」，按数据泄露预案处理。
2. 由所有者决定（破坏性）：删除该 Release 资产。这不能撤回已发生的下载，但能
   停止继续分发。
3. 流程：`deployment/RELEASE_AUDIT.md` 应增加一条硬门禁 —— 拒绝把任何
   `*.aesgcm` / 恢复归档上传到 public 仓库的 Release。
4. 同一 Release 上的 `risk-router-v4-c82cfde20465.joblib` 也是公开资产。
   `.joblib` 是 pickle，`app/models/risk_router.py:90` 直接 `joblib.load()`。
   目前该文件由所有者自己上传、且部署走仓库内 `artifacts/`，因此没有实际
   利用面；但恢复流程若将来改为从 Release 拉取模型，就会变成远程代码执行路径。
   建议在恢复流程里对模型做哈希绑定校验（发布说明中已有哈希，应强制比对）。

---

## M-1 · 公网 API 限流退化成单一全局桶（已复现）

`app/api/main.py:_rate_limit_client_key()` 的文档写的是按客户端限流：

> The production API listens on loopback. Only a connection that actually
> arrives from a configured proxy host may supply `X-Real-IP`; direct callers
> cannot manufacture new buckets with `X-Forwarded-For`.

这个设计是对的，但**实际拓扑里没有人给它送 `X-Real-IP`**：

```
浏览器 ──HTTPS──> Nginx ──proxy──> Streamlit(127.0.0.1:18501) ──HTTP──> API(127.0.0.1:18000)
                    ↑                        ↑
              这里有 X-Real-IP      app/web/common.py:api_request()
                                    只设置 Accept / Content-Type
```

Nginx 把真实 IP 交给了 Streamlit，但 Streamlit 服务端再去调 API 时没有转发。
API 看到的 peer 永远是 `127.0.0.1`，而 `127.0.0.1` 恰好在
`api_trusted_proxy_hosts` 里 —— 于是走到 "no X-Real-IP → 回落到 client_host"
这一支，**所有公网访客共用一个桶**。

**复现（对真实 `create_app()`，非模拟）：**

```
visitor A (one browser session) succeeded: 180/180
visitor B (a DIFFERENT person, first ever request): HTTP 429
  -> RATE_LIMITED | API rate limit exceeded: 180 requests per minute
rate-limit buckets in memory: ['127.0.0.1']
```

**放大系数。** `app/web/Home.py` 每次 rerun 都会打**未缓存**的
`/api/v1/events`（722/737 行）、`/api/v1/events/{id}`（791）、
`/api/v1/events/{id}/evidence`（793）；只有 overview / facets / product-metrics
走 `cached_api_get`。Streamlit 每次筛选、翻页、开预览都会 rerun。按每次交互
约 4 次未缓存调用估算，**全站合计**约 45 次交互/分钟就会触顶，之后
`render_api_error` 会给所有访客渲染「请求频率已受控」。

**影响：可用性，不涉及机密性或越权。** 但对一个用于演示和答辩的公网页面，
一个访客快速点几页就能让所有人看到错误态，是实打实的问题。

**建议（API 侧已经准备好了，缺的只是把头接上）：**

在 `app/web/common.py:api_request()` 里，当 `UI_ROLE == "public"` 时带上
访客 IP —— Streamlit 1.61+ 已提供 `st.context.ip_address`（本次已验证存在）：

```python
client_ip = getattr(st.context, "ip_address", None)
if client_ip:
    headers["X-Real-IP"] = str(client_ip)
```

因为 API 只接受来自 `api_trusted_proxy_hosts`（回环）的 `X-Real-IP`，且会用
`ipaddress.ip_address()` 校验后才建桶，这个改动不会引入伪造桶的风险 ——
这正是 `_rate_limit_client_key` 当初设计要支持的路径。

同时建议给 Home 的事件列表/详情/证据三个调用也套上 `cached_api_get` 的短 TTL，
把每次交互的上游调用数降下来。

---

## M-2 · 生产 Nginx 模板缺少点击劫持防护

`deployment/systemd/nginx-radar-direct.conf`（即 `install_remote.sh:1772`
的 `DIRECT_ENDPOINT_TEMPLATE`，生产实际使用的模板）设置了：

```nginx
add_header Strict-Transport-Security "max-age=31536000" always;
add_header X-Content-Type-Options    "nosniff"          always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
```

**缺少 `X-Frame-Options` 和 CSP `frame-ancestors`。** 对比之下，被文档明确
标注为「非生产、可移植备选」的 `deployment/Caddyfile` 反而设了
`X-Frame-Options "SAMEORIGIN"`。也就是说非生产形态比生产形态更严。

`tests/` 与 `scripts/` 中**没有任何一处断言安全响应头**，所以这个缺口没有
测试会发现。

**建议：** 在 `nginx-radar-direct.conf` 的 server 块加：

```nginx
add_header Content-Security-Policy "frame-ancestors 'self'" always;
add_header X-Frame-Options "SAMEORIGIN" always;
```

注意 nginx `add_header` 的继承规则：一旦某个 `location` 自己写了
`add_header`，就不再继承 server 级的头。本文件里
`location = /radar/release.json` 已经正确地重复声明了全部头，新增两条时
**必须同步加到该 location 里**，否则 `release.json` 会缺头。

另建议补一条部署验收测试，对模板做字符串断言，把这四个头钉住。

---

## L-1 · Nginx `$arg__page` 守卫可绕过（无访问影响，但不应计入控制）

`nginx-radar-direct.conf` 里：

```nginx
location /radar/ {
    if ($arg__page ~ ^(Event_Intelligence|Operations_and_Model|Adjudication_Studio)$) {
        return 404;
    }
```

这个守卫有两条独立的绕过：

1. **nginx 的 `$arg_*` 不做百分号解码，Streamlit 会解码。**
   `?_page=Event%5FIntelligence` → nginx 看到字面量 `Event%5FIntelligence`，
   正则不匹配、放行；Streamlit 解码成 `Event_Intelligence`。
2. **重复参数取值端不一致。** nginx `$arg__page` 取**第一个**值，
   Streamlit 取**最后一个**（本次已实测：重复 `_page` 时
   `st.query_params.get("_page")` 返回 `'Event_Intelligence'`）。
   更直接的是，同一份配置里的公开页重定向本身就会造出这个重复：
   ```nginx
   location ~ ^/radar/(Replay_Lab|Method_and_Boundaries)/?$ {
       return 302 /radar/?_page=$1&$args;
   }
   ```
   访问 `/radar/Replay_Lab?_page=Event_Intelligence` 会被重定向成
   `/radar/?_page=Replay_Lab&_page=Event_Intelligence`，一跳完成绕过。

**实际访问影响：无。** 真正生效的控制在应用层，而且是对的：

- `app/web/Home.py:337-348` 的 `page_targets` **按 `UI_ROLE` 构造**，
  public 角色下只含 `Replay_Lab` 和 `Method_and_Boundaries`，
  内部页根本不在映射里，`st.switch_page` 无从触发；
- 三个内部页各自在**任何 API 调用之前**调用 `require_ui_role()`
  （已核对：1_Event_Intelligence 守卫 45 行 / 首个 API 调用 109 行；
  3_Operations_and_Model 22 / 27；4_Adjudication_Studio 27 / 64）。

这一点值得单独说明：Streamlit 多页应用的页面切换走的是**已建立的
websocket**（`_stcore/stream`），不产生新的 HTTP 请求，因此 Nginx 的
路径级 404 对应用内跳转本来就无效。**内部页不可达，靠的完全是
`require_ui_role`，不是 Nginx。** 这个纵深防御是成立的。

**建议：** 保留该 `if` 作为降噪，但在配置注释里明确它不是访问控制，
避免未来有人因为「Nginx 已经挡了」而放松 `require_ui_role`。

---

## L-2 · `preview_event_id` 未编码即拼入 API 路径

`app/web/Home.py:791,793`：

```python
preview_detail  = api_request(f"/api/v1/events/{preview_event_id}")
preview_evidence = api_request(f"/api/v1/events/{preview_event_id}/evidence")["items"]
```

`preview_event_id` 直接来自 `st.query_params.get("preview_event_id")`，
未经 `urllib.parse.quote()` 就拼进 URL 路径。

**当前不可利用**，原因有三：Python `http.client` 会对含控制字符（含空格）的
请求行抛 `InvalidURL`；Starlette 不做 `..` 路径归一化；public UI 不持有任何
令牌，因此即使拼到别的端点也只会拿到 403/404。

但这是典型的「今天不是漏洞、改一行就变成漏洞」的写法。同一文件里的
`query_path()` 已经在用 `urlencode`，`components.py` 也在用 `quote()`，
这里属于遗漏。

**建议：** `api_request(f"/api/v1/events/{quote(preview_event_id, safe='')}")`。

---

## L-3 · `decrypt_file` 在 GCM 校验前落盘未认证明文

`scripts/backup_crypto.py:decrypt_file()` 在循环里逐块
`destination_handle.write(decryptor.update(chunk))`，`decryptor.finalize()`
（真正校验 GCM tag 的一步）在循环之后。也就是说最多 828 MiB 未认证明文会先
落到 `.partial` 文件上。

代码在异常路径上做了 `temporary.unlink(missing_ok=True)`，正常失败会清理。
残留窗口出现在**进程被强杀**时（SIGKILL、断电、OOM —— 备份链路本身就跑在有
`MemoryMax` 约束的 systemd 单元里），此时 `.partial` 会带着未认证明文留在盘上。

**建议：** 用 `0600` 打开临时文件（现在依赖调用方 umask），并在恢复流程入口
先清理陈留 `.partial`。流式 AEAD 的彻底解法是分块独立认证，但对当前用量
（单文件、单机、随后立即跑 `audit_migration_restore.py`）属于过度设计。

---

## L-4 · 未认证的事件详情接口每次请求都跑模型推理

`app/api/main.py:790` 在 `/api/v1/events/{event_id}` 里无条件执行
`router.predict(text, evidence_context=evidence_context)`。该端点无认证，
API 只有 `--workers 1`。

`model_shadow_output` **没有**泄露给公众（`Home.py:809` 用
`if UI_ROLE == "admin"` 门住了），所以这不是信息泄露；但每次公开预览点击都会
在单 worker 上跑一次 sklearn 推理，与 M-1 的全局桶叠加成 DoS 放大器。

**建议：** 仅在调用方具备 reviewer/operator 令牌时才计算并返回
`model_shadow_output`，公开路径跳过推理。

---

## I-1 · CI 秘密扫描没有「基础设施泄露」模式

`.github/workflows/ci.yml` 的扫描对凭据类覆盖不错（私钥、AKIA、`gh*_`、
`sk-`、`AIza`、`xox*`、Stripe、Telegram bot token、basic-auth URL、具名赋值），
但**没有任何一条针对基础设施标识符**。这正是 H-1 能穿过「转公开」这道坎的原因。

有意思的是，仓库里已经有针对性的单点断言 ——
`tests/test_systemd_install_contract.py:74` 和
`tests/test_pull_server_migration_backup_contract.py:20` 都在断言
`"18.208.34.152" not in source`。也就是说项目已经认定这个 IP 不该出现在脚本里，
只是这个判断从没推广到全仓扫描。

**建议：** 在 CI 扫描里增加（对 tracked 文件，允许一个显式白名单）：
公网 IPv4、`i-[0-9a-f]{8,17}`、`sgr-[0-9a-f]{8,17}`、`vol-[0-9a-f]{8,17}`、
`C:\\Users\\[^\\]+\\`、`ubuntu@`/`root@` + IP。

## I-2 · 443 端口裸默认 vhost 回显 nginx 版本

同主机 `443` 返回 `Server: nginx/1.28.3 (Ubuntu)`，且无任何安全响应头
（该 vhost 对所有测试路径都是 404，未暴露应用内容）。

**建议：** `server_tokens off;`，并给 443 配一个显式的 default_server
（`return 444;`），避免它成为一个未被治理的入口。

---

## 复核为「无问题」的部分

这些是本次重点看过、**确认实现是对的**的地方，列出来是为了让后续维护
不必重复怀疑：

**HTML 渲染 / XSS。** 全仓 45 处 `unsafe_allow_html=True` 全部逐个核对。
所有插值都过 `html.escape()`（Python 默认 `quote=True`，属性上下文安全）；
`state` / `tone` / `authority_class` 一律走白名单收敛；锚点用
`event_anchor_id()` 做 SHA-256 摘要而不是原始 ID；URL 一律走 `urlencode()`
或 `quote()`。事件正文来自外部源（SEC、聚合源、Telegram 频道），属于
攻击者可影响的内容，但在我检查的每一个渲染点都被转义。**未发现 XSS。**

**外链协议。** `Home.py:public_source_url()` 与
`pages/1_Event_Intelligence.py:313` 都校验 `scheme in {"http","https"}` 且
`netloc` 非空，`javascript:` / `data:` 被挡住。

**SQL。** `app/storage/` 全部参数化。`list_events` 的 `ORDER BY` 是
`sort_orders[sort]` 字典查表并预先 `if sort not in sort_orders: raise`；
`WHERE` 由固定片段拼接、值走 `?`；日期额外做 `date.fromisoformat` 往返校验。
`f"SELECT COUNT(*) FROM {table}"` 的 `table` 来自代码内常量。**未发现注入。**

**命令执行。** `app/workers/` 的 `subprocess.run` 全部 list 形式、无
`shell=True`；参数来自 `Settings`，非请求输入。

**认证。** 所有令牌比较用 `secrets.compare_digest`。
`/etc/finance-radar-public.env` 只写入 `FINANCE_RADAR_API_URL`、
`FINANCE_RADAR_UI_ROLE=public`、`FINANCE_RADAR_SHOW_DEBUG=0`，**不含任何令牌**；
`finance-radar-web.service` 还显式 `UnsetEnvironment=FINANCE_RADAR_ADMIN_TOKEN`，
以独立 UID 运行，并带较完整的 systemd 沙箱（`ProtectSystem=strict`、
`ProtectProc=invisible`、`CapabilityBoundingSet=`、`InaccessiblePaths=` 覆盖
`.env` 与共享数据目录）。人工盲审用 `LoadCredential` 而非环境变量。

**公开 API 的字段投影。** `/api/v1/health`、`/api/v1/overview` 经
`public_health_paths()` / `public_backup_status()` 把绝对路径收敛为文件名，
并把可能上兆的 manifest `components` 压成计数摘要。

**「excluded 事件可被公开预览」不是越权。** 公开 feed 的
`preview_state` 白名单本身就包含 `excluded`，`Home.py` 也为该状态准备了
面向读者的文案（「线索已排除」）—— 这是产品设计的一部分，不是 IDOR。

**依赖。** `requirements.lock` 为哈希锁定且版本较新：
streamlit 1.61.1 / fastapi 0.141.1 / starlette 1.3.1 / cryptography 50.0.0 /
urllib3 2.7.0 / requests 2.34.2 / certifi 2026.7.22。未发现明显过期的组件。

---

## 优先级建议

1. **先做不可逆性最低、收益最高的：** 收敛安全组入站、轮换 SSH 公钥与备份口令
   位置、给根卷加密（H-1 的 1–2 项）。这些不需要碰仓库历史。
2. **确认 H-2 归档的口令来源。** 这一条决定 H-2 是「遗留待清理项」还是
   「需要按数据泄露处理的事件」。
3. **补 M-1 与 M-2。** 两处都是小改动，M-1 的服务端支持已经写好了。
4. **补 I-1 的 CI 模式**，否则 H-1 类问题会再次发生。
5. 破坏性动作（删除 Release 资产、重写历史）单独决策；注意 fork 已存在，
   删除不等于撤回。
