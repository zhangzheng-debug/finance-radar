> **历史设计档案，非部署方案。** `prototype/index.html` 只保留为冻结的
> 设计材料，不能直连生产 API、不能由 FastAPI/Nginx 提供，也不能与
> Streamlit 并行部署。任何可复用设计原则都须在当前单一 UI 中重新评审。

# Finance Radar 设计系统 · Calm Institutional v2「Obsidian」

> 目标读者：codex（整合方）与后续维护者。
> 本文档定义视觉升级的完整规范；`tokens.css` 是唯一权威变量源；
> `../prototype/index.html` 是可交互的设计目标（design target）；
> `../streamlit_patch/` 是可以直接落到现有 Streamlit 五页终端的最小化补丁。

---

## 1. 设计理念

现有 UI 已经是正确的方向（深色终端、等宽数字、状态色语义、无交易边界横幅）。
v2 不是推翻，而是把它从「工程师配色」提升到「机构级终端」。四条原则：

1. **证据优先（Evidence-first）**：这是证据链情报系统，不是行情软件。
   界面最亮、最贵的视觉资源必须给：精确证据段落、权威层级、冲突状态、
   人工复核队列。行情永远是配角（右侧栏、小卡片）。
2. **密度即尊重（Density is respect）**：参考 Bloomberg / Fortress 类终端：
   密集表格 + 等宽数字 + 40px 行高。用户是分析师，一屏能看 20 行事件
   比大留白重要。留白只用于分组，不用于装饰。
3. **双通道语义（Color + Shape）**：权威（P0/P1/P2）与极性（利空/利好/中性/混合）
   是两个独立维度，永不合并成一个"买卖分"。极性除颜色外必须带形状
   （▼ ▲ ◆ ◈），色盲用户也能读。这同时满足现有 a11y 审计门。
4. **诚实状态（Honest states）**：MISSED_WINDOW、ABSTAIN、SHADOW、
   NOT_READY 这类"不好看"的状态是本系统的核心卖点，要用设计强调
   而不是遮掩——虚线边框、显式标签，绝不用旧数据或空白顶替。

## 2. 色彩

### 2.1 画布层级（亮度分层代替阴影）

| Token | 值 | 用途 |
|---|---|---|
| `--fr-canvas` | `#05090f` | 页面底色 |
| `--fr-panel` | `#0b1220` | 卡片、面板 |
| `--fr-raised` | `#101a2b` | hover、选中行 |
| `--fr-overlay` | `#141f33` | 命令面板、弹层（唯一允许投影的层） |

规则：层级差就是亮度差；同层元素禁止随意加投影。

### 2.2 状态语义（值的颜色）

`ok=green · watch=amber · risk=red · evidence=purple · interactive=cyan`
与现有 `.status-value.ok/.watch/.risk` 类完全兼容，只更新色值。

### 2.3 权威层级（来源，不是情绪）

| 层级 | Token | 视觉 |
|---|---|---|
| P0 官方一手 | `--fr-p0` 制度金 `#e9c46a` | 实心描边 chip，最醒目 |
| P1 发行人 | `--fr-p1` 冰蓝 `#8ecae6` | 普通 chip |
| P2 发现源 | `--fr-p2` 灰蓝 `#6d8196` | 弱化 chip |

### 2.4 极性（方向，配形状双通道）

`▼ 利空 red · ▲ 利好 green · ◆ 中性 gray · ◈ 混合/冲突 amber`
形状永远与颜色同时出现。**权威 chip 和极性 glyph 在事件行里分列两个位置，
布局上就不可能被读成一个综合分。**

## 3. 字体

- UI 文字：`--fr-font-ui`（Inter / Segoe UI / Noto Sans SC）
- **所有数字、时间戳、ID、机器枚举**：`--fr-font-mono` + `tabular-nums`
- 中文操作层 + 英文机器枚举并存是本项目特色，规范为：
  中文用 UI 字体常规字重；机器枚举（`RISK_REVIEW`、`MISSED_WINDOW`）
  一律等宽字体、大写、11px、letter-spacing .04em——一眼可分辨"这是机器状态"。

字号阶：`10 / 11 / 12 / 13 / 15 / 18 / 24`（见 tokens）。
现有 UI 大量 `.54rem–.65rem`（≈8.6–10.4px）过小，v2 下限提到 10px，
且 10px 只允许全大写拉丁字符/数字（中文最小 12px）。

## 4. 布局

### 4.1 页面骨架（原型采用；Streamlit 可渐进靠拢）

```
┌────────────────────────────────────────────────────────────┐
│ 顶部命令条: 品牌 · 页面Tabs · 全局检索(/) · 模式徽章 · UTC时钟 │
├────────────────────────────────────────────────────────────┤
│ KPI 状态带（6-8 格，含迷你趋势线）                            │
├──────────────────────────────┬─────────────────────────────┤
│ 主工作区（事件流 / 证据矩阵）   │ 决策右栏（队列/来源健康/行情） │
├──────────────────────────────┴─────────────────────────────┤
│ 边界页脚: 无交易边界 · Schema · quick_check · 快照时间        │
└────────────────────────────────────────────────────────────┘
```

侧边栏导航改为顶部 Tabs 的理由：五个页面是固定的平级工作面，顶部 Tabs
省出 ~240px 横向空间给事件表——分析终端的通行做法（Bloomberg、
TradingView、OpenBB Workspace 均为顶部导航）。Streamlit 原生 sidebar
短期保留亦可，原型演示的是目标形态。

### 4.2 Event Workbench 三栏

`左 300px 事件列表（J/K 键导航） · 中间自适应 证据矩阵 · 右 320px 决策上下文`
窄屏（<1100px）右栏下沉为中栏底部分区；移动端单栏顺排。

## 5. 核心组件规范

| 组件 | 规范要点 |
|---|---|
| KPI 格 `status-strip` | 标签 10px 大写 muted；值 18px mono；可选 24×56 迷你 sparkline；状态色只上到值，不上到底色 |
| 事件行 `feed-row` | 网格 `时间(56px) 极性glyph(20px) 主体+chips(1fr) 证据数(64px)`；行高 ≥40px；hover→raised；选中行左侧 2px cyan 内边线 |
| chip | 高 18px，radius 4px，11px mono 大写；变体：authority（描边）/ family（填充 dim）/ status（文字色） |
| 证据卡 `evidence-card` | 左 3px purple 边；头部：来源+权威+SHA 前 12 位（mono 10px）；正文引用段落 13px/1.6；冲突卡切换红边 + `◈ CONFLICT` 徽章 |
| 硬门横幅 | 证据不足/冲突时占满中栏宽度，amber/red dim 底 + 实色左边，文案说明"为什么不能自动升级" |
| 时间线 | 竖向，节点 glyph：○ 观测 → ◉ 修订 → ◆ 证据 → ■ 门禁；supersedes 用虚线连接 |
| 行情卡 `market-context-card` | UNAVAILABLE/MISSED 一律虚线边框 + 显式标签；T+ 三窗口横排 mono；绝不显示涨跌箭头颜色（防止读成信号） |
| 命令面板 | Ctrl+K / `/` 唤起 overlay 层；数据驱动（页面、事件族、来源、动作）；上下键 + 回车 |
| 空态/故障态 | 沿用现有"不伪装数据"文案规范；图形上用居中 glyph + 两行说明，禁止骨架屏假装在加载 |

## 6. 动效与可达性

- 动效只有两档：`120ms`（hover/按压）与 `200ms`（弹层/展开），缓动 `--fr-ease`。
- 保留并继承现有 a11y 合同：`:focus-visible` 2px cyan 外框 + 3px 光晕；
  触控目标 ≥40px（移动 44px）；`prefers-reduced-motion` 全部归零。
- 对比度：所有文字色相对其底色 ≥ 4.5:1（`--fr-muted` 在 panel 上为 4.6:1，已验证）。
- 状态永不只靠颜色：risk 值旁总有文字枚举或形状 glyph。

## 7. 与现有代码的映射（给 codex 的迁移表)

| 现有（common.py / components.py） | v2 动作 |
|---|---|
| `:root` 7 个颜色变量 | 直接替换为 tokens.css 的对应值（名称兼容，新增变量按需取用） |
| `.status-strip / .status-item` | 保留结构；值字号升到 18px，加 `tabular-nums` |
| `.feed-row` 网格 | 在时间列后插入极性 glyph 列；行高从 auto 提到 ≥40px |
| `.feed-tag` | 拆分为 `.chip-authority / .chip-family / .chip-status` 三个变体 |
| `.command-palette`（横向链接条） | 保留；另加 Ctrl+K overlay（st.components.v2 组件，参考 saved_flow 实现方式） |
| `.evidence-card` | 头部加 SHA 短哈希 + 权威 chip；冲突态样式 |
| `.market-horizon-value.risk` (MISSED) | 加 `text-decoration: none` + 显式 `MISSED` 文案（已有）+ 虚线卡边 |
| Streamlit 默认图表（浅蓝柱状图） | 改用主题色序列 `[cyan, green, amber, red, purple]`，背景透明 |
| sidebar 导航 | 短期：保留但压缩宽度、导航项加图标；长期：顶部 Tabs（见原型） |

## 8. 落地顺序建议（尊重现有验收门）

任何 CSS 改动都会触发「当前 release 浏览器视觉/交互/无障碍矩阵必须刷新」
（spec v5.1 P1_UI_QA）。因此：

1. **第一批（低风险，一次 QA 刷新）**：替换 tokens 色值 + 字号阶 +
   KPI/feed-row/chip 三个组件升级 + 图表配色。全部是 `install_style()`
   内 CSS 字符串改动，不动 DOM 结构，AppTest 结构回归不受影响。
2. **第二批**：命令面板 overlay、时间线 glyph、冲突卡。新增两个
   v2 组件（有 saved_flow 先例），需补 AppTest。
3. **第三批（原型形态）**：顶部导航 + 三栏 Workbench。若未来重新评审，
   只能将必要的交互原则迁入当前单一 Streamlit UI；不得恢复原型静态壳或
   新增第二条公开部署路径。
