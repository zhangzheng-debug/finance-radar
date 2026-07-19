# Finance Radar course readiness audit

- Generated: `2026-07-19T05:17:23.205773+00:00`
- Overall: **NOT_READY**
- Engineering product: **WAITING**
- Authentic course process: **WAITING_EXTERNAL**

## Engineering evidence

| 状态 | 工程门禁 | 下一步 |
|---|---|---|
| PASS | `public_acceptance_current_report_all_checks_pass` |  |
| PASS | `full_encrypted_migration_restore` |  |
| WAITING | `runtime_24h_hash_chain_pass` | 等待服务端窗口自然达到24小时后重新运行 capture_runtime_evidence.py。 |
| PASS | `external_blind_failure_disclosed_and_shadow_blocked` |  |
| PASS | `risk_label_v3_invalid_data_blocked` |  |
| PASS | `v3_dual_review_workflow_operational` |  |
| PASS | `v3_public_readonly_boundary_pass` |  |
| PASS | `human_taskbook_present` |  |
| PASS | `ai_spec_present` |  |
| PASS | `rendered_defense_deck_present` |  |
| PASS | `browser_QA_baseline_present` |  |
| PASS | `public_1920_interaction_QA_baseline_pass` |  |
| WAITING | `current_release_browser_QA_pass` | 用真实浏览器对当前release重跑大屏/桌面/移动、键盘、Replay和可访问性矩阵，并在报告写入release字段。 |

## Authentic student / teacher evidence

| 状态 | 课程门禁 | 下一步 |
|---|---|---|
| WAITING | `teacher_approval_evidence` | 保存教师签字扫描件或原始回复，计算SHA-256后写入manifest。 |
| WAITING | `teacher_approved_high_difficulty` | 教师明确确认自主高难度选题后才设为true。 |
| WAITING | `teacher_approved_FZ3` | 教师明确批准finality_gate或指定替代项后才设为true。 |
| WAITING | `role_matrix_evidence` | 由学生填写角色/代码/测试/评审/答辩责任矩阵并保存真实路径。 |
| WAITING | `member_records_complete` | 在manifest登记每位真实成员、角色和答辩责任范围。 |
| WAITING | `three_forbidden_zone_file_sets` | 每个禁飞区补齐学生设计、实现与测试三个真实文件。 |
| WAITING | `forbidden_zone_ownership_and_review` | 每个禁飞区指定不同的负责人和复核人。 |
| WAITING | `forbidden_zone_student_commits` | 填写每个禁飞区真实首提交和最终提交，禁止倒签AI历史。 |
| WAITING | `three_timed_drills_per_member` | 每人完成三次计时Bug练习，保存失败提交、修复提交、起止时间和报告。 |
| WAITING | `one_improvised_change_per_member` | 每人完成一次即兴修改并保存报告与真实提交。 |
| WAITING | `repository_has_real_commits` | 学生审阅边界后开始真实、小步、可解释的Git提交。 |

This audit deliberately refuses to infer teacher approval, student authorship, Git history or timed performance from AI-generated files. Fill `config/course_evidence_manifest.json` only with real evidence paths and commit IDs, then re-run the audit.
