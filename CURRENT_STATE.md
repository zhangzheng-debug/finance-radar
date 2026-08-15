# Finance Radar current state

审计日期：2026-08-15（Asia/Singapore）
审计范围：Git 仓库、发布记录与可复核测试记录；**不包含本轮 AWS 生产现场核验**。

## 证据层级

| 项目 | 本轮可确认状态 | 不能据此推断 |
|---|---|---|
| 最后一个带标签的恢复发布 | `v2026.07.22.2` | 不是当前服务器仍运行该版本的证明 |
| 对应应用发布 | `20260722T084500Z` | 不是当前生产 symlink 或服务状态 |
| 对应加密迁移快照 | `20260722T084527Z` | 不是 2026-08-15 的新鲜备份 |
| 当前仓库状态 | PR #4 已合并为 `main@96db114`；归档锁修复分支为 `codex/release-archive-lock-portability` | 修复分支尚未合并、打标签或形成已接受恢复点 |
| 当前候选 | 首个 `20260815T015844Z-96db114f59a5` 在隔离解包时被拒绝；必须重新形成新 ID | `READY` 清单不能替代候选内自校验 |
| 最近一次完整本地回归 | Python 3.12 hash-lock 环境：`663 passed, 5 skipped, 20 subtests passed` | 不是归档锁修复分支的 GitHub Actions 结果 |
| 生产运行状态 | 2026-08-15 公网 Nginx/Streamlit 进程响应，但页面报 `ModuleNotFoundError: No module named 'app'` | HTTP 200 和 `_stcore/health` 不能证明应用可用 |

## 发布口径

`v2026.07.22.2` 是仓库中最后一个可按标签定位的恢复基线，其清单记录在
[`BACKUP_INVENTORY.md`](BACKUP_INVENTORY.md)。当前分支包含之后的 UI 收敛、
Evidence policy、备份/恢复、systemd 资源保护和发布审计改动；在以下条件全部完成前，
这些改动只能称为 **Unreleased**：

1. 预期文件经过显式 staging 和差异复核；
2. 精确候选提交通过全量测试、shell 语法、秘密扫描与只读边界门禁（本地工作树门禁已通过，提交 SHA 尚待形成）；
3. GitHub Actions 在该候选 SHA 上成功；
4. 如要创建新恢复标签，另有与该提交绑定且已独立验证的发布归档和恢复快照。

## 生产状态边界

2026-08-15 的外部探针确认公网拒绝面仍工作，但唯一公共 UI 的 Python 导入失败；
SSH 在 banner 交换前超时，AWS 控制台登录态已过期。因此 API、Worker、备份计时器、
事件新鲜度、磁盘、内存和实例身份仍未现场核验。恢复访问后必须核对目标 AWS 账户和
区域、活动 release、systemd、账本周期、备份收据以及公开只读边界。历史报告仍可
用于说明设计与恢复方法，但必须明确标注其日期。
