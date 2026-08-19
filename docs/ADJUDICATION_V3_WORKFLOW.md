# V3 双人盲标工作流

## 目的

这套工作流把 `config/risk_label_contract_v3.json` 从静态规则变成可操作的人审系统，但不会替任何人填写标签。它只解决四件事：建立未标注样本、隔离两名审核者、对冲突进行第三人裁决、输出仍为 `UNASSIGNED` 的预冻结候选。

生产影子模型不读取这些未冻结记录，也不会因为标注数量增长而自动晋级。

## 数据隔离

- 样本来自已有账本事件和精确证据段落，不含事件后行情。
- 审核页面隐藏原始 `source_id`，只显示稳定匿名 token 和证据权限类别。
- 审核者看不到影子模型结果、旧标签、派生目标标签或另一名审核者的答案。
- 人只提交 `materiality`、`polarity`、`evidence_state` 和理由。
- 两份判断完全一致才直接形成共识；不一致时必须由第三个独立身份裁决。
- `RISK_REVIEW / NON_TARGET / ABSTAIN` 由纯函数在审核完成后派生，来源不能直接成为目标标签。

## 使用流程

1. 运维为两名真实 Reviewer 和一名独立 Arbiter 分别创建凭据；每个角色由 API 服务端绑定，不允许页面自报或切换。
2. 打开仅回环可达的 Reviewer UI，再进入 **Adjudication Studio**。
3. 每个人输入自己的个人审核凭据；共享 Reviewer/Admin 令牌不能提交真人标签。
4. 两名 `REVIEWER` 分别完成独立判断。提交后本人不再看到该任务。
5. 若进入 `CONFLICT`，只有凭据绑定为 `ARBITER` 的第三人可以阅读匿名分歧并裁决。
6. 运行审计，确认数量、来源组、冲突、身份独立性和冻结前缺口。

公网部署默认只显示聚合进度，审核写入被关闭。内部标注窗口必须同时设置：

```text
FINANCE_RADAR_REVIEW_UI_ENABLED=1
FINANCE_RADAR_REVIEW_ACCESS_CODE=<独立访问码>
```

访问码只保存在服务器环境中，不写入仓库、报告或答辩包。关闭标注窗口后移除或禁用开关并重启 Reviewer 服务。它只是 UI 入口门，不代表审核者身份。API 的真人读写要求 `X-Reviewer-Token` 对应 `/etc/finance-radar-reviewer-principals.json` 中的独立主体；`X-Admin-Token` 和共享 Reviewer UI token 都不能冒充真人审核者。

首次系统级部署会生成一个内容为 `[]` 的 root-only credential 文件，因此真人写入默认返回 503。由所有者确认参与人后，按 `deployment/README.md` 使用 `scripts/generate_reviewer_principals.py` 生成并安装凭据，再分别安全交付个人 token。恢复到新主机时该文件故意重置为 `[]`，不得从历史迁移包自动复活旧人审身份。

```powershell
python scripts/seed_adjudication_queue.py --limit 24
python scripts/audit_adjudication_workflow.py
```

本地当前队列只建立任务，不含任何人工结论。输出位于：

- `reports/adjudication_v3_latest.json`
- `reports/adjudication_v3_latest.md`

## API

建样本仍是 Admin 动作；含原文的队列读取与审核写入必须使用服务端绑定的个人 `X-Reviewer-Token`：

- `GET /api/v1/adjudication/status`：只返回聚合状态。
- `POST /api/v1/adjudication/samples/from-event/{event_id}`：创建不带目标标签的样本。
- `GET /api/v1/adjudication/queue`：服务端根据个人凭据返回其独立任务；不接受客户端 `reviewer_id/role`。
- `POST /api/v1/adjudication/samples/{sample_id}/reviews`：提交判断轴。
- `ARBITER` 凭据：只读取冲突样本并进行第三人裁决。

Streamlit 不会把服务器管理员令牌交给浏览器，但其后端可以代发请求，因此 UI 必须保持上述独立访问门；不能仅依赖 API 管理员令牌。

## 冻结门禁

当前报告只有达到以下最低条件才会变为 `READY_FOR_OVERLAP_AUDIT`：

- `RISK_REVIEW >= 30`
- `NON_TARGET >= 30`
- `ABSTAIN >= 20`
- 至少 4 个独立来源组
- 每行通过 V3 合同验证

这仍不等于盲测集已冻结。冻结脚本会对全部历史训练/盲测清单做实体、事件链、精确文本和真正的近重复相似度检查；同时要求至少存在一个完整未在历史语料出现的来源家族。它先生成数据集、哈希、来源留出结论和未批准的授权模板：

```powershell
python scripts/freeze_human_blind_v3.py `
  --output-dir "D:\FinanceRadarBlindFreeze\candidate"
```

所有者必须检查候选清单后，复制授权模板并精确填写 `approved=true`、动作 ID、外部操作者、目的和到期时间。模板已绑定 `freeze_id`、完整数据集哈希、样本 ID 集哈希、数量与 held-out source families；任一字段变化都使授权失效。命令行自报 `actor=owner` 不构成授权，`--apply` 只接受独立的 `--authorization-file`。

正式 apply 建议在生产 Linux 的私有目录执行。脚本先原子写 `PREPARED` 清单，再用一个 SQLite 事务同时写入完整冻结收据和全部 `FROZEN` 状态，最后写 `COMMITTED` 清单。若最后一步崩溃，同一目录、同一授权和同一哈希的重试只做幂等对账；任何差异均失败关闭。Windows 上 `chmod 0600` 不能证明 NTFS ACL，清单会如实标记 `WINDOWS_CALLER_ACL_NOT_PROVEN_BY_CHMOD`，本地候选应放在受控 D 盘目录，不能把它当成已经证明 owner-only。

冻结不会训练、替换或晋级模型，`split` 仍只成为 `HUMAN_BLIND_V3` 的一次性评估集合。任何阈值调参都不得读取该盲集。

## 真实性边界

系统可以证明“工作流可用”和“错误数据会被拦截”，不能证明学生已经完成真实双审。审核身份、理由和时间必须来自实际参与者；不得批量自动填写、复制理由或把旧模型输出转写成人工结论。
