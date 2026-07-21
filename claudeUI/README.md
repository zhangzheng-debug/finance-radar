# claudeUI · Finance Radar UI 升级工作区

> Claude 独立工作目录。只包含新增文件，未改动主工程任何代码。
> 最终由 codex 决定合入方式与时机。

## 目录

```text
claudeUI/
├── README.md                    本文件
├── RECOMMENDATIONS.md           项目后续开展建议书（产品/UI/模型/工程四维度）
├── design/
│   ├── DESIGN_SYSTEM.md         Calm Institutional v2 "Obsidian" 设计系统规范
│   ├── tokens.css               唯一权威设计令牌（--fr-* 变量，向后兼容）
│   └── verify_tokens.py         校验原型内联 token 与权威源同步
├── prototype/
│   ├── index.html               Evidence Terminal v2 完整交互原型
│   └── dev_server.py            本地静态服务 + 只读实时 API 代理
└── streamlit_patch/
    ├── style_v2.css             现有五页终端的 CSS 整体替换稿（零 DOM 改动）
    └── INTEGRATION.md           codex 集成步骤 + QA 门提示
```

## 三件交付物是什么关系

1. **`prototype/index.html` — 设计目标**。零依赖单文件（无 React、无构建、
   无外部资源，符合 spec v5.1 技术栈停止规则），内置合成演示快照，
   浏览器直接打开即可看到目标形态：
   - 五个页面：态势室 / 事件工作台（三栏）/ 回放实验室 / 运行与模型 / 盲标裁决
   - 交互：J/K 事件切换、`/` 聚焦检索、Ctrl+K 命令面板、回放模拟时钟、
     筛选与冲突硬门横幅
   - 页面右上角 DEMO 徽章可点击：同源部署时自动切换 LIVE 核心只读模式，
     加载态势、来源、事件、所选详情、证据与时间线
   - DEMO 回退数据为**合成演示快照**（公司与事件虚构，规模数字对齐
     2026-07-19 已接受快照）；LIVE 与 DEMO 状态在徽章和页脚中明确区分
2. **`streamlit_patch/` — 现在就能落地的部分**。把原型的视觉语言
   反向移植回现有 Streamlit 五页终端：一次纯 CSS 字符串替换 +
   两处可选单行 markup 增强。详见 INTEGRATION.md。
3. **`design/` — 两者共同的规范**。tokens、组件规范、
   与现有类名的迁移映射表、分三批的落地顺序。

## 本地运行与实时 API

直接双击 `index.html` 会保持 DEMO 模式。浏览器会阻止普通本地静态服务跨域
读取线上 API，因此实时验收请运行：

```powershell
python .\claudeUI\prototype\dev_server.py
```

然后打开 `http://127.0.0.1:8765/index.html`。该代理仅允许 GET/HEAD，所有
POST/PUT/PATCH/DELETE 请求均返回 405。

- `LIVE CORE · READ ONLY`：态势、来源、事件、所选详情、证据及时间线来自实时 API；
- `DEMO · API OFFLINE`：API 不可达，页面明确保持合成快照；
- 回放与盲标样例始终是冻结夹具，不会伪装成实时事件；
- 顶部 `PRESENT` 可开启答辩投影高可读模式。

## 已完成的验证（原型）

- 桌面 1280×800 与移动 375×812 均无水平溢出
- 五个视图全部渲染（7 KPI / 10 事件行 / 22 来源瓦片 / 4 回放案例 / 22 行来源表）
- 交互验证：J/K 导航、事件族筛选、冲突硬门横幅、回放播放至 T+00:21 完整 5 步、
  Ctrl+K 面板开合（17 条命令）、Esc 关闭
- 控制台零报错；对比度按 4.5:1 下限复核

## 设计参考来源

- Bloomberg 终端设计语言（密集表格、等宽数字、暗色优先）及其开源仿作
- [OpenBB Workspace](https://openbb.co/products/workspace/)（组件化金融工作台）
- 2026 年 Fintech 仪表盘趋势（[AdminLTE 榜单](https://adminlte.io/blog/fintech-banking-dashboard-templates/)、
  [TailAdmin 金融模板](https://tailadmin.com/blog/finance-dashboard-templates)）：
  OKLCh token 化暗色、密度切换、shadcn 风格组件

## 给 codex 的最短路径

1. 读 `streamlit_patch/INTEGRATION.md`，第一批（纯 CSS）与本轮
   浏览器 QA 矩阵刷新合并为一次提交。
2. 批次 2/3 见 `design/DESIGN_SYSTEM.md` 第 8 节。
3. 产品层面的优先级判断见 `RECOMMENDATIONS.md`。
