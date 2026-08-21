<!--
UI 交付物提交模板。完整流程见 docs/UI_SUBMISSION_GUIDE.md。
填不出来的行请如实写“未做”或“不适用”，不要删行、不要留空。
-->

## 1. 交付范围

一句话说明这次交付了什么，以及页面/脚本的入口是什么。

## 2. 溯源

| 项目 | 内容 |
|---|---|
| 原始压缩包文件名 | |
| 压缩包 SHA-256 | |
| PBI | `PBI_` |
| Sprint 任务 | `Sprint<N>-T<序号>` |
| 提交人（GitHub 账号） | @ |

## 3. 文件清单

列出本 PR 触碰的每一个文件，以及它为什么必须改。压缩包以外的改动要单独说明理由。

| 路径 | 新增/修改 | 说明 |
|---|---|---|
| | | |

## 4. 本地门禁输出

贴命令的实际文本输出，不要贴截图。

```text
git diff --cached --check
python -m compileall -q app scripts tests
python -m pytest -q
python scripts/verify_dependency_locks.py
python scripts/audit_ui_submission.py --zip <压缩包> --base origin/main --with-authors
```

## 5. 界面证据

桌面与窄屏各一张截图或一段录屏；如果改了交互，补一句键盘操作是否仍然可用。

## 6. 边界声明

- [ ] 未新增下单、持仓、余额或交易执行相关的接口、控件或文案
- [ ] 页面只调用只读接口（`GET /api/v1/*`）
- [ ] 未改动 `app/`、`deployment/`、`release/`、`requirements*.lock`、`dependency-lock.json`、`VERSION`
- [ ] 未提交 `.zip`、`.env`、密钥、证书、`__pycache__` 或其他生成物
- [ ] 提交作者身份是本人，`user.email` 为 GitHub 已验证邮箱

## 7. 已知问题与后续

还没做完、已知不足或需要后续处理的事项。没有就写“无”。
