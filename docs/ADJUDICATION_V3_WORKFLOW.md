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

1. 打开 Web 终端的 **Adjudication Studio**。
2. 每个人使用不同的 Reviewer ID；不得共享身份。
3. 先以 `REVIEWER` 身份完成独立判断。提交后本人不再看到该任务。
4. 第二名审核者在不知道第一份答案的情况下完成同一样本。
5. 若进入 `CONFLICT`，第三人切换为 `ARBITER`，阅读两种匿名意见后裁决。
6. 运行审计，确认数量、来源组、冲突和冻结前缺口。

公网部署默认只显示聚合进度，审核写入被关闭。内部标注窗口必须同时设置：

```text
FINANCE_RADAR_REVIEW_UI_ENABLED=1
FINANCE_RADAR_REVIEW_ACCESS_CODE=<独立访问码>
```

访问码只保存在服务器环境中，不写入仓库、报告或答辩包。关闭标注窗口后移除或禁用开关并重启 Web 服务。API 仍要求 `X-Admin-Token`，两层门禁缺一不可。

```powershell
python scripts/seed_adjudication_queue.py --limit 24
python scripts/audit_adjudication_workflow.py
```

本地当前队列只建立任务，不含任何人工结论。输出位于：

- `reports/adjudication_v3_latest.json`
- `reports/adjudication_v3_latest.md`

## API

写操作和含原文的队列读取都沿用 `X-Admin-Token`：

- `GET /api/v1/adjudication/status`：只返回聚合状态。
- `POST /api/v1/adjudication/samples/from-event/{event_id}`：创建不带目标标签的样本。
- `GET /api/v1/adjudication/queue?reviewer_id=...&role=REVIEWER`：获取对该身份可见的独立任务。
- `POST /api/v1/adjudication/samples/{sample_id}/reviews`：提交判断轴。
- `role=ARBITER`：只读取冲突样本并进行第三人裁决。

Streamlit 不会把服务器管理员令牌交给浏览器，但其后端可以代发请求，因此 UI 必须保持上述独立访问门；不能仅依赖 API 管理员令牌。

## 冻结门禁

当前报告只有达到以下最低条件才会变为 `READY_FOR_OVERLAP_AUDIT`：

- `RISK_REVIEW >= 30`
- `NON_TARGET >= 30`
- `ABSTAIN >= 20`
- 至少 4 个独立来源组
- 每行通过 V3 合同验证

这仍不等于盲测集已冻结。下一步必须先与 V1 训练集和 external-blind-v1 做精确/近重复排查，再按 `entity_group + event_chain_group` 分组切分、哈希并冻结。任何阈值调参都不得读取 blind-v2。

## 真实性边界

系统可以证明“工作流可用”和“错误数据会被拦截”，不能证明学生已经完成真实双审。审核身份、理由和时间必须来自实际参与者；不得批量自动填写、复制理由或把旧模型输出转写成人工结论。
