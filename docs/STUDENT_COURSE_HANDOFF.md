# Finance Radar 学生接手与课程封版清单

更新：2026-08-19。当前工程成品已经可公开演示，但本文件不会把教师批准、学生作者身份、计时表现或当前版浏览器证据推断为完成。所有运行数字均以 `CURRENT_STATE.md` 的最新现场窗口为准。

## 当前已经由系统证明

| 项目 | 当前证据 |
|---|---|
| 公网版本 | 当前 AWS 区域、release、服务状态与公网验收见 `CURRENT_STATE.md`；本文不复制易过期的运行数字 |
| 功能与数据 | 完整回归复跑 `python -m pytest -q`；公网产品、行情能力、离线终端和来源健康以当前提交生成的验收报告为准；证据对象为内容寻址原始快照，分页不会被头部失败记录永久卡住 |
| 换机恢复 | 最近在线／异机恢复点、Schema、清单哈希和隔离恢复结果见 `CURRENT_STATE.md`；任何真实新机激活仍须通过资源、端口、工具和 HTTPS 失败前置门 |
| 安全边界 | 无交易路由；无账户数据；Telegram默认dry-run；备份不含交易项目和TLS私钥 |
| 文档 | 课程材料、浏览器矩阵与可访问性结论必须绑定当前 release 重跑；旧渲染只能作历史证据 |
| 模型治理 | v1 误报 95% FAIL → v2 再 FAIL → v4 三层架构在 blind-v3 上误报 6.7%、11 道门全过，但因标签非人工双盲而自限 `QUALIFIED_SHADOW`；真人盲集未冻结，不声称真人准确率 |

## 仍必须由人完成

| 负责人 | 必须完成 | 机器如何验收 |
|---|---|---|
| 教师 | 明确认定自主高难度选题并批准FZ3或指定替代项 | 签字/原始回复路径、SHA-256和两个批准布尔值 |
| 全体学生 | 填写角色、代码、测试、Review和答辩责任 | `role_matrix_path`和每位成员的`role`、`answer_scope` |
| 禁飞区负责人 | 分别手写FZ1/FZ2/FZ3设计、实现、测试 | 三套真实文件、不同负责人/复核人、首/末Git提交 |
| 每位学生 | 三轮计时Bug、一次即兴修改 | 起止时间、报告、失败提交、修复提交和复核记录 |
| UI验收者 | 对当前release重跑大屏/桌面/移动、键盘、Replay和可访问性 | 浏览器报告的`release`必须等于`.deploy_context.json` |
| 运维者 | 等待24小时门禁自然转绿；任何主机切换前完成恢复门和 TLS 切换计划 | runtime 报告 `PASS`；新端点重新跑当前版公网与行情验收 |

## 建议三天顺序

1. Day 1：提交`.agent/teacher_approval_request.md`；确定真实成员和三个禁飞区负责人；开始小步Git历史。
2. Day 2：每个禁飞区先提交失败测试和设计，再提交最小实现；不同成员完成Review。
3. Day 3：每人完成三轮计时Bug和一次即兴修改；刷新当前release浏览器矩阵；重新生成答辩包。

不要先填`true`再补材料，也不要把已有AI外围代码倒签成学生实现。`config/course_evidence_manifest.json`只写仓库内真实相对路径和真实提交ID。

## 每次收口命令

```powershell
python -m pytest -q
python scripts/collect_product_acceptance.py
python scripts/capture_market_capabilities.py
python scripts/capture_runtime_evidence.py
python scripts/audit_course_readiness.py
python scripts/audit_course_readiness.py --require-product-ready
python scripts/audit_course_readiness.py --require-ready
python scripts/build_defense_evidence_pack.py
```

前五条用于生成证据；两个`--require-*`命令是硬门禁，未达到时应返回非零，不得手工改报告。最终只有`--require-ready`返回0，才可称为课程封版就绪。
