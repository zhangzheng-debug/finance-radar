# Finance Radar current state

状态日期：2026-08-18（Asia/Singapore）

最近完整部署验收窗口：2026-08-18 05:07–05:34 UTC

最近只读运行复核窗口：2026-08-18 05:30–05:34 UTC

运行版本：`2026.08.18.2`

## 2026-08-18 部署后候选改进（尚未发布到生产）

当前 `codex/postdeploy-product-loop` 工作分支已经完成但尚未合并、标记或部署：

- Public 首屏先渲染产品外壳，概览/筛选使用有界、按角色隔离的短缓存；刷新失败时
  只允许显示带年龄的旧快照。30 天产品指标移到事件列表之后，不再阻塞主要内容。
- 真人盲审改为独立凭据在服务端绑定主体哈希和固定角色；客户端不能自报身份或角色，
  Admin/共享 Reviewer 令牌不能冒充真人。新增 `human-blind-v3.1` 事件时点合同、
  issuer/event-chain、精确/近重复、来源分层和一次性哈希冻结门。
- 当前真实状态仍是旧合同 24 个 `OPEN` 样本、0 个真人结论；旧样本被明确列为
  contract-ineligible，新盲集保持 `NOT_READY`，没有训练、模型晋级或生产行为变化。
- 事件卡支持最高权威原始来源直达、保留筛选并返回精确卡片位置，以及仅限本次浏览
  会话的“自上次查看”状态/版本/证据变化说明。
- 本地完整回归为 `686 passed, 5 skipped`；真实浏览器桌面、键盘、事件预览/返回，
  以及 `390×844` 窄屏均通过，窄屏无横向溢出。上述结果不代表生产已运行这些改进。

## 2026-08-18 生产发布、恢复与事实完整性复核

本节取代下方 2026-08-15 发布快照和 2026-08-18 03:25–03:31 UTC 的
部署前运行快照，作为当前生产事实源：

- `v2026.08.18.2` 已在美国 `us-east-1` 主机完成事务式激活，生产链接精确指向
  `/opt/finance-radar/releases/20260818T043555Z-64a7caaeff6a`；仓库提交与标签
  均指向 `64a7caaeff6a1c7132d661d81a3a4e8453232f09`，发布归档 SHA-256 为
  `5894f2f40bd4e972e103e4fb6e4bc6e117eaea6d9abd18752fee76dfe4cbf017`。
- 部署前完整恢复点 `finance_radar_20260818T050751Z_b8ae6598` 已通过恢复门，
  收据 SHA-256 为
  `20f1126915d8a4d87f5b3774e822e070cd0cfa6032411f1f24aca1032dabbfaa`；
  切换后唯一正式在线恢复点为 `finance_radar_20260818T052124Z_0ebde600`，
  清单 SHA-256 为
  `299958f33e2d27b0cde3bf0bcec417ae077ccdede8c782e4cbcb584bfdc6b325`。
  正式日备份库存恰好一份，`weekly` 目录为空，符合“新备份完整验证成功后再替换
  旧备份、每日一份、周备份零份”的口径。
- API、Public Web、Worker 与每日备份计时器均为 active/enabled；备份 one-shot、
  Evidence LLM、Admin、Reviewer 与 Operator 均为 inactive，其中 Evidence LLM
  为 disabled，三个内部 UI 为 static/按需。Worker 为
  `NRestarts=0 / ExecMainStatus=0`，当前约 274 MiB、峰值约 281 MiB。
- 根盘为 `38 GiB`，约 `26 GiB` 已用、`12 GiB` 可用（69%）；908 MiB RAM
  约 515 MiB available，2 GiB swap 使用约 326 MiB。过去 24 小时内核日志
  没有 OOM、Out of memory 或 Killed process 记录。
- 公网 `/radar/`、`/radar/_stcore/health` 和 `/radar/release.json` 返回 200，
  release marker 精确为 `20260818T043555Z-64a7caaeff6a`；
  `/radar/offhost-status.json`、FastAPI、Admin、Reviewer 与 Operator 公网路径
  均返回 404。过期异机备份 JSON 已不再公开。
- 新版已切断常驻周期对旧 `manual_review_config` 的正式写入，并上线证据零相关门、
  发行人-事件谓词绑定和只读历史审计。生产审计没有尝试 canonical mutation：
  6,499 条旧 Evidence Agent 边被当前相关性门拒绝，4,275 条旧决策需按新合同
  重跑，2,729 条轻量核验需人工复核，105 条旧“人工核验配置”被标记为
  provenance 不可证。报告 SHA-256 为
  `ac75b5715b99c46dddcc4ee8a66848c8da70166e768141e1e30d02941ff0078f`。
- Windows 上三条 Finance Radar 计划任务仍为 Disabled，`D:\FinanceRadarBackups`
  尚不存在，因此不能宣称已有新鲜异机恢复点。新脚本已经具备隐藏/S4U、D 盘密钥与
  密文分离、单份保留和完整恢复验证，但当前本地 SSH 虽能建立 TCP 22 连接，仍在
  SSH banner 阶段超时；在取得一条经授权的可用 SSH 路径前不得启用计划任务，也
  不得删除公共 Release 中的旧恢复密文。

本次失败路径也已诚实保留：

- `20260818T040331Z-eb1c585ce812` 候选因安装器使用数据库密集的完整
  `/api/v1/health` 作为启动探针而超时，虽然后台日志证明 API 已启动，仍按门禁
  自动回滚到旧版本；其 7.3 GiB 已验证恢复包随后被独立复验并恢复为正式在线备份。
- `20260818T043555Z-64a7caaeff6a` 的第一次尝试在诊断中被人工 `Ctrl+Z` 暂停，
  使已完成备份的 `systemd-run` 桥接子进程成为僵尸。该次尝试在切换前被明确终止，
  Worker 抑制标记清除、旧版本恢复；失败代码目录仅改名保留，未删除恢复数据。
  最终重试全程不再暂停，并通过部署前恢复门、切换后恢复门、服务/cgroup、Nginx、
  公网 deny-list 与 release marker 的完整验收。

## 2026-08-18 部署前 AWS 与恢复现场复核（历史快照）

核验窗口：2026-08-18 03:25–03:31 UTC。以下是本次重新取得的现场证据，
不是对 2026-08-15 快照的推断：

- 美国 `us-east-1` 的 `i-0fa9bfafa5eab00bf`（`us-vpn-news-1`）正在运行，
  实例类型 `t3.micro`，状态检查 `3/3` 通过，可用区 `us-east-1c`；唯一 EIP
  `18.208.34.152` 仍绑定该实例。
- 唯一根卷为 `vol-0ee52134d18962a6c`，`gp3 40 GiB / 3000 IOPS /
  125 MiB/s`，状态正常、正在使用；AWS 控制台显示该卷未加密。主机内约
  `26/38 GiB` 已用（69%），约 12 GiB 可用。
- Nginx、API、Public Web、Worker 与每日备份计时器均为 active/enabled；
  Worker 为 `NRestarts=0 / ExecMainStatus=0`，当前约 369 MiB、峰值约
  382 MiB。主机约 444 MiB 内存可用，2 GiB swap 使用约 412 MiB，过去
  24 小时内核日志未发现 OOM。
- 最新服务器备份 `backup-4dffdf3c1c304113936a7ffb9e2aa049` 在
  `2026-08-17T05:43:09Z` 完成完整恢复验证，`quick_check=ok`，恢复包约
  7.67 GB；恢复计数为 13,368 个事件、13,841 条证据、22,554 个事件版本。
  备份单元明确使用 `--retention 1 --weekly-retention 0`，上次结果 success，
  下一次定时运行计划在 2026-08-18 05:46 UTC 左右。
- 新加坡 `ap-southeast-1` 现场清单为：EC2 0、EBS 卷 0、EIP 0、账户自有
  EBS 快照 0。未重新创建已退出的新加坡拓扑。
- 公网 `/radar/` 与 Streamlit health 返回 200；FastAPI、Admin、Reviewer、
  Operator 公网路径均返回 404。生产仍是 release
  `20260815T051127Z-ceb9f577b548` / `2026.08.15.4`。

本次同时确认两个未闭合边界：

- Worker 仍持续采集，但最新周期因一个失效原始页面 404（以及个别 SEC 请求
  超时）返回 `DEGRADED`；这不等于 Worker 停止，也不能被写成完全健康。
- 三个 Windows Finance Radar 计划任务仍为 Disabled，避免终端弹窗；
  `D:\FinanceRadarBackups` 当前不存在。公网 `/radar/offhost-status.json` 仍
  暴露 2026-07-22 的过期 `VERIFIED` JSON，因此它不是当前异机恢复证明。
  在隐藏任务、密钥分离、单份保留和新鲜 D 盘恢复验证完成前不得重新启用旧任务，
  也不得先删除公共 Release 中可能仍是唯一异机副本的旧密文。

## 2026-08-17 仓库可见性补充核验（历史快照）

- GitHub API/CLI 现场返回 `zhangzheng-debug/finance-radar` 为 `PUBLIC`。
- 历史 `v2026.07.22.2` Release 仍含一份加密迁移资产；因此旧文档中的
  “private GitHub Release”假设不成立。加密不等于私有可见性。
- 本轮只修正规则和未来发布路径，没有擅自删除历史恢复资产。公开 Release 今后只放
  已确认可公开的部署、演示、模型和证据工件；生产恢复包改放独立私有存储，保留、
  迁移或删除旧资产需要单独决策。
- 当时只更新仓库/Release 可见性事实；服务器现状已经由本文最上方
  2026-08-18 05:30–05:34 UTC 的现场窗口重新核验。

## 当前结论

Finance Radar 已完成本轮仓库收敛、恢复门加固和 AWS 生产统一部署。公网只读产品可用，
API、Public Web、Worker 与每日备份计时器均已在切换后现场核验；Reviewer、Operator、
Admin 和可选 Evidence LLM 保持回环、按需或禁用状态。系统仍然没有下单、仓位、余额
或交易执行能力。

生产发布标识：

| 项目 | 已核验值 |
|---|---|
| Git 提交 | `64a7caaeff6a1c7132d661d81a3a4e8453232f09` |
| Git 标签 | `v2026.08.18.2`（解引用后精确指向上述提交） |
| Release ID | `20260818T043555Z-64a7caaeff6a` |
| 生产路径 | `/opt/finance-radar/releases/20260818T043555Z-64a7caaeff6a` |
| 发布归档 SHA-256 | `5894f2f40bd4e972e103e4fb6e4bc6e117eaea6d9abd18752fee76dfe4cbf017` |
| 公网入口 | `https://radar.18-208-34-152.sslip.io:8443/radar/` |

## 现场运行证据

- 持久部署单元结果为 `success`，激活记录为 `PASS`；Nginx、API、Public Web 和
  Worker 切换门均通过。
- API、Public Web、Worker 和 `finance-radar-backup.timer` 为 active/enabled；API
  监听回环端口 `18000`，Worker 现场为 `NRestarts=0 / ExecMainStatus=0`、当前
  约 274 MiB、峰值约 281 MiB。三种内部 UI 为 inactive/static，Evidence LLM
  为 inactive/disabled。
- 切换后唯一正式备份为 `finance_radar_20260818T052124Z_0ebde600`，已完成
  双数据库恢复验证；清单 SHA-256 为
  `299958f33e2d27b0cde3bf0bcec417ae077ccdede8c782e4cbcb584bfdc6b325`。
- 正式备份库存恰好一份，临时 hold 库存为空，符合“每天一次；新备份验证通过后
  替换旧备份”的策略。
- 38 GiB 根卷在切换后使用约 26 GiB、剩余约 12 GiB（69%）；908 MiB RAM
  约 515 MiB available，2 GiB swap 约 326 MiB 已用。
- API、Public Web 和 Worker 的 cgroup 在核验窗口内均为 `oom=0`、`oom_kill=0`、
  `high=0`。
- 低权限 API 的整体健康状态为 `degraded`，这是诚实状态而非备份失败：API 身份
  无权直接遍历 root 保护的备份实体，因此不得自行宣称 `FRESH`；特权安装流程的
  清单、哈希和隔离全恢复已独立证明该备份为 `VERIFIED`。
- 公网验收中主页、Streamlit health 与 release marker 返回 HTTP 200，发布标记与
  Release ID 一致；过期 off-host JSON、FastAPI 与三个内部 UI 路径全部返回 404。

## 2026-08-15 数据新鲜度与已知限制（历史快照）

公网验收时页面显示最近成功采集约 5.8 分钟前、最近发现新事件约 5.7 小时前；
“最近采集异常”表示至少一个来源在最近周期降级，不等于 Worker 停止。最后一次
事件更新时间为 `2026-08-15T00:02:51.034134+00:00`。

本轮发现的主要体验债务是首次打开总览时后台聚合偏慢。导航和页面框架先出现，
事件主区随后加载；浏览器最终完整呈现且无脚本错误，但健康/总览查询应在下一阶段
改为有界缓存、分段加载并加入明确进度与超时说明。

生产切换后的首三个 Worker 周期分别在约 05:41、05:47 和 05:53 UTC 完成，状态均为
`SUCCESS`、进程返回码为 0。通过 AWS 浏览器管理通道对同期 journal 做了定向扫描，
`database is locked`、lease heartbeat 和 traceback 均未命中，结果为 `LOCKSCAN=CLEAN`。

## 验证与恢复边界

- 当前 `v2026.08.18.2` 源码完整回归：`680 passed, 5 skipped`；发布归档、清单、
  依赖锁、Bash/PowerShell 语法与隔离解包验证均已通过。
- 2026-08-15 的 D 盘 Python 3.12 历史回归为：
  `664 passed, 5 skipped, 20 subtests passed`。
- 发布归档在源目录和隔离解包目录均通过 release audit；解包候选定向验证为
  `44 passed, 5 skipped, 3 subtests passed`，运行与开发依赖锁均通过。
- GitHub PR #4–#9 均已合并，每个精确候选提交的两项 Actions 检查均通过。
- 早期 `.2` 候选在公开账户无法读取 `VERSION` 时于切换前安全终止并回滚；`.3`
  候选在长 SSH 输出通道中断后由已验证备份完成保全与状态对账。这两次真实失败路径
  促成 `.4` 的权限修复、持久部署单元和受限日志输出，不能被包装成一次无故障发布。
- 正式标签 `v2026.08.15.4` 已精确指向生产代码提交；GitHub Release 为非草稿、
  非预发布状态，5 个恢复资产均为 `uploaded`，其中 TGZ 的远端 digest 与本地及生产
  发布记录一致。`v2026.07.22.2` 保留为上一份历史恢复标签。

详细的目的偏差、工程发现和整改优先级见
[`reports/PROJECT_ALIGNMENT_AUDIT_2026-08-13.md`](reports/PROJECT_ALIGNMENT_AUDIT_2026-08-13.md)。
