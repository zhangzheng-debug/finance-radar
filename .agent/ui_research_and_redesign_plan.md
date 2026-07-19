# Finance Radar UI 研究与改版方案 v1.0

更新时间：2026-07-18  
定位：只读金融事件情报与证据复核终端，不是交易终端，不提供下单、仓位、余额或账户能力。

## 1. 结论

界面方向定为 **Evidence Terminal / 证据情报终端**。

不照搬 Bloomberg 的复古橙黑外观，也不把 Finance Radar 做成充满大图表和大号 KPI 的普通数据看板。要借鉴的是专业终端的四个原则：

1. 同屏完成“发现—判断—核验”，避免在下拉框和多个页签之间往返。
2. 信息密度高，但每一种颜色和数字都有稳定语义。
3. 默认先显示事件、证据和时效；模型输出退居辅助位置。
4. 支持保存视图、键盘导航和可追溯的操作状态。

一句话视觉定义：**深海军蓝底色、冷白正文、青色交互、琥珀风险、红色阻断、绿色健康、紫色证据对象；弱圆角、细边框、等宽时间与数字。**

## 2. 外部产品研究：借什么，不借什么

### Bloomberg Terminal

- 借：持续可见的系统状态、密集多面板、稳定的键盘工作流、时间和数字对齐。
- 不借：复古橙黑配色、功能代码记忆负担、交易执行心智。

### Benzinga Pro / FinancialJuice / Newsquawk

- 借：中心是时间戳新闻流；Flash / Important / Data 等类别在扫视时即可区分；经济日历、新闻流和详情可以同屏；最重要消息可有声音或桌面提醒。
- 不借：把“快”当成唯一价值、把未经核验的快讯直接包装成可交易结论。

### TradingView News Flow

- 借：筛选条件组成可保存的 Flow；Flow 可以绑定提醒；支持按 watchlist、资产、国家、来源和内容格式过滤；使用键盘前后切换事件。
- 不借：围绕图表和交易品种组织全部信息。Finance Radar 应围绕“事件实体”组织。

### Koyfin / OpenBB / AlphaSense

- 借：可复用的工作区和视图；新闻、监控列表、图表与研究材料形成一个工作面；用户可以按任务保存布局。
- 不借：本阶段不做自由拖拽 Widget。Streamlit 中先提供 3 个固定工作区预设，效果更稳定、答辩也更可控。

### RavenPack

- 借：不要把复杂判断压成一个神秘总分；分别显示 relevance、novelty、impact、sentiment / polarity 等维度。
- 对 Finance Radar 的转译：分别显示 **事件风险、证据覆盖、来源权威、信息新颖度、冲突状态、模型置信度**。任何维度都不能自动变成交易指令。

### Dataminr

- 借：Flash / Urgent / Alert 的清晰分级、事件详情抽屉、来源与位置上下文、事件随时间演化的连续视图。
- 不借：地图不是本项目当前主场景，除非以后增加地缘事件或资产地理暴露。

### NewsLiquid

- 借：低延迟新闻分流适合做第一层“是否值得看”的排序器；多资产影响需要显式表达。
- 不借：不展示交易执行，不把模型分流分数写成收益承诺，也不以单一模型输出覆盖证据规则。

### 2.1 2026-07-18 可复核研究来源

本轮用公开搜索和 X 做发现，但设计判断优先以产品官方页面/帮助中心为依据；X 只证明从业者普遍讨论 watchlist、自动提醒和研究文件夹等工作流，不作为功能真实性的唯一来源。

- X 发现入口：[The Terminalist](https://x.com/TheTerminalist)。
- TradingView：[News Flow 产品说明](https://www.tradingview.com/support/solutions/43000728828-news-flow-your-daily-hub-for-financial-news/)、[分屏详情与键盘导航](https://www.tradingview.com/blog/en/customized-real-time-updates-news-flow-46582/)、[过滤维度](https://www.tradingview.com/support/solutions/43000732560-news-flow-s-filters-overview/)。
- Benzinga Pro：[Newsfeed 过滤规则](https://help.benzinga.com/en/articles/1769530-how-do-i-filter-my-newsfeed)、[工作区 Widget 结构](https://help.benzinga.com/en/articles/1769521-what-is-a-widget)。
- Koyfin：[自定义新闻视图](https://www.koyfin.com/help/custom-news-screens/)、[Dashboard / Watchlist / News 功能](https://www.koyfin.com/help/topic/functionality/)。
- OpenBB：[Widget 元数据、来源归属与参数联动](https://docs.openbb.co/workspace/analysts/widgets/overview)、[RSS/Atom 与研究工作区组件](https://docs.openbb.co/workspace/analysts/widgets/core-widgets)。
- RavenPack：[News Analytics 的 relevance、novelty、impact 多维分析](https://www.ravenpack.com/products/edge/data/news-analytics)。

这些来源共同支持当前取舍：列表与详情分屏、可保存 Flow、多维可解释状态、来源归属、全局新鲜度，以及“模型分数不能覆盖证据门”。

## 3. 当前 UI 审计

### 已经具备

- 深色基调与安全边界横幅。
- Situation Room、Event Intelligence、Replay Lab、Operations & Model 四个功能域已经完整。
- API 数据、事件、证据、Agent trace、回放、Worker 和备份状态都能展示。
- 演示模式和交易禁区在产品层面可见。

### 改造前主要短板（按影响排序，历史基线）

1. **核心工作流断裂**：事件通过下拉框选择，证据、时间线、市场观察和 Agent trace 分散在页签中，无法同屏判断。
2. **信息层级偏“普通看板”**：大标题、六张 KPI 卡、渐变背景和大圆角占用空间，专业终端密度不足。
3. **列表不可扫视**：默认 dataframe 缺少事件优先级、时间新鲜度、来源层级、证据覆盖和冲突状态的视觉编码。
4. **模型置信度过于显眼**：容易被误读为“交易信号强度”；应与证据覆盖、风险和新颖度并列，并明确 shadow / abstain。
5. **筛选不可复用**：没有保存 Flow、快速预设、watchlist 或键盘选择。
6. **运维状态割裂**：Worker、数据新鲜度、备份与 API 健康应缩成全局状态条，不应只藏在运维页。
7. **视觉语言仍像 Streamlit 默认样式**：emoji 页面图标、中英混排、默认控件和表格、12px 大圆角削弱专业感。

## 4. 页面信息架构

### 全局壳层

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ FINANCE RADAR  / 搜索事件、Ticker、公司、来源    LIVE  UTC+8  API● DB● WK● │ 48
├──────┬───────────────────────────────────────────────────────────────────────┤
│ Feed │ Flow: 全部重大事件  [风险] [SEC] [P0/P1] [近24h]     1185 events     │ 36
│ Work │───────────────────────────────────────────────────────────────────────│
│ Repl │ 事件流 44%             │ 证据工作台 36%             │ 上下文 20%      │
│ Ops  │ 时间 / 标签 / 标题     │ 摘要、声明—证据矩阵         │ 评分拆分          │
│      │ 来源 / 公司 / Ticker   │ 来源原文、冲突、时间线       │ 行情观察          │
│      │ 新鲜度 / 证据 / 风险   │ Agent 与人工复核（折叠）     │ 版本与审计        │
│      │                         │                              │                   │
├──────┴─────────────────────────┴──────────────────────────────┴───────────────────┤
│ READ ONLY · no trading · last event 42s · worker 5.2s · backup verified 10:58 │ 24
└──────────────────────────────────────────────────────────────────────────────┘
```

### 4.1 Situational Feed（默认首页）

- 不再用六张大 KPI 卡占据首屏。
- 顶部只保留一条 32px 状态带：总事件、待复核、最新年龄、数据源异常。
- 主体采用左侧事件流 + 右侧快速详情。点击列表行后不跳页即可看到证据摘要。
- 默认 Flow：`重大负面 / 待复核 / SEC / 近 24h / 数据源异常`。
- 每行固定结构：
  - `HH:mm:ss`（等宽）
  - 风险级别 `CRITICAL / HIGH / WATCH`
  - Ticker / 公司
  - 一行标题
  - 来源层级 `P0 / P1 / P2`
  - 证据 `3/4`
  - 新鲜度 `42s`
  - 冲突/修订图标

### 4.2 Event Workbench（核心答辩页）

- 左：可筛选的事件列表，支持 `J/K` 上下移动、`Enter` 打开、`/` 聚焦搜索。
- 中：事件摘要、原子声明、声明—证据矩阵。原始来源 URL 一键打开。
- 右：六维评分条、只读行情快照、当前版本、审计状态。
- Agent trace、人工覆盖、原始 JSON 默认折叠到下部抽屉，避免压过证据本体。

六维评分条：

| 维度 | 视觉 | 解释 |
|---|---|---|
| Risk | 琥珀到红色刻度 | 事件潜在破坏性，不是方向预测 |
| Evidence | 青色分段条 | 已支持声明 / 总声明 |
| Authority | 紫色标签 | 最高来源层级与来源组合 |
| Novelty | 蓝色刻度 | 相对已知事件新增信息量 |
| Conflict | 红色标记 + 文本 | 来源是否冲突或需要人工判断 |
| Model | 灰蓝色刻度 | shadow 模型置信度；允许 ABSTAIN |

### 4.3 Replay Lab

- 改为横向时间轴，不用一组全部展开的 expander。
- 每个步骤显示 `T+秒数 → 新证据 → 状态变化 → 模型辅助判断`。
- 右侧固定显示冻结预期、实际结果、同一下游路由和“无外网”标志。
- 提供“一键演示”节奏：开始、暂停、下一步、重置。

### 4.4 Operations

- 默认展示四个 SLO：最新事件年龄、Worker 周期、源错误数、备份可恢复状态。
- 数据源表按 `异常 > 过期 > 正常` 排序，先显示需要处理的项。
- 模型卡突出覆盖率、弃权率、限制与训练分布；不要把 accuracy 单独做成大号宣传数字。
- 保留硬边界审计和恢复演练证据。

## 5. 视觉规范

### 色彩 Token

| Token | 色值 | 用途 |
|---|---:|---|
| `bg.canvas` | `#071019` | 全局背景 |
| `bg.panel` | `#0B1624` | 一级面板 |
| `bg.raised` | `#101F30` | 选中、浮层 |
| `border.default` | `#1D3042` | 面板和表格边界 |
| `text.primary` | `#E8F0F7` | 主文字 |
| `text.secondary` | `#91A6B8` | 辅助文字 |
| `accent.interactive` | `#39C6F4` | 搜索、选中、链接 |
| `state.healthy` | `#4AD7A8` | 正常、通过 |
| `state.watch` | `#F0B65B` | 待复核、注意 |
| `state.risk` | `#FF657A` | 高风险、冲突、失败 |
| `state.evidence` | `#A78BFA` | 证据对象、权威层级 |

规则：绿色只表示“健康/通过”，不表示“做多”；红色只表示“风险/阻断”，不表示“做空”。颜色必须同时配合文字或图标。

### 字体与数字

- 中文/UI：`Inter, "Noto Sans SC", "Microsoft YaHei", sans-serif`。
- 时间、Ticker、ID、延迟、百分比：`"IBM Plex Mono", "JetBrains Mono", Consolas, monospace`。
- 正文 13px，表头/标签 11px，事件标题 13–14px；禁止首页 2rem 大标题。
- 数字使用 tabular-nums，时间统一到 UTC+8 并显式标注。

### 密度与形状

- 8px 基础栅格；主面板间距 8px，行高 32–38px。
- 面板圆角 6px，按钮圆角 4px；状态胶囊只用于短标签。
- 1px 边框负责分区，尽量不用阴影。
- 事件标题最多两行；详情文字可以完整展开。
- 动效 120–180ms；仅实时状态点允许轻微呼吸，不做大面积渐变动画。

## 6. Streamlit 落地策略

不在当前阶段迁移 React。先把 Streamlit 做到稳定、专业、可答辩：

1. 建立 `design_tokens.py` 和统一 CSS，覆盖字体、侧栏、控件、表格、tabs、metric、tooltip。
2. 建立可复用组件：`status_strip`、`event_row`、`score_rail`、`evidence_card`、`service_badge`。
3. 首页改为紧凑事件工作面；Event Intelligence 改为双/三栏，不再依赖事件下拉框。
4. 用 session state 保存 Flow、选中事件和演示模式；用 query params 保持可分享链接。
5. 受 Streamlit 表格交互限制的部分，优先用原生容器和按钮行实现；不为了“像终端”引入不可维护的前端组件。

## 7. 实施计划与验收

### P0：视觉地基与壳层（0.5–1 天）

- 统一主题 Token、字体、4–6px 圆角、细边框、紧凑控件。
- 去除 emoji、巨大标题、全屏渐变和大块安全横幅。
- 增加顶部/底部状态条；安全边界缩成常驻底栏文案。
- 验收：1366×768 首屏能同时看到状态、至少 8 条事件与快速详情。

### P1：核心事件工作台（1.5–2 天）

- 可选择的紧凑事件流；双/三栏联动；证据矩阵常驻。
- 六维评分拆分；模型标注 `SHADOW` / `ABSTAIN`。
- 预设 Flow 与 URL 状态保持。
- 验收：从发现事件到打开原文最多 2 次点击；不切页签即可完成初步核验。

### P2：回放与运维（1–1.5 天）

- 回放时间轴、一键演示节奏、结果对照。
- Operations 按异常排序，增加 SLO 状态与备份恢复证据。
- 验收：没有实时 SEC 事件时，3 分钟内可以完整演示固定案例与审计链。

### P3：专业性 QA（0.5–1 天）

- 1366×768、1920×1080 和窄屏检查。
- 键盘导航、色盲可辨识、长标题、空数据、API 失败和慢响应状态。
- 中文术语统一、时间格式统一、模型边界文案检查。
- 页面加载与交互不因为视觉改造显著变慢。

### 7.1 当前落地状态（2026-07-18 14:24 UTC）

| 阶段 | 状态 | 已落地证据 | 仍需 |
|---|---|---|---|
| P0 视觉地基 | DONE | 统一深色 Token、4–6px 圆角、细边框、紧凑状态条、四页一致壳层；1366×768 与 390×844 既有截图 | 无 |
| P1 事件工作台 | DONE | 三栏 Event Intelligence；完整 flow/family/q/limit/event_id 保存视图；事件、证据矩阵、六维 rail 和复核上下文同屏；上一条/下一条与 J/K、方向键、`/` 检索导航已使用 Streamlit v2 组件落地 | 无 |
| P2 回放与运维 | DONE | 四个冻结回放；SEC 官方更正可撤回告警资格；Run 后先显示一步，支持 Next step / Show all / Reset；本地 AppTest 真实点击通过；Operations 异常优先与恢复证据已上线 | 新控件真人浏览器截图 |
| P3 专业 QA | PARTIAL | 四页 AppTest；Event Workbench 自动页面级导航、空结果复位和安全异常态回归；焦点可见、ARIA 状态语义与 reduced-motion；既有桌面/移动浏览器矩阵；15/15 公网验收；release 20260718T151927Z 的 120/15 负载 p95 2.48s | 当前浏览器控制接口不可用，待恢复后补键盘导航与 Replay 新案例真人截图；1920×1080 视觉矩阵仍可增强 |

本阶段不因截图工具暂不可用而把运行/API/AppTest 证据包装成“浏览器视觉已通过”。

### 暂不做

- 自由拖拽工作区、复杂地图、订单簿、交易执行、账户资产、收益排行榜。
- 仅为炫技引入 Three.js、重动画或前端框架迁移。
- 把多个维度强行合成一个“买卖分数”。

## 8. 成功标准

改版后评委在 10 秒内应看懂：

1. 现在发生了什么；
2. 哪些事件最值得先看；
3. 证据来自哪里、是否足够、是否冲突；
4. 模型做了什么、没有做什么；
5. 没有现场事件时如何用 Replay 证明系统；
6. 系统是否健康、数据与历史是否可恢复；
7. 系统不具备交易能力。

这套界面不靠“像交易软件”获得专业感，而靠 **清晰的证据结构、实时状态、紧凑工作流和可审计边界** 获得专业感。
