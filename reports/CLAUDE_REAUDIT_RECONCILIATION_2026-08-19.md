# Claude 审计复核与工程收口记录

- 日期：2026-08-19
- 起点：`main@d31b00f`
- 工作分支：`codex/claude-reaudit-hardening`
- Claude 原复审：远端 `claude/repository-audit-q77ut2` 的 `repository_reaudit_20260815.md`
- 合并结果：PR #17 已于 2026-08-19 squash 合并为 `6401db4aac4e9479cb92ee641f7b6f5c6991ecfd`
- 边界：仓库实现与 GitHub 事实；不宣称已部署到 AWS，不宣称真人标签已完成，不晋级 shadow 模型

## 结论

Claude 对 2026-08-15 之前的限流、依赖锁、备份互斥、角色 UI 和版本一致性复核大体成立，但“未发现新缺陷”不能沿用到当前的人审/冻结链。代码级反例复查发现三项 P1：真人凭据只有运行时解析器、生产部署没有供给路径；盲集冻结把 CLI 自报身份当授权且数据库没有完整冻结收据；所谓 near-duplicate 只是规范化文本等值哈希，来源留出也没有 apply 门。

本分支把三项从文档愿望改成了可执行合同。它没有生成任何真人结论，也没有改变模型、canonical event 或交易边界。

## 发现与处置

| ID | 原状态 | 风险 | 本分支处置 | 当前状态 |
| --- | --- | --- | --- | --- |
| FR-RA-001 | API 支持 bound principals，但 systemd/Compose/恢复路径未闭合 | 生产 API 会 503，真人双审不可实际开展 | systemd `LoadCredential`、root-only 默认 `[]`、恢复重置、Compose JSON、无回显生成器与合同测试 | 仓库已修；待部署后现场验收 |
| FR-RA-002 | `--actor owner` 等 CLI 字符串即可 apply | 无外部、精确范围授权；可先改状态后丢清单 | 独立授权文件精确绑定 freeze/data/sample/source scope；PREPARED→DB 单事务收据+状态→COMMITTED；完全相同重试幂等 | 仓库已修；真人候选尚不存在 |
| FR-RA-003 | near-duplicate 等于规范化文本 SHA 相等 | 小编辑/重排可跨训练与盲集泄漏 | token overlap coefficient + 5-gram Jaccard，历史语料与盲集内部双向检查，含中英文反例测试 | 仓库已修 |
| FR-RA-004 | “来源留出”只有文档文字 | 同源数据可同时用于开发和盲测 | 规范化 source family；冻结前至少四个独立家族，apply 另要求至少一个历史语料完全未出现的家族并绑定授权 | 仓库已修；同一提供方的多个 feed 只计一个家族 |
| FR-RA-005 | Windows `chmod 0600` 被测试当成 owner-only | NTFS 上形成虚假安全证明 | POSIX 继续验证 0600；Windows 清单明确 `WINDOWS_CALLER_ACL_NOT_PROVEN_BY_CHMOD` | 已修 |
| FR-RA-006 | Claude 课程/团队 HTML 与产品源码边界不清 | 主分支继续膨胀，演示工件混入运行时 | 仅保留源文档；新增精确 ignore，生成的 team HTML/PDF 走 Release/外部工件 | 已修 |

## 关键实现证据

- `app/config.py`：环境 JSON 与 systemd credential 二选一；同时出现即拒绝；credential 有 64 KiB 上限。
- `deployment/systemd/finance-radar-api.service`：API 使用 `LoadCredential=reviewer-principals.json:...`。
- `scripts/generate_reviewer_principals.py`：至少两名 Reviewer 加一名 Arbiter；ID/token 唯一；独占创建；控制台不打印 token。
- `app/storage/operations.py`：operations schema 7；`adjudication_freezes` 保存完整数据/样本/授权哈希和 receipt；与样本 `FROZEN` 同事务。
- `scripts/freeze_human_blind_v3.py`：先 dry-run 和未批准模板；apply 只读独立 authorization file；精确绑定和到期检查；写前失败不产生可误认的授权清单。
- `app/services/adjudication.py`：近重复不再等于等值哈希；阈值、历史对照与集合内对照都进入 manifest。

## 尚未完成且不得伪装完成

1. 当前没有两名真实 Reviewer 与一名真实 Arbiter 的所有者批准名单，因此 production credential 文件应继续为 `[]`。
2. 当前 24 个样本仍是 0 份真人 review；没有 authentic-human blind-v3 可以冻结。
3. 没有真实盲集结果，模型继续 `QUALIFIED_SHADOW / advisory / no-auto-verify / no-trading`。
4. 本分支不是 AWS 部署证据。合并后仍需独立发布、恢复门、部署和现场验收。
5. 公共 Release 中的历史生产恢复密文仍是开放治理违规；本轮只登记，不在缺少单独破坏性授权时删除。

## 本地验证

- `python -m pytest -q`：`724 passed, 5 skipped`（PR #17 合并前最终 Windows 全量）
- `git diff --check`：PASS
- `python -m compileall -q app scripts tests`：PASS
- `deployment/compose.yml` YAML parse：PASS
- `deployment/systemd/*.sh` 共 9 个 `bash -n`：PASS
- CI 同款高置信秘密与交易写路由门：PASS

## 发布建议

PR #17 的 push 与 pull-request 两套 GitHub Actions 均已通过并完成 squash 合并。
该合并不自动触发模型晋级、真人冻结、AWS 部署或公共资产删除。

## Claude 后续反馈的二次核对

Claude 对课程材料去过期化、取回 2026-08-15 历史复审和登记历史事实债的方向成立，
但其历史债报告把 6,499 个“至少含一条被拒边的 Agent decisions”误写为 6,499 条边，
并把 2,729 条轻量核验分类直接等同于当前语义门失败。后续分支已把审计合同升级为 v2：
单列 `rejected_edge_total`、按原因拆轻量记录，并生成三份只读 affected manifests。
旧配置的 105 条来源不可证记录也单列“当前 canonical 仍为 verified”的数量，避免用
“部分进入 canonical”弱化风险。任何正式状态变更仍需独立动作授权。

工作区本地数据库的 v2 只读预演生成报告 SHA-256
`a2aedaf67487373e3dcabb67419f131e053cf78720fc1fa8d1f10ceb24a5becf`：本地没有生产
operations／轻量历史，故 A/B/C 必须在生产另行只读复跑；D 类则精确得到 105 条不可证且
105 条当前均为 canonical `verified`。预演没有尝试 canonical mutation。
