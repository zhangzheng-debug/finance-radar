# Finance Radar current state

状态日期：2026-08-18（Asia/Singapore）

最近完整部署验收窗口：2026-08-15 05:35–05:50 UTC

最近只读运行复核窗口：2026-08-18 03:25–03:31 UTC

运行版本：`2026.08.15.4`

## 2026-08-18 AWS 与恢复现场复核

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

## 2026-08-17 仓库可见性补充核验

- GitHub API/CLI 现场返回 `zhangzheng-debug/finance-radar` 为 `PUBLIC`。
- 历史 `v2026.07.22.2` Release 仍含一份加密迁移资产；因此旧文档中的
  “private GitHub Release”假设不成立。加密不等于私有可见性。
- 本轮只修正规则和未来发布路径，没有擅自删除历史恢复资产。公开 Release 今后只放
  已确认可公开的部署、演示、模型和证据工件；生产恢复包改放独立私有存储，保留、
  迁移或删除旧资产需要单独决策。
- 以上只更新仓库/Release 可见性事实，不更新下方 2026-08-15 AWS 运行快照；服务器
  当前状态仍须重新现场核验。

## 当前结论

Finance Radar 已完成本轮仓库收敛、恢复门加固和 AWS 生产统一部署。公网只读产品可用，
API、Public Web、Worker 与每日备份计时器均已在切换后现场核验；Reviewer、Operator、
Admin 和可选 Evidence LLM 保持回环、按需或禁用状态。系统仍然没有下单、仓位、余额
或交易执行能力。

生产发布标识：

| 项目 | 已核验值 |
|---|---|
| Git 提交 | `ceb9f577b5486f6eac6a6fba5699f9e8131509df` |
| Git 标签 | `v2026.08.15.4`（解引用后精确指向上述提交） |
| Release ID | `20260815T051127Z-ceb9f577b548` |
| 生产路径 | `/opt/finance-radar/releases/20260815T051127Z-ceb9f577b548` |
| 发布归档 SHA-256 | `bebb3f69f014da02a4d66228551bae00c77c166facf17e7c36fc07c7128c2eff` |
| 公网入口 | `https://radar.18-208-34-152.sslip.io:8443/radar/` |

## 现场运行证据

- 持久部署单元结果为 `success`，激活记录为 `PASS`；Nginx、API、Public Web 和
  Worker 切换门均通过。
- API、Public Web、Worker 和 `finance-radar-backup.timer` 为 active/enabled；API
  监听回环端口 `18000`，现场为 `NRestarts=0`、当前约 96 MiB、峰值约 304 MiB。
  三种内部 UI 为 inactive/static，Evidence LLM 为 inactive/disabled。
- 切换后唯一正式备份为 `finance_radar_20260815T052950Z_9dfc2bd0`，在
  `2026-08-15T05:35:41.945384+00:00` 完成完整恢复验证；清单 SHA-256 为
  `b32d8dc49a101b886d121e2542a3b449c37feddcbdc2166f1d8fdc9014d3174b`。
- 正式备份库存恰好一份，临时 hold 库存为空，符合“每天一次；新备份验证通过后
  替换旧备份”的策略。
- 38 GiB 可用根卷在切换后使用约 26 GiB、剩余约 13 GiB；908 MiB RAM 当时约
  581 MiB 已用、327 MiB 可用，2 GiB swap 约 424 MiB 已用。
- API、Public Web 和 Worker 的 cgroup 在核验窗口内均为 `oom=0`、`oom_kill=0`、
  `high=0`。
- 低权限 API 的整体健康状态为 `degraded`，这是诚实状态而非备份失败：API 身份
  无权直接遍历 root 保护的备份实体，因此不得自行宣称 `FRESH`；特权安装流程的
  清单、哈希和隔离全恢复已独立证明该备份为 `VERIFIED`。
- 公网浏览器验收返回 HTTP 200，标题为“态势总览 · Finance Radar”，发布标记与
  Release ID 一致；总览展示 13,357 条事件、7,471 条待核验事件及中文行动提示，
  浏览器控制台无错误。

## 数据新鲜度与已知限制

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

- D 盘 Python 3.12 哈希锁定环境完整回归：`664 passed, 5 skipped, 20 subtests passed`。
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
