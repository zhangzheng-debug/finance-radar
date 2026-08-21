# UI 交付物 Git 提交规范（组员用）

更新：2026-08-21。适用对象：把 UI 页面、预览服务或前端脚本以压缩包形式交付给本仓库的组员。

> 一句话原则：**zip 只是运输方式，不是交付物。交付物是一次由本人署名、可复跑、可回滚、能被逐行审计的 Git 提交。**

## 0. 为什么不能直接把 zip 丢进仓库

| 留痕要素 | 一个 zip 附件 | 一次规范的 Git 提交 |
|---|---|---|
| 谁做的 | 无。文件属性可任意修改 | commit author 绑定 GitHub 账号，不可事后伪造 |
| 什么时候做的 | 无。修改时间可改 | author date + push 时间 + PR 时间线三重记录 |
| 改了什么 | 无。只能整包对比 | 逐行 diff，评审可以只针对某一行提问 |
| 能不能复跑 | 不能。CI 不会解压 zip | CI 对每次 push 重跑白空格、编译、锁、密钥、全量测试 |
| 出事能不能退回 | 不能 | `git revert` 单次提交即可 |
| 能不能审计 | 只能人眼看 | `scripts/audit_ui_submission.py` 机器判定 PASS/FAIL |

由此得到四条硬规则，任何一条不满足都会被打回：

1. **不把 `.zip` 本身提交进仓库。** 提交解压后的源文件；原始压缩包留在团队自己的存档里。
2. **由做这份 UI 的人自己提交。** 不要把 zip 发给仓库所有者代传——代传会把作者记成所有者，这份工作就在 Git 里消失了。
3. **一个分支一件事。** UI 交付分支里不出现顺手改的其他模块。
4. **不 force-push、不 amend 已推送的提交。** 评审意见用新的提交回应，历史必须保留。

## 1. 一次性准备（每人只做一次）

### 1.1 取得提交通道（二选一）

本仓库目前只有所有者 `zhangzheng-debug` 一个协作者，组员默认没有推送权限。两条路都能保住作者身份，选一条即可：

- **路线 A（推荐）：加为协作者。** 所有者在 `Settings → Collaborators → Add people` 里把组员加成 **Write** 角色。组员之后直接在本仓库开分支。
- **路线 B：Fork 后提 PR。** 组员点右上角 `Fork`，在自己的 fork 上开分支，再向 `zhangzheng-debug/finance-radar` 的 `main` 提 PR。不需要所有者改任何权限。

两条路线除了 `git clone` 的地址不同，后面的步骤完全一样。

> 建议所有者同时给 `main` 开分支保护：要求经 PR 合并、要求 `finance-radar-ci` 通过、禁止 force push。没有保护规则时，“规范提交”只是自觉，不是制度。

### 1.2 克隆并装好环境

要求 Python 3.12。以下命令在仓库根目录的 PowerShell 里执行：

```powershell
git clone https://github.com/zhangzheng-debug/finance-radar.git
cd finance-radar
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r requirements-dev.lock
```

### 1.3 配置真实身份（这是留痕的根）

```powershell
git config user.name  "你的真实姓名或固定英文名"
git config user.email "你 GitHub 账号里的邮箱"
git config core.autocrlf false
```

`user.email` 必须是你 GitHub 账号已验证的邮箱，否则提交在 GitHub 上不会归到你名下，贡献记录为空。设完先自查：

```powershell
git config user.name; git config user.email
```

仓库的 `.gitattributes` 已经强制文本文件按 LF 入库，所以 `core.autocrlf` 设 `false` 即可，不要再设 `true`。

## 2. 交付物落位规则

### 2.1 压缩包里的路径就是仓库根的相对路径

导出 zip 时，**路径要从仓库根算起，不要再套一层目录**。本次这份 `Finance_Radar_UI_API_20260821.zip` 的结构就是正确示范：

```text
ui_preview/finance-radar-ui-concept.html
ui_preview/README.md
scripts/serve_light_ui_preview.py
tests/test_light_ui_preview.py
```

在仓库根解压后，四个文件正好落到 `ui_preview/`、`scripts/`、`tests/`。错误示范是 `Finance_Radar_UI_API_20260821/ui_preview/...`——多出的那层会让代码里的相对路径全部失效（`scripts/serve_light_ui_preview.py` 用 `parents[1]` 定位仓库根，再用 `ui_preview/` 找页面文件）。

### 2.2 可以碰和不可以碰的位置

| 位置 | 说明 |
|---|---|
| `ui_preview/`、`claudeUI/` | UI 页面、样式、预览说明 |
| `scripts/` | 本地预览服务器等独立入口脚本 |
| `tests/` | 与本次交付一一对应的测试，文件名 `test_<脚本名>.py` |
| `docs/` | 仅当交付包含需要长期查阅的说明 |
| `app/`、`config/`、`deployment/`、`release/` | **UI 交付分支不要动。**确实需要改，先单独开 issue 或 PR 说明 |
| `requirements*.lock`、`dependency-lock.json`、`VERSION` | **不要动。**依赖与版本由发布流程统一维护 |

### 2.3 永远不要提交的文件

`.zip` / `.tgz` / `.7z`、`.env` 或任何含 token 的文件、`__pycache__/` 与 `.pyc`、`node_modules/`、`.exe` / `.dll`、`*.session`、私钥与证书（`.pem` / `.key` / `.pfx`）、截图之外的大二进制、IDE 目录。

CI 里有一道密钥硬门禁会扫描全部被跟踪文件，一旦命中直接失败；而且**密钥一旦推上去，改掉再提交也没用，必须立刻作废重发**。

### 2.4 UI 交付的产品边界

页面只能调用只读接口（`GET /api/v1/*`）。不得新增下单、持仓、余额、交易执行相关的路径或控件——CI 会用 AST 扫描 `app/` 下的写路由并拒绝这类命名，产品章程也禁止。

## 3. 提交流程（照抄即可）

### 第 1 步：从最新的 main 出发

```powershell
git switch main
git pull --ff-only origin main
```

### 第 2 步：开一个能看出人和事的分支

命名规则 `ui/<成员标识>-<主题>`，全小写，用连字符分词：

```powershell
git switch -c ui/liwei-light-preview
```

### 第 3 步：在仓库根解压

```powershell
Expand-Archive -Path "$HOME\Downloads\Finance_Radar_UI_API_20260821.zip" -DestinationPath . -Force
git status --short
```

`git status` 必须只列出你这次交付的文件。多出任何一行，先弄清楚原因再继续。

### 第 4 步：只 add 你的交付文件，逐个确认

不要用 `git add .`。按路径显式添加：

```powershell
git add ui_preview/finance-radar-ui-concept.html ui_preview/README.md
git add scripts/serve_light_ui_preview.py tests/test_light_ui_preview.py
git diff --cached --stat
git diff --cached --check
```

### 第 5 步：跑完本地五道门（见第 4 节），全绿再继续

### 第 6 步：写一条能被机器检索的提交信息

主题行用英文祈使句、不超过 72 字符、句末不加句号（与仓库现有历史一致）；正文用中文说清楚做了什么、为什么；结尾的 trailer 是留痕的关键，`git log --grep` 可以直接检索：

```powershell
git commit
```

模板：

```text
Add read-only light UI preview server and page

新增浅色只读研究工作台预览：页面接 GET /api/v1/overview、/events、/events/{id}，
预览服务器只转发 GET，其余方法返回 405 READ_ONLY_PREVIEW，API 目标限制在回环地址。
不新增任何写接口，不触碰 app/ 与部署配置。

PBI: PBI_52
Sprint-Task: Sprint4-T27
Source-Archive: Finance_Radar_UI_API_20260821.zip
Archive-SHA256: 3a3e5c8aad1d9ef52d352006c7cc27a085051223f8d3738798c28f377bc7789c
```

四个 trailer 的含义：

- `PBI:` —— 对应 `docs/scrum/README.md` 产品 Backlog 里的条目号。**没有对应条目，就先把这条 PBI 加进产品 Backlog，再来提交**；不要事后补编号。
- `Sprint-Task:` —— 对应 Sprint Backlog 里的任务行，格式 `Sprint<N>-T<任务序号>`。
- `Source-Archive:` —— 你交付的原始压缩包文件名。
- `Archive-SHA256:` —— 该压缩包的 SHA-256，用 `Get-FileHash` 取：

```powershell
Get-FileHash "$HOME\Downloads\Finance_Radar_UI_API_20260821.zip" -Algorithm SHA256
```

有了这两行，任何人日后都能拿原始压缩包和这次提交做逐字节比对。压缩包本人留存备份，文件名建议带哈希前 12 位。

一次交付如果包含几块相互独立的内容（例如页面、预览服务、测试），**拆成几个提交**比挤成一个更好审，也更好回退。

### 第 7 步：推送

```powershell
git push -u origin ui/liwei-light-preview
```

## 4. 推送前必须自己跑的五道门

这五条与 `.github/workflows/ci.yml` 一一对应。在本地先跑一遍，CI 就不会替你公开失败：

| # | 本地命令 | 对应 CI 步骤 | 卡住时怎么办 |
|---:|---|---|---|
| 1 | `git diff --cached --check` | Check changed text for whitespace errors | 删掉行尾空格，不要用空格凑对齐 |
| 2 | `.\.venv\Scripts\python.exe -m compileall -q app scripts tests` | compileall | 有语法错误，先改对 |
| 3 | `.\.venv\Scripts\python.exe -m pytest -q` | pytest | 必须全绿。**不允许删测试或加 skip 让它变绿** |
| 4 | `.\.venv\Scripts\python.exe scripts\verify_dependency_locks.py` | verify_dependency_locks | 说明你改了依赖文件，UI 交付不应该改 |
| 5 | `git diff --cached` 通读一遍 | 密钥与交易路由硬门禁 | 确认没有 token、密码、内网地址、真实邮箱名单 |

再加一道本仓库专门为这类交付准备的自查（同时也是审计者会跑的那条命令）：

```powershell
.\.venv\Scripts\python.exe scripts\audit_ui_submission.py `
  --zip "$HOME\Downloads\Finance_Radar_UI_API_20260821.zip" `
  --base origin/main --with-authors
```

必须看到 `Result: PASS`。它会逐个文件比对压缩包与仓库里已提交的内容，并检查是否夹带了压缩包以外的改动。

## 5. 开 Pull Request

推送后 GitHub 会给出创建 PR 的链接，或者直接打开（把分支名换成你的）：

```text
https://github.com/zhangzheng-debug/finance-radar/compare/main...ui/liwei-light-preview?expand=1&template=ui_submission.md
```

`template=ui_submission.md` 会自动套用本仓库的 UI 交付模板（`.github/PULL_REQUEST_TEMPLATE/ui_submission.md`）。逐项填写，**不要删掉填不出来的行——如实写“未做”比留空好**。PR 里至少要有：

- 交付范围与文件清单
- 原始压缩包文件名与 SHA-256
- 对应的 PBI 与 Sprint 任务
- 本地五道门的实际输出（贴文本，不贴截图）
- 页面截图或录屏（桌面 + 窄屏各一张）
- 明确声明未新增写接口、未改动 `app/` 与部署配置

然后：先开成 **Draft**，等 CI 全绿再点 `Ready for review`，最后指定所有者为 reviewer。

## 6. 审计者会怎么验

组员和审计者跑的是同一条命令，所以不该出现“你那边过了我这边没过”：

```powershell
python scripts\audit_ui_submission.py --zip <原始压缩包> --base origin/main --with-authors --manifest reports\ui_submission_audit.json
```

判定结果的含义：

| 状态 | 含义 | 是否通过 |
|---|---|---|
| `MATCH` | 仓库文件与压缩包逐字节一致 | 通过 |
| `MATCH_NORMALIZED` | 只差在 BOM / CRLF，Git 已按规则归一 | 通过（会给出提示） |
| `MISMATCH` | 提交的内容和压缩包不是同一份 | 不通过 |
| `MISSING` | 压缩包里有、仓库里没有 | 不通过 |
| `UNTRACKED` | 文件在硬盘上但没被 Git 跟踪（多半是被 `.gitignore` 挡了） | 不通过 |

另外还会失败的情况：压缩包里有路径穿越（`../`）、绝对路径、软链接、`.env`、嵌套压缩包等不可评审内容；行尾空格；BOM；以及**分支改了压缩包里没有的文件**（夹带私货）。确实需要额外改动时，用 `--allow-extra` 让它只报告不判负，并在 PR 里写清楚每一处额外改动的理由。

`--with-authors` 会记录每个文件是被谁、在哪个提交、什么时间加进来的——这正是这套流程要留下的痕迹。`--manifest` 生成的 JSON 建议附在 PR 里，而不是提交进仓库（本仓库的规矩是纯代码 PR 不混入生成报告）。

## 7. 被打回之后怎么改

```powershell
# 在同一个分支上继续改
git add <改动的文件>
git commit -m "Fix keyboard focus order in evidence list"
git push
```

- **不要** `git commit --amend` 或 `git push --force` 已经推送过的提交：评审看到的行号会全部错位，修改过程也被抹掉。
- 每条 review 意见都要有回应：改了就回复改在哪个提交，不改就说明原因。
- 需要跟进 `main` 的更新时用 `git merge origin/main`（保留合并记录），不要 rebase 别人已经看过的分支。

## 8. 常见错误对照

| 做法 | 后果 | 正确做法 |
|---|---|---|
| 把 zip 提交进仓库 | 二进制不可评审，仓库变大 | 提交解压后的源文件 |
| 所有者代为解压提交 | 作者变成所有者，组员贡献归零 | 组员自己提交 |
| `git add .` | 顺手带上 `.venv`、`__pycache__`、本地配置 | 按路径显式 `git add` |
| 一个提交打包三天的活 | 无法定位问题，无法单独回退 | 一个可解释的切片一个提交 |
| 提交信息写 `update`、`fix bug` | 半年后没人知道改了什么 | 祈使句主题 + 中文正文 + trailer |
| 本地不跑测试直接推 | CI 公开失败，来回好几轮 | 先跑完五道门 |
| 直接推 `main` | 绕过评审，无法留痕 | 一律走分支 + PR |
| 用 force-push “整理”历史 | 评审记录和修改过程消失 | 追加提交 |
| 改 `requirements.lock` 装自己要的包 | 锁校验失败，牵动整个发布流程 | 先提 issue 讨论依赖 |

## 9. 本次交付（Finance_Radar_UI_API_20260821.zip）的落位与校验值

压缩包 SHA-256：`3a3e5c8aad1d9ef52d352006c7cc27a085051223f8d3738798c28f377bc7789c`（18294 字节，4 个文件）

| 仓库路径 | 字节 | SHA-256 |
|---|---:|---|
| `ui_preview/finance-radar-ui-concept.html` | 58716 | `175d0c87baa25963cf838a5972d4479f26e66b9028daa3ce1a20b0b843b7454f` |
| `ui_preview/README.md` | 1471 | `bca4de40cd1fdfaa5dfd4c3a64a99fad88b5253661cff75abf4682c1da0c1c9f` |
| `scripts/serve_light_ui_preview.py` | 7260 | `eb5c134107994511d00120c73b1db4cad9c42f140ea6276e51abcbdcfff47cd4` |
| `tests/test_light_ui_preview.py` | 3087 | `ca3459c75260aed3992dd0ba33ec29ea367887082c7722cc6ba31dbfded9068d` |

这四个值属于这一份特定压缩包。如果重新导出，哈希会变，请重新取值并在 PR 里更新。

四个文件已确认符合入库要求：LF 换行、无 BOM、无行尾空格、文件末尾有换行；`compileall` 通过；
`tests/test_light_ui_preview.py` 三个用例全部通过。按第 3 节流程提交即可。

**先补 Backlog 条目。** 现有产品 Backlog 的 51 条 PBI 里没有一条覆盖“浅色只读 UI 接口预览”：
`PBI_07` 是已完成的深色五页证据终端，`PBI_20`/`PBI_22` 是首屏与中文文案，都不是这件事。
所以第 6 步里的 `PBI_52` / `Sprint4-T27` 是**待创建的编号**——请先在 `docs/scrum/README.md`
的产品 Backlog 和 Sprint4 任务表里补上这两行（说明用户故事、验收条件、估算与状态），
再在提交里引用它们。不要先提交代码再倒签编号。

> `docs/scrum/` 随 PR #21 进入 `main`。该 PR 合并之前，先和所有者确认新 PBI 的编号，避免撞号。

## 相关文档

- `docs/GITHUB_BACKUP_AND_RELEASE_WORKFLOW.md` —— 分支、标签与发布模型
- `docs/UI_AESTHETIC_DIRECTION.md` —— UI 审美方向与验收口径
- `docs/scrum/README.md` —— 产品 Backlog 与 Sprint Backlog（PBI 编号来源）
- `.agent/coding_conventions.md` —— 代码约定
- `.github/workflows/ci.yml` —— CI 门禁的唯一事实来源
