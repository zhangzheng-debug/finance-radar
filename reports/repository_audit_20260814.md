# Finance Radar 仓库全量审计

- 审计日期：2026-08-14
- 受检提交：`7b1c5d1`（main）
- 审计范围：663 个文件 / app 6,959 行 / scripts 30,165 行 / tests 10,014 行 / 8 个提交 / 4 个 PR
- 执行方式：全量测试运行、覆盖率测量、凭证与危险调用静态扫描、SQL 与 HTML 转义逐处人工核查、限流缺陷运行时探针验证
- 测试结果：391 通过 / 1 失败；`app/` 覆盖率 80%

结论摘要：工程质量显著高于同类实习项目。问题集中在三处**日历驱动的活动故障**，而非架构缺陷。

| 等级 | 数量 |
| --- | --- |
| 须立即处理 | 3 |
| 应当修复 | 4 |
| 工程卫生 | 6 |
| 确认做得好 | 8 |

---

## A. 须立即处理

### A-01 测试时间炸弹（CI 即将转红）— 已实证

`tests/test_official_event_collector.py:151` 的 RSS 夹具把 `pubDate` 写死为
`Wed, 15 Jul 2026 18:00:00 GMT`，而 `scripts/official_event_collector.py:368` 的
`entry_is_recent()` 以 `datetime.now()` 计算 30 天时效闸门。夹具因此有保质期。

```
现在(UTC): 2026-08-14T23:43:08+00:00
fixture pubDate: 2026-07-15T18:00:00+00:00
已过天数: 30.238            # 闸门阈值 30
entry_is_recent(max_age_days=30) = False
→ 条目被过滤，observation_jobs 为空，断言取到 None
```

失败形式：`TypeError: 'NoneType' object is not subscriptable`。
分水岭为 **2026-08-14 18:00 UTC**。CI 上次跑绿是 8/13，因此当前仍显示绿色，**下一次 push 必红**。

修复：夹具 `pubDate` 改为相对当前时间生成，或为闸门注入可控时钟。
仓库已有先例——提交 `85131b6`「make v4 router checks hermetic in CI」修的是同一类问题。

### A-02 API 限流可被请求头完全绕过 — 已实证

`app/api/main.py:139` 取 `X-Forwarded-For` 的**第一段**作为限流键：

```python
forwarded = request.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
key = forwarded or client_host
```

而 `deployment/systemd/nginx-radar-direct.conf:41` 使用 `$proxy_add_x_forwarded_for`，
该指令将真实 IP **追加在客户端自带值之后**，因此攻击者伪造值恒定排在首位。
当前部署下可直接利用：

```
[1] 固定来源 8 次   -> [404,404,404,404,404,429,429,429]   限流生效
[2] 轮换 XFF 60 次  -> 429 出现 0 次 / 60                  限流完全失效
```

修复：改用 nginx 已设置的 `X-Real-IP`（值为 `$remote_addr`，客户端不可伪造），
或取 XFF 的**最后**一段。

### A-03 README「在线成品」指向已下线服务器

`README.md:39` 挂新加坡 VPS `radar.167-172-69-16.sslip.io`，
但 `reports/aws_migration_20260721.md` 记录服务已于 7/21 迁移至 AWS `18.208.34.152`。

运维脚本、v6 计划书、nginx 配置均已更新，唯独 README 未更新。
同样过期的还有 `deployment/README.md` 与 `.agent/deployment_runbook.md`。

修复：更新这三处主机地址，并确认新地址当前可访问。

---

## B. 应当修复

### B-01 限流计数器无上限增长 — 已实证

`app/api/main.py:130` 的 `rate_buckets: defaultdict` 只增不减，无淘汰逻辑。
探针发出 61 个不同来源后字典稳定停在 61 键。与 A-02 叠加后可被无限伪造 IP 撑爆内存。

修复：周期性清理空队列键，或改为带容量上限的 LRU。

### B-02 管理令牌使用非常量时间比较

`app/api/main.py:205` 使用 `!=` 比较令牌，存在计时侧信道。
远程利用难度高，但修复成本为一行。

修复：`secrets.compare_digest(x_admin_token or "", settings.admin_token)`。

### B-03 版本标识三处不一致

| 来源 | 值 | 实际作用 |
| --- | --- | --- |
| `VERSION` | 2026.07.22.1 | 无任何代码读取（死文件） |
| `app/__init__.py:3` | 0.1.0 | **API 对外真正上报的值** |
| `release/RELEASE_NOTES_2026.07.22.2.md` | 2026.07.22.2 | 发布记录 |

`__version__` 同时作为 FastAPI `version=` 与 `/api/v1/health` 的 `service_version`，
即健康检查对外回答 `0.1.0`，与仓库全部文档矛盾。

修复：`app/__init__.py` 读取 `VERSION` 文件作为唯一事实源。

### B-04 常驻生产的 worker 零测试覆盖

| 模块 | 语句 | 覆盖率 |
| --- | --- | --- |
| `app/workers/notifier.py` | 26 | 0% |
| `app/workers/backup_scheduler.py` | 30 | 0% |
| `app/workers/continuous.py` | 81 | 49% |
| `app/api/main.py` | 287 | 85% |
| `app/storage/operations.py` | 315 | 91% |

备份调度器是数据安全的最后一道防线，目前一行未被测试覆盖。

---

## C. 工程卫生

### C-01 生产基础设施细节入库

仓库当前为 private，非正在发生的泄露；但转公开或对外交付前必须清理。

| 内容 | 出现范围 |
| --- | --- |
| AWS 公网 IP `18.208.34.152` | 10 个文件 |
| EC2 实例 ID `i-0fa9bfafa5eab00bf` | 3 个文件 |
| SSH 登录 `ubuntu@…` 写死为默认参数 | 4 个 `.ps1` |
| 本地私钥路径 `C:\Users\MR\.ssh1\id_ed25519` | 7 个文件 |
| 旧 VPS IP `167.172.69.16` | 58 个文件 |

修复：主机地址与登录名改由环境变量提供，脚本默认值留空。历史提交中亦存在，
彻底清除需重写历史；对 private 仓库可先改现状，转公开前再评估。

### C-02 构建产物撑大仓库，其中 9.8 MB 为重复内容

`reports/` 占 35 MB / 321 文件（全仓 45 MB）。`docx_qa_*` 快照 27.3 MB，按内容哈希去重后：

```
docx_qa 快照总计: 27.3 MB
其中重复内容:   9.8 MB (36%)    # 44 个文件是别处副本
去重后仅需:     17.5 MB
```

`v5_1` 单个版本保留了 6 份近乎相同的渲染快照。
另 `coverage.json` + `coverage.xml`（668 KB）为 CI 可重生成产物，不应入库。

### C-03 依赖未锁定，构建不可复现

13 个依赖中 9 个为开放下限约束（`>=`），无锁文件。
CI 直接 `pip install -r requirements-dev.txt`，同一代码在不同日期装出不同依赖树。
本次审计解析到 `pandas 3.0.5`，相对 `requirements.txt` 的 `pandas>=2.2` 已跨大版本。
测试在该版本下仍全部通过（A-01 除外），说明代码本身健壮，但属运气而非保证。

修复：生成 `requirements.lock`，CI 安装锁文件；另设定时任务跑最新版做前瞻检测。

### C-04 提交历史为「备份式」而非「开发式」

全仓 8 个提交，首提交 590 文件 / 109,525 行，第三个提交 91 文件 / 17,322 行。
功能无影响；但作为作品集或实习评估材料时，历史无法体现开发过程。

建议：后续改动走小步提交；已有历史不必回改。

### C-05 PR #4 停滞三周

「Harden evidence governance, public UI, and recovery cutover」7/23 开启，
8/13 最后推送，至今未合并，CI 为绿。分支含 `main` 所无的工作。
注意其合并后同样会撞上 A-01。

### C-06 `.gitignore` 末尾混入 `-w`

`.gitignore:24` 为孤立一行 `-w`，系命令行参数误粘。无害，删除即可。

---

## D. 确认做得好的部分

以下结论均经逐处核查，非泛泛评价。

1. **SQL 全部参数化。** `app/` 与 `scripts/` 每条 `execute` 均已核查；少数 f-string 仅拼接
   硬编码表名常量与占位符个数，用户输入一律走 `?`。零注入面。
2. **无任何凭证泄露。** AWS / OpenAI / GitHub / Slack / Telegram bot token / PEM 私钥等
   格式与高危关键字全扫，当前文件与 git 历史均干净，`.env` 从未被提交。
3. **CI 内置边界强制断言。** 除跑测试外，断言仓库不出现明文令牌，且 `app/` 中不存在
   orders / positions / trade 端点——将「只读」产品承诺转化为机器可验证约束。
4. **备份加密实现正确。** AES-256-GCM + scrypt(n=2^15)、随机 salt/nonce、
   header 参与 AAD 认证、`os.replace` 原子落盘、口令长度下限校验。
5. **HTML 转义无遗漏。** 18 处 `unsafe_allow_html` 插值逐个核对，全部经 `html.escape`；
   唯一未转义的 `href` 为常量拼 `quote()`。无 XSS 缺口。
6. **无危险原语。** 全仓无 `eval` / `exec` / `pickle.loads` / `shell=True` / `yaml.load`，
   无 `verify=False` 关闭 TLS 校验。
7. **API 设计规范。** 统一响应信封、trace_id 贯穿、结构化错误码、`nosniff` / `no-store`
   安全头、CORS 收窄至 localhost；未配置管理令牌时写端点直接 503——失败方向安全。
8. **容器与技术债。** Dockerfile 用 slim 基础镜像、非 root 用户（uid 10001）运行。
   全仓 `TODO/FIXME/HACK` 仅 1 处，且位于某检查工具的关键词表内，非真实技术债。

---

## E. 建议处理顺序

1. 修 A-01 时间炸弹（约十分钟；不修则下次 push 即红，且原因不直观）
2. 改 A-03 README 链接（约五分钟；被外人看见概率最高）
3. 修 A-02 + B-01 限流（同一中间件，一并处理）
4. 清掉 B-02 与 C-06（各一行）
5. 统一 B-03 版本号
6. 决定 PR #4 去留（C-05）
7. 其余按需：依赖锁定（C-03）与补 worker 测试（B-04）价值最高；
   仓库瘦身（C-02）可留待转公开时一并处理

---

## 附：审计执行说明

- 测试在 Python 3.11 虚拟环境中运行；Telethon 因 `pyaes` 在本环境构建失败而跳过，
  不影响被测模块（Telethon 仅用于 MTProto 采集脚本）
- 标注「已实证」的结论均由实际运行复现；未标注者基于代码与仓库证据推断
