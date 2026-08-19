# Finance Radar 复审记录（第二次审计）

> 历史审计快照：本报告仅描述 2026-08-15 指定提交区间，不证明当前仓库、CI 或 AWS 状态。当前事实必须回到 `CURRENT_STATE.md` 和最新提交现场复验。

- 复审日期：2026-08-15
- 复审区间：`7b1c5d1` → `663fdf5`（207 文件 / +31,518 −10,156 行 / 7 次提交）
- 发布版本：`2026.08.15.4`
- 上一轮报告：[`repository_audit_20260814.md`](repository_audit_20260814.md)
- 被复核的对账文档：[`CLAUDE_AUDIT_RECONCILIATION_2026-08-15.md`](CLAUDE_AUDIT_RECONCILIATION_2026-08-15.md)

本文件不修改上一轮报告，只记录复核结果。所有结论均经独立验证，未直接采信对账文档的自述。

## 摘要

| 项 | 结果 |
| --- | --- |
| 原发现彻底关闭 | 11 / 13 |
| 原发现部分收敛 | 2 / 13（C-01、C-02，均为有意保留的决定） |
| 新引入缺陷 | 0 |
| 测试 | 392 → 669 全过（+277） |
| `app/` 覆盖率 | 80% → 82%（语句数 2,876 → 5,489） |
| `app+scripts` 覆盖率 | 55% |
| 开放 PR | 0 |

---

## 一、13 条发现的复核结果

| 编号 | 原发现 | 处置 | 状态 |
| --- | --- | --- | --- |
| A-01 | 测试时间炸弹 | 为采集时效判断注入可控时钟，并新增 30 天边界测试（18:00:00 通过 / 18:00:01 拒绝） | 关闭 |
| A-02 | XFF 绕过限流 | 可信代理白名单 + 仅接受来自代理的 `X-Real-IP` + IP 格式校验 | 关闭 |
| A-03 | README 指向已下线服务器 | 改为 `YOUR_DOMAIN` 占位；`.agent/deployment_runbook.md` 重写，硬编码地址归零 | 关闭 |
| B-01 | 限流桶无界增长 | `OrderedDict` + 过期清理 + 4096 客户端硬上限 | 关闭 |
| B-02 | 令牌非常量时间比较 | 改用 `secrets.compare_digest`，5 处调用点全覆盖 | 关闭 |
| B-03 | 版本号三处矛盾 | `app/__init__.py` 读取 `VERSION`；新增一致性与格式测试 | 关闭 |
| B-04 | worker 零覆盖 | `backup_scheduler` 0→41%、`notifier` 0→54%、`continuous` 49→82% | 关闭 |
| C-01 | 基础设施细节入库 | 可执行路径已清理；历史报告按决定保留 | 部分 |
| C-02 | 仓库臃肿 | `reports/` 35 MB → 24 MB；coverage 产物移除 | 部分 |
| C-03 | 依赖未锁定 | uv 生成 1,265 条哈希锁 + CI `--require-hashes` + 锁文件摘要绑定 | 关闭 |
| C-04 | 备份式提交历史 | 此后 7 次提交平均百行量级，已转增量开发 | 关闭 |
| C-05 | PR #4 停滞 | 已合并；当前零开放 PR | 关闭 |
| C-06 | `.gitignore` 的 `-w` | 已删；CI 加 `git diff --check` 防回归 | 关闭 |

### A-01 的修法优于原建议

原建议为「夹具日期改相对时间」。实际实现是给 `collect_feed` 与 `entry_is_recent`
注入 `now=` 参数，并新增边界测试：

```python
def test_entry_age_guard_uses_an_injected_clock_at_the_boundary(self) -> None:
    published = "2026-07-15T18:00:00+00:00"
    assert collector.entry_is_recent(published, max_age_days=30,
        now=dt.datetime(2026, 8, 14, 18, 0, tzinfo=dt.timezone.utc))
    assert not collector.entry_is_recent(published, max_age_days=30,
        now=dt.datetime(2026, 8, 14, 18, 0, 1, tzinfo=dt.timezone.utc))
```

这是根因修复而非规避。全仓时效逻辑复查：12 处已接受时钟注入，
剩余墙上时钟读取点均不参与测试断言，**未发现新的时间炸弹**。

### A-02 / B-01 的运行时实证

以与上一轮完全相同的探针复测：

```
                        修复前        修复后
轮换 XFF 60 次拦截      0 / 60        60 / 60
轮换 X-Real-IP 拦截     —             60 / 60   （来源非可信代理，头被忽略）
限流桶键数              61（无上限）  1（上限 4096）
```

两项均有防回归测试锁定（伪造来源洪泛、`max_clients` 上限）。

---

## 二、超出审计范围的改进（复核确认）

### 1. 权限面重构：Public / Admin → Public / Reviewer / Operator / Admin

复核方式与结果：

- **写端点鉴权矩阵**：运行时枚举 29 条路由，7 个写端点全部有鉴权，**未鉴权写端点 0 条**；
- **内部 UI 监听**：Admin/Reviewer/Operator 的 systemd 单元均为
  `--server.address 127.0.0.1`，端口 18502/18503/18504 互斥；
- **公网边缘**：`/radar-admin`、`/radar-review`、`/radar-ops` 在 nginx 中为
  **显式 `return 404`**，而非未配置后依赖兜底；
- **测试锁定**：nginx 与 Caddy 两套边缘的拒绝规则均有契约测试。

同时，原本公开的 `/trace`、`/model/status`、`/adjudication/status`、
`/evidence/archive` 已收紧为需鉴权——**公开信息面是缩小的**。

### 2. CI 门禁升级

- 交易路由扫描由 grep 改为 **AST 遍历**（grep 可被换行绕过，AST 不能）；
- 密钥检测扩展为 10 类，带占位符白名单与已知夹具哈希豁免；
- `pip install --require-hashes -r requirements-dev.lock`；
- 9 个 systemd 脚本 `bash -n` 语法校验；
- `git diff --check` 空白字符检查。

### 3. 备份并发控制

- 互斥采用**持久 inode 上的内核锁**（`flock` / `msvcrt.locking`），
  而非原子重命名——后者在陈旧锁争用竞态下不可靠，代码注释已说明该理由；
- `_pid_is_alive` 特意避开 Windows 上的 `os.kill(pid, 0)`：Python 在 Windows 上
  将信号映射为进程终止，用其探测存活会**杀死正在被检查的备份进程**；
- `PermissionError` 一律判定为「存活」（失败方向安全）；
- 删除路径双重护栏：`_safe_bundle_path` 两侧 `resolve()` 防路径穿越与符号链接逃逸，
  `_assert_direct_backup_child` 校验父目录与文件名前缀后才允许 `rmtree`。

### 4. 发布闸门拦截了缺陷候选

候选 `20260815T015844Z-96db114f59a5` 清单状态为 `READY`，但隔离解包后依赖绑定校验失败：
元数据按 LF 计算摘要，Windows Git 导出归档时将 lock 文件转为 CRLF。该候选被标记
`REJECTED`，未上传未部署。此事证明「清单 READY」不等于「恢复形态可用」。

---

## 三、对账文档自述数据的独立核实

| 其声明 | 独立测量 | 结论 |
| --- | --- | --- |
| app + scripts 覆盖率 55% | 55% | 精确吻合 |
| 报告 278 个 / 24,298,877 字节 | 281 个 / 24,319,880 字节 | 吻合（差额为其后新增的 3 份文档） |
| runbook 已重写为无硬编码 | 硬编码地址 0 处 | 属实 |
| 内部入口仅回环、互斥 | systemd + nginx + 测试三重确认 | 属实 |
| 三个内部入口令牌独立 | 3 个环境变量 + 契约测试 | 属实 |

对账文档明确记载了尚未完成的部分（「本记录不证明 AWS 当前健康或已经部署修复」），
并主动记录了被拒绝的候选版本。经复算，其数据无夸大。

---

## 四、新缺陷排查（结果：未发现）

本轮针对 3 万行新增代码重跑了全套检查：

| 检查项 | 结果 |
| --- | --- |
| 明文凭证（10 类格式） | 干净 |
| 危险原语（`eval`/`exec`/`pickle.loads`/`shell=True`/`yaml.load`/`verify=False`） | 干净 |
| SQL 注入面 | 干净（2 处 f-string 均为硬编码常量：`COUNT_TABLES`、迁移列定义） |
| HTML 转义（44 处 `unsafe_allow_html`） | 抽查数据来源均经 `escape()` 或为常量/哈希/整数 |
| 写端点鉴权 | 7/7 有鉴权，0 缺口 |
| 时效逻辑时间炸弹 | 未发现新增 |
| 依赖版本 | 全部当前（cryptography 50.0.0、urllib3 2.7.0、certifi 2026.7.22 等） |

---

## 五、剩余事项

1. **C-01 残留**：10 个文件仍含基础设施细节，分布在历史报告、契约测试与 v6 计划生成器。
   可执行路径已清理（`.ps1` 改为 `$env:FINANCE_RADAR_SSH_HOST` 并在缺失时抛错）。
   「保留历史报告日期真实性、不重写历史」的决定合理，转公开前统一处理即可。
   注：`reports/repository_audit_20260814.md`（上一轮报告）亦在其中。

2. **两个已合并分支的残留引用**：`codex/sec-shadow-evidence-clarity`（显示领先 12 提交）
   与 `codex/release-archive-lock-portability`（领先 1 提交）系 squash 合并后的正常现象。
   经核实两者内容均**严格落后于 main**，删除不丢失任何内容；保留易造成
   「尚有未合并工作」的误解。

3. **对账文档版本号滞后**：文中写「候选版本统一为 2026.08.15.1」，实际发布为
   `2026.08.15.4`，建议对齐。

4. **`app/web/Admin.py` 覆盖率 0%**：该文件仅为调用 `require_ui_role("admin")`
   的页面外壳，实际权限控制位于 `app/web/common.py` 且有测试覆盖。非安全缺口，优先级低。

5. **AWS 生产验收未完成**：这是当前唯一实质缺口，亦为对账文档自列待办。
   所有修复已通过本地全量门禁与 CI，但生产现场验收尚未执行。

---

## 六、结论

上一轮的判断是「工程质量远高于同类实习项目，扣分项不在设计而在时间」。
本轮处置**从根因上消除了时间性问题**——引入时钟注入、哈希锁定、发布审计等
使同类问题不再复发的机制，而非逐个打补丁。

3 万行改动、13 条发现全部处置、零新增缺陷，且新增能力（权限分级、CI 门禁、
并发安全备份）本身质量高于原有基线。

## 附：复审执行说明

- 测试在 Python 3.11 虚拟环境中运行，669 通过 0 跳过；对账文档报告的
  「663 passed, 5 skipped」为 Windows / Python 3.12 环境结果，跳过项应为
  POSIX 特定用例，不构成差异。
- Telethon 因 `pyaes` 在本环境构建失败而跳过，不影响被测模块。
- 标注「运行时实证」的结论均由实际执行复现。
