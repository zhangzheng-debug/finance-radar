# Finance Radar current state

## 2026-08-23 `2026.08.23.5` 本地发布候选（尚未代表 AWS）

分支 `codex/event-quality-recovery` 已完成本阶段后端收口。公共事件流现在浏览
全部 canonical 记录，但把“证据强度”“风险路由”和内部工作进度拆成独立语义：
只有当前版本达到可引用合同才返回结构化事实句；其他记录只返回有界且明确标记为
未核验的来源捕获摘录。风险路由仍是 `SHADOW` 复核优先级，不是真假、严重度、
交易方向或自动发布许可。

本候选同时完成个人 Reviewer 身份绑定、跨凭据碰撞拒绝、公开访客 IP 的可信转发、
生产/重放证据语义对齐、严格 P0/P1 枚举、内部 UI 的单会话启动器、只读老板总览、
DeepSeek systemd credential 隔离、AES-GCM 解密原子落盘、点击劫持响应头，以及
部署期间对五分钟解读任务的事务式停启。解读预算继续按所有者决定保持日金额与日
请求上限为 `0`（不设上限），但单批、并发、超时、重试和输出 token 仍有硬边界。

公共 API 事实槽 fail-closed 修复后的最终本地完整回归为
`1058 passed, 6 skipped`；相关定向回归 `96 passed`，安装脚本 `bash -n`、
`compileall`、依赖锁校验和 `git diff --check` 均通过。正式发布仍需从干净提交
生成归档并核对 manifest、标签与归档提交一致性。

本节只证明本地候选，不证明当前生产版本、服务、定时器、备份、磁盘、数据库或
公网响应。下方生产段落均为历史现场快照，可能过期；只有后续部署与现场验收结果
可以更新 AWS 结论。

## 2026-08-22 本地未发布候选（不代表当前 AWS 状态）

本地分支 `codex/event-quality-recovery` 已完成一轮可在仓库内验证的收口：API 捕获内容解读增加原子用量记账、失败计费、租约/退避和默认禁用的定时 Worker；按产品决策，大模型日金额与日请求配置均以 `0` 明确表示不设上限，但批量、并发、超时、重试与最大输出仍有硬边界；行情默认路径改为指定事件分钟的 OHLCV bar 并核验供应商时间戳；金融规则扩展为 12 个事件家族、24 张确认/误判卡，新增 FTS5 检索和三项带来源引用的确定性计算器；真人金标可直接转换为不泄露盲测标签/正文的模型输入；Public 聚合读取增加有界短缓存。

本地完整回归：`981 passed, 5 skipped`；`git diff --check` 通过。该结果没有核验、部署或改变 AWS，没有启用外部模型定时器，没有训练/晋级模型，也没有改变 canonical 事件。下方生产段落是历史现场快照，可能已经过期；当前服务器版本、服务、磁盘和公网状态必须在下一次发布前重新现场核验，不能从本地测试推断。

状态日期：2026-08-19（Asia/Singapore）

最近完整部署验收窗口：2026-08-19 06:25–06:54 UTC

最近只读事实完整性复核：2026-08-19 06:56–06:57 UTC

运行版本：`2026.08.19.1`

## 2026-08-19 生产发布与历史事实完整性审计

本节取代下方 2026-08-18 快照，作为当前生产事实源。它只记录本次现场复核能够
证明的事实，不把仓库 CI、旧截图或历史文档当作当前服务器状态：

- `v2026.08.19.1` 已在美国 `us-east-1` 的 `i-0fa9bfafa5eab00bf`
  （`us-vpn-news-1`）完成事务式激活。生产链接精确指向
  `/opt/finance-radar/releases/20260819T062521Z-fb9b61fb0aa0`，版本为
  `2026.08.19.1`，仓库提交为 `fb9b61fb0aa0`。发布归档 SHA-256 为
  `1e63aadb31737946baae8fc7faa4bf180dbd6e64de7d54cb08b8dc4cda66f930`，
  发布清单 SHA-256 为
  `dff60841e1d8bf5e7a0b02253c0e7ca114cdb2dfdb8d8cb00ef4794da463089b`。
- 部署前完整恢复点 `finance_radar_20260819T062743Z_bd47be6f` 已通过恢复门，
  收据 SHA-256 为
  `aaa8249f1b68ad596ba4221b82df889e70a2c682bf6f646784e75bf7057cfa6e`；
  切换后唯一正式在线恢复点为 `finance_radar_20260819T064350Z_faa0c011`，
  清单 SHA-256 为
  `b931aaae79d261e1f7abc074b16ad6ce936317e583edb16873a09736d654669a`。
  新恢复点独立验证成功后旧恢复点才被移出保留链；正式在线库存仍恰好一份，
  recovery hold 只余 `12 KiB` 元数据。
- Nginx、API、Public Web、Worker 与每日备份计时器均为 `active/enabled`；
  Worker 为 `NRestarts=0 / ExecMainStatus=0`，当前约 188 MiB、峰值约 378 MiB。
  根盘为 `38 GiB`，部署双备份峰值曾到 93%，验证和单份轮换后回落到
  `27 GiB` 已用、`12 GiB` 可用（70%）。主机约 531 MiB 内存可用，2 GiB
  swap 使用约 490 MiB。下一次每日备份计划为 2026-08-20 06:50:59 UTC。
- 公网 `/radar/`、`/radar/_stcore/health` 和 `/radar/release.json` 返回 200，
  release marker 为 `20260819T062521Z-fb9b61fb0aa0`；FastAPI、Admin、
  Reviewer、Operator、`/radar/offhost-status.json` 与退役的旧工作台路径均
  返回 404。
- Worker 部署后周期从 2026-08-19 06:54:28 UTC 运行到 06:55:51 UTC，
  因一个已失效的原始页面返回 404 而诚实标为 `DEGRADED`；Worker 没有退出。
  周期中的旧人工配置处置为
  `LEGACY_REVIEW_CONFIG_UNPROVEN_PROVENANCE`，`applied=0`、
  `formal_mutation_attempted=false`。这不是“全部数据源健康”，也不是正式结论写入。
- `fact-integrity-history-audit-v2` 已在生产数据库只读复跑，明确记录
  `read_only=true` 与 `canonical_mutation_attempted=false`。11,045 个历史
  Evidence Agent 决策中，271 个符合当前合同、4,275 个需按新合同重跑，
  6,499 个决策至少含一条被当前关联门拒绝的旧边；被拒旧边总数也是 6,499。
  2,729 条轻量正式化全部因合同版本不匹配且当前门不支持而需复审；105 条旧
  “人工配置”全部缺乏可证明来源，且目前均对应 canonical `verified`。
- 精确逐项清单只保留在受限服务器：
  `/opt/finance-radar/shared/reports/fact_integrity_history_audit_v2_20260819.json`
  （9,604,616 字节，文件 SHA-256
  `a30ee6776eea0f8d2b09f5f80f4e91d6db472055b59d439abf380aa4d9d78e1f`）和同名
  Markdown（1,857 字节，SHA-256
  `c590afd0b19db1ec48e6cc6d60b1e2380e9de1781af99ea20dabfdc669555f25`）。
  JSON 内部规范化载荷哈希为
  `d6b755a7ece36975bf0108b5c5303c877fe446dc7bf44a15cf8e5d9948f6cd91`。
  仓库只记录聚合结果和哈希，不公开事件 ID、证据 passage 或生产数据库内容。
- 本轮没有 canonical 回滚、批量改写、模型晋级、交易、系统升级或重启。历史债
  的修复必须先审阅本次精确清单，再使用与该清单哈希绑定、限时、限量且动作专属的
  新授权合同；不得把本次只读审计解释为写入授权。
- 为本次部署临时放行的 EC2 Instance Connect 入站规则
  `sgr-0813614bd680afd8c`（TCP 22，AWS 托管前缀列表
  `pl-0e4bcff02b13bef1e`）已在完成取证并取得操作时确认后删除。AWS 控制台显示
  修改成功，入站规则由 7 条回到 6 条；现有
  `sgr-018f725a61dfbd882 / 159.89.226.240/32` 及其他规则均保留未改。

## 2026-08-18 产品改进（已发布生产）

原 `codex/postdeploy-product-loop` 候选已经以 PR #15 合并、发布并完成事务式部署：

- Public 首屏先渲染产品外壳，概览/筛选使用有界、按角色隔离的短缓存；刷新失败时
  只允许显示带年龄的旧快照。30 天产品指标移到事件列表之后，不再阻塞主要内容。
- 真人盲审改为独立凭据在服务端绑定主体哈希和固定角色；客户端不能自报身份或角色，
  Admin/共享 Reviewer 令牌不能冒充真人。新增 `human-blind-v3.1` 事件时点合同、
  issuer/event-chain、精确/近重复、来源分层和一次性哈希冻结门。
- 当前真实状态仍是旧合同 24 个 `OPEN` 样本、0 个真人结论；旧样本被明确列为
  contract-ineligible，新盲集保持 `NOT_READY`，没有训练、模型晋级或生产行为变化。
- 事件卡支持最高权威原始来源直达、保留筛选并返回精确卡片位置，以及仅限本次浏览
  会话的“自上次查看”状态/版本/证据变化说明。
- 精确合并提交本地完整回归为 `693 passed, 5 skipped`，两套 GitHub Actions 均
  通过；真实浏览器桌面、键盘、事件预览/返回，以及 `390×844` 窄屏均通过。
- 真实恢复演练发现旧发布归档漏掉被忽略的 `risk_router.joblib`。新版本把正式
  SHADOW 模型、SHA 声明、模型卡和 blind-v3 报告纳入 Git 与发布关键文件清单；
  生产模型文件 SHA-256 与三份声明精确一致，不再静默退化为关键词 fallback。

## 2026-08-18 生产发布、恢复与事实完整性复核

本节取代下方 2026-08-15 发布快照和 2026-08-18 03:25–03:31 UTC 的
部署前运行快照，作为当前生产事实源：

- `v2026.08.18.3` 已在美国 `us-east-1` 主机完成事务式激活，生产链接精确指向
  `/opt/finance-radar/releases/20260818T080656Z-a39224683399`；仓库提交与标签
  均指向 `a39224683399d5e330d5fd823f1cea2f0313b678`，发布归档 SHA-256 为
  `3cc7b0e7259432d93e61d62c3b858f2aa5b5a00d5e0f3bad186f3bce439adf9c`。
- 部署前完整恢复点 `finance_radar_20260818T080925Z_a47ced96` 已通过恢复门，
  收据 SHA-256 为
  `fac715ff444b2678dd57cf215b9df884594b9087a45c406ae958c8a5b7b25adc`；
  切换后唯一正式在线恢复点为 `finance_radar_20260818T082654Z_fe73ed7f`，
  清单 SHA-256 为
  `c7486530be1bfe3da59d51bc83469cdbb61035216f803dd72049debe257fe6cf`。
  正式日备份库存恰好一份，`weekly` 目录为空，符合“新备份完整验证成功后再替换
  旧备份、每日一份、周备份零份”的口径。
- API、Public Web、Worker 与每日备份计时器均为 active/enabled；备份 one-shot、
  Evidence LLM、Admin、Reviewer 与 Operator 均为 inactive，其中 Evidence LLM
  为 disabled，三个内部 UI 为 static/按需。
- 根盘为 `38 GiB`，异机传输临时文件清理后约 `27 GiB` 已用、`12 GiB` 可用
  （70%）；部署前/后备份峰值曾到 92%，但均在验证及单份轮换后回落。
- 公网 `/radar/`、`/radar/_stcore/health` 和 `/radar/release.json` 返回 200，
  release marker 精确为 `20260818T080656Z-a39224683399`；
  `/radar/offhost-status.json`、FastAPI、Admin、Reviewer 与 Operator 公网路径
  均返回 404。过期异机备份 JSON 已不再公开。
- 新版已切断常驻周期对旧 `manual_review_config` 的正式写入，并上线证据零相关门、
  发行人-事件谓词绑定和只读历史审计。生产审计没有尝试 canonical mutation：
  6,499 个旧 Evidence Agent **决策**各自至少含一条被当前相关性门拒绝的边，
  另有 4,275 个旧决策需按新合同重跑；被拒边本身的总数未由当时 v1 报告单独统计。
  2,729 条轻量核验被归入需复核／事件演化分类，但当时未拆分具体原因；105 条旧
  “人工核验配置”被标记为 provenance 不可证。报告 SHA-256 为
  `ac75b5715b99c46dddcc4ee8a66848c8da70166e768141e1e30d02941ff0078f`。
- Windows 上三条 Finance Radar 计划任务继续保持 Disabled；本次使用一次性受控
  SSH 路径生成了唯一接受的异机恢复点
  `D:\FinanceRadarBackups\20260818T083746Z`。AES-256-GCM 密文为
  1,547,871,368 字节，SHA-256 为
  `e3c7523691d99432f7e56b3d6759d704aafedb03f87ede9b28752634d2bd9706`；
  口令独立位于 `D:\FinanceRadarRecovery`。54,468 个归档成员、51,270 条清单、
  两个 SQLite、正式模型哈希链和边界审计全部通过，恢复工作区已清理。
- 临时 SSH 规则精确为 `sgr-0f1e0716b5e993b73`（`211.145.54.96/32:22`），
  完成传输后已从安全组删除；入站规则计数由 8 回到 7，随后从同一物理地址直连
  TCP 22 超时。旧公共 Release 恢复密文仍未删除，等待单独的破坏性动作确认。

本次失败路径也已诚实保留：

- 第一份 2026-08-18 异机归档在加密和解密回环后被完整恢复审计拒绝，因为当时
  生产发布缺少 `artifacts/risk_router.joblib`。系统没有降低门禁或把失败包标成
  成功；发布合同修复并部署 `.3` 后，重新从新的在线恢复点生成归档才取得 PASS。
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
| Git 提交 | `a39224683399d5e330d5fd823f1cea2f0313b678` |
| Git 标签 | `v2026.08.18.3`（解引用后精确指向上述提交） |
| Release ID | `20260818T080656Z-a39224683399` |
| 生产路径 | `/opt/finance-radar/releases/20260818T080656Z-a39224683399` |
| 发布归档 SHA-256 | `3cc7b0e7259432d93e61d62c3b858f2aa5b5a00d5e0f3bad186f3bce439adf9c` |
| 公网入口 | `https://radar.18-208-34-152.sslip.io:8443/radar/` |

## 现场运行证据

- 持久部署单元结果为 `success`，激活记录为 `PASS`；Nginx、API、Public Web 和
  Worker 切换门均通过。
- API、Public Web、Worker 和 `finance-radar-backup.timer` 为 active/enabled；API
  监听回环端口 `18000`，Worker 现场为 `NRestarts=0 / ExecMainStatus=0`、当前
  约 274 MiB、峰值约 281 MiB。三种内部 UI 为 inactive/static，Evidence LLM
  为 inactive/disabled。
- 切换后唯一正式备份为 `finance_radar_20260818T082654Z_fe73ed7f`，已完成
  双数据库恢复验证；清单 SHA-256 为
  `c7486530be1bfe3da59d51bc83469cdbb61035216f803dd72049debe257fe6cf`。
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

- 当前 `v2026.08.18.3` 源码完整回归：`693 passed, 5 skipped`；发布归档、清单、
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
