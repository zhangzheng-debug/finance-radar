# Finance Radar UI 审美方向与打磨计划

更新日期：2026-07-19

## 1. 最终决策

采用 **Calm Institutional Intelligence Console / 冷静的机构情报终端**。

它应当像专业新闻与证据控制台，而不是零售炒币页面、通用数据大屏，也不是 Bloomberg Terminal 的视觉仿制品。专业感来自明确的信息层级、稳定的工作流、可验证的状态和高效的键盘操作，而不是霓虹、动画或无关图表。

首屏按顺序回答五个问题：

1. 刚刚发生了什么？
2. 信息来自哪里，证据等级如何？
3. 为什么进入待复核队列？
4. 相关只读行情是否新鲜，事件后如何变化？
5. 采集、模型、备份与人工工作流是否正常？

最终动作永远是 **查看证据、人工裁决、进入回放**，不是下单。

## 2. 竞品研究结论

### 2.1 可吸收的成熟模式

| 产品 | 值得吸收 | 本项目不采用 |
|---|---|---|
| Bloomberg Terminal | 功能优先、高对比、明确层级、键盘效率、渐进披露 | 为了“像终端”而复制老式拥挤感 |
| TradingView News Flow | 保存信息流、紧邻列表的筛选器、列表/正文分屏、键盘导航、新闻自动进入 | 以图表为中心，或把新闻直接解释成交易方向 |
| Benzinga Pro | 精确时间、关键词与来源过滤、悬停快捷动作、告警与连接状态 | 用红绿涨跌代替证据判断，或把音频评论当主证据 |
| Newsquawk | 连续快讯流、重要性分层、独立的人工解读通道 | 把“播报员判断”与原始事实混为一层 |
| Koyfin | 自定义新闻屏、列表/正文可调比例、术语高亮、持久化工作区 | 任意拖拽造成的面板杂乱和无决策价值的组件 |
| LSEG Workspace | 新闻、数据和分析并置；浏览器/桌面一致；搜索为最短入口 | 假装免费数据具有付费机构源的覆盖率与时延 |
| RavenPack | 将相关性、新颖性、影响和情绪拆为独立维度 | 合成一个无法解释的“万能分数” |
| Dataminr | 事件先发现、再验证；实时状态与可行动信息层分开 | 把算法告警呈现成已核验事实 |
| NewsLiquid | 极低延迟单条新闻分流、News/Strategy 分区 | 钱包、持仓、订单和自动交易控件 |

### 2.2 X 平台研究结论

X 适合作为候选事件发现层和产品更新观察渠道，不适合作为本项目的最终真相层。

- Koyfin 2026 年更新仍在加强可配置导航、收藏入口和表格列，说明专业产品正在减少查找成本，而不是不断扩大首页模块数量。
- Bloomberg 对 X 内容的专业处理是实时摄取后做实体、人物、话题映射，再进行筛选、补充元数据和新闻价值核验。
- 因此 Finance Radar 中的 X 帖子只能进入 `candidate/discovery`，必须与 P0/P1 原始来源、时间戳和证据状态并列显示。

### 2.3 研究转化成的产品原则

1. **Feed is the product**：事件流是主视觉，系统统计只能辅助。
2. **Master-detail without context loss**：切换事件时列表位置、筛选条件和当前信息流不能丢失。
3. **Scores stay plural**：证据权威、相关性、新颖性、冲突、模型置信度、工作流状态分别展示。
4. **Freshness is visible**：新闻、行情、Worker、来源游标均显示新鲜度；不可用时明确写不可用。
5. **Human action is explicit**：待复核、双人标注、仲裁和回放都必须能从页面层级看懂。
6. **Density is earned**：高密度只用于可比较、可扫描的信息；解释文本使用足够行高。
7. **No fake institutional theatre**：不放无数据来源的走势线、虚假 Level 2、模拟订单簿或装饰性地图。

## 3. 信息架构

保留五个工作区，但让名字、目的和主动作更一致。

| 工作区 | 用户要完成的事 | 首要信息 | 首要动作 |
|---|---|---|---|
| Situation Room | 扫描最新事件与系统态势 | 实时事件流、快速信息流、人工队列、来源脉搏 | 打开事件工作台 |
| Event Workbench | 核验一个事件 | 队列、原文证据、独立判断维度、只读行情 | 打开原始来源 / 提交人工判断 |
| Replay Lab | 证明系统可复现 | 固定输入、步骤、预期结果、真实结果 | 开始/重置回放 |
| Operations & Model | 解释系统是否可信 | 来源、Worker、备份、模型卡、硬边界 | 查看故障与证据 |
| Adjudication Studio | 完成独立双人标注 | 盲标状态、冲突、仲裁、冻结门槛 | 提交评审 |

### 3.1 页面骨架

```text
┌─ 左侧导航 ─┬─ 命令栏：产品 / 工作区 / 当前模式 ─────────────────────┐
│ Situation  │ 只读与模型边界                                             │
│ Workbench  ├─ 状态条：事件 / 新鲜度 / Worker / 来源 / 备份 ────────────┤
│ Replay     │ 快速信息流：全部｜待复核｜已核验｜弱证据｜已拒绝             │
│ Operations ├───────────────────────────────┬───────────────────────────┤
│ Adjudicate │ 主事件流 / 证据工作区          │ 人工队列 / 来源 / 行情上下文 │
└────────────┴───────────────────────────────┴───────────────────────────┘
```

Event Workbench 桌面端维持三列：事件队列 22%、证据与正文 53%、判断与行情 25%。小于 900px 时按“当前事件 → 判断维度 → 证据 → 队列”排序堆叠，而不是简单照桌面顺序堆叠。

## 4. 视觉系统

### 4.1 色彩

| Token | 色值 | 用途 |
|---|---:|---|
| Canvas | `#060C13` | 深色中性背景 |
| Panel | `#0A1420` | 基础面板 |
| Raised | `#0E1B29` | 选中、悬停、可交互表面 |
| Border | `#1B2D3D` | 结构边界 |
| Border Strong | `#294257` | 当前焦点附近的结构 |
| Text | `#E6EEF5` | 主文字 |
| Muted | `#879CAF` | 元数据 |
| Cyan | `#29BDE3` | 导航、焦点和当前项 |
| Green | `#3ED59F` | 已核验、健康；禁止表示“建议买入” |
| Amber | `#F0B35A` | 待复核、观察、数据延迟 |
| Red | `#FF6B7C` | 冲突、错误、越界；禁止表示“建议卖出” |
| Violet | `#9B8AFB` | 证据与模型元数据 |

禁止渐变、毛玻璃、发光边框、背景行情线和装饰性霓虹。状态色必须同时带文字标签。

### 4.2 字体与密度

- 中文和界面：`Segoe UI / Noto Sans SC / Microsoft YaHei`。
- 时间、ID、计数、来源等级、模型状态：等宽字体。
- 正文最小 12px；关键元数据最小 10px；不能用极小文字制造“专业感”。
- 标题采用句式大小写；全大写只用于不超过两个词的系统状态。
- 默认列表行高 58–76px；每行只保留两层：扫描信息和证据摘要。

### 4.3 栅格与间距

- 桌面内容区最大 1600px，12 列栅格，4px 基础间距单位。
- 面板圆角 4–5px；不使用大圆角卡片。
- 结构依赖 1px 边界和间距，不依赖大面积阴影。
- 同一层最多六个运行指标；超过后进入详情页或分组标签页。

## 5. 核心组件规范

### 5.1 命令栏

固定表达：`FINANCE RADAR / 当前工作区 / 模式`。模式只允许 `LIVE`、`RECENT_CAPTURE`、`REPLAY`、`READ ONLY`、`HUMAN ONLY` 等可审计状态。

### 5.2 快速信息流

Situation Room 顶部直接提供：全部事件、待复核、已核验、弱证据、已拒绝。每个入口带数量，进入 Event Workbench 后保留查询参数。后续再允许用户保存自定义 Flow。

### 5.3 事件行

固定顺序：

`UTC 时间 → 工作流状态 → 来源等级 → 事件族 → 来源 → 主体/标题 → 证据摘要`

禁止把模型分数放在标题之前。事件被选中后使用青色结构线或 Raised 背景，不使用红绿整行填充。

### 5.4 判断维度轨

至少分开显示：

- 风险路由：`RISK_REVIEW / NON_RISK / ABSTAIN`
- 证据数量与最高权威等级
- 新颖性：`NEW / REVISION`
- 证据冲突：`CLEAR / DETECTED`
- 模型置信度，并始终带 `SHADOW`
- 人工工作流：`CANDIDATE / VERIFIED / REJECTED`

不能合成一个“83 分”并让用户猜它表示什么。若以后展示市场影响分，应与证据可信度并排、但不相加。

### 5.5 只读行情上下文

只有当报价与当前事件明确关联且仍在新鲜度窗口内时才显示。建议结构：

`资产 / 提供商 / LIVE 或 DELAYED / 报价时刻 / T+5m / T+30m / T+1d`

无数据时显示 `UNAVAILABLE · 原因`，不画零值折线。走势图只用于解释事件后市场结果，不展示交易按钮、余额、订单或仓位。

### 5.6 证据卡

必须同时看见：权威等级、来源名称、证据状态、精确原文、发布时间与源链接。AI 摘要与原文使用不同表面，原文始终优先。

### 5.7 空状态和故障态

说明三件事：什么不可用、是否使用旧数据、下一次能做什么。不得用缓存结果伪装实时结果。

## 6. 动效、交互与可访问性

- 新事件最多轻微闪动两次；`prefers-reduced-motion` 下完全关闭。
- `/` 聚焦检索，`J/K` 或上下键移动事件，`Enter` 打开，`Esc` 返回列表焦点。
- 每页恰好一个可见 `h1`、一个主区域；页面导航使用唯一 navigation landmark。
- 可交互元素具备可读名称；焦点使用 2px 青色描边与非颜色光环。
- 390px 宽度不得横向溢出；移动端主要触点至少 44px。
- 普通文字达到 WCAG AA 4.5:1。
- 机器审计不能代替真实屏幕阅读器用户验收；后者作为外部课程证据保留。

## 7. 分阶段打磨计划

### P0：迁移安全与视觉基线

- [x] 本地保存加密服务器迁移包、恢复校验和恢复说明。
- [x] 保留 Situation Room、Event Workbench、Replay Lab、Operations & Model、Adjudication Studio 五页结构。
- [x] 使用冷静深蓝、独立状态色、等宽运行数据与无交易边界。
- [x] 上一公共版完成桌面、移动、键盘、对比度、焦点和溢出机器审计；当前物料UI版保持待刷新，不复用旧截图冒充。
- [x] 在 Situation Room 增加带数量的快速信息流入口。
- [x] Event Workbench 将只读行情从原始表格折叠区提升为可见上下文卡，明确提供商、币种、采集年龄和不可用原因。
- [x] Operations 将事件源与行情提供商分栏，显示 Binance、Twelve Data 与 IBKR 的不同角色和真实观测状态。
- [x] Operations 增加“证据存档”页签，分开显示官方原始HTML/PDF快照、精确引文、存档字节、MIME和SHA-256完整性状态；明确快照不等于自动核验。
- [x] Situation Room 增加全终端检索；事件流强制进入“全部事件”，URL筛选覆盖旧控件状态，避免深链被残留会话吞掉。
- [x] 新增只读 `/events/facets` 聚合；Event Workbench 提供事件族模糊联想与来源精确筛选，Situation Room 以实时统计生成 FAMILY/SOURCE/Replay/Operations 命令条。
- [x] 行情卡增加 T+5m/T+30m/T+1d 三窗口；以首个真实快照为基线，错过窗口显示 `MISSED`，不以最新价补洞。

### P1：优先补齐的 UI 木桶短板

1. **事件工作台固定主从关系**：桌面端让队列、正文、判断三列在一屏形成完整闭环；移动端调整阅读顺序。
2. **事件后窗口增强**：T+5m / T+30m / T+1d 的调度、错过保护和可见状态已完成；下一阶段等待新事件自然形成按时三窗口，再加入轻量微型走势。
3. **自定义 Flow 持久化**：已完成本机浏览器版——最多保存8个命名Flow，持久化状态、事件族、来源、关键词和数量，支持恢复/删除；不上传正文、身份或交易数据。跨设备账号同步与告警开关仍不做，避免为了展示引入账户系统。
4. **搜索增强**：统一全局搜索、来源/事件族联想、来源精确筛选和数据驱动命令动作均已完成；后续只在真实使用中扩充同义词，不引入不可审计的语义搜索黑箱。
5. **中英文一致性**：中文作业场景用中文主标签，英文只保留行业术语和可审计状态。
6. **真实故障示例**：Operations 同屏保留一次来源退化和一次恢复证据，形成答辩故事。

### P2：完成核心流程后再做

1. 保存并恢复操作者工作区，不开放任意拖拽面板。
2. 高权威、高影响事件可试验语音播报，但原始证据必须一键可达。
3. 加入真实屏幕阅读器用户和键盘用户验收记录。
4. 如事件数据规模足够，再加入密度视图和事件后表现小型图；不提前造图。

## 8. 验收标准

一次 UI 改动只有同时满足以下条件才算完成：

1. 首屏 10 秒内能找到最新事件、待复核数量、系统是否新鲜。
2. 30 秒内能从事件进入原始证据并说清为什么被分流。
3. 桌面 1366×768、1920×1080 与移动 390×844 无横向溢出。
4. 五页无控制台错误、页面错误和 HTTP 错误。
5. 键盘导航、深链、回放与只读边界不退化。
6. 新增颜色不承担唯一语义；模型输出始终显示 `SHADOW`。
7. 没有行情时诚实显示不可用，不使用虚构或陈旧数据。

## 9. 本项目当前判断

当前版本已经具备专业终端的结构基线：事件流是主视觉，证据和模型维度分离，运行与边界可观测，桌面/移动与键盘路径已有自动验收。只读行情上下文现已统一为“提供商、资产、报价币种、采集年龄、不可用原因”，并在 Operations 中与事件源分开；原始证据存档也已成为独立可审计面板。自定义Flow现可在本机浏览器命名保存、恢复和删除，且不产生服务器写入。来源/事件族联想、来源精确筛选和数据驱动命令条已补齐。当前最明显的 UI 短板收敛为 **事件后多窗口指标尚未形成完整自然时间样本、少量语言层级仍有混用，以及当前release浏览器视觉矩阵待刷新**。后续应继续按 P1 顺序补齐，避免堆新页面。

## 10. 主要参考

- Bloomberg UX 与功能优先：https://www.bloomberg.com/company/stories/bloombergs-customer-centric-design-ethos/
- Bloomberg 对 X 流的映射与筛选：https://partners.x.com/en/partners/bloomberg
- TradingView News Flow：https://www.tradingview.com/support/solutions/43000728828-news-flow-your-daily-hub-for-financial-news/
- TradingView 筛选器：https://www.tradingview.com/support/solutions/43000732560-news-flow-s-filters-overview/
- Benzinga Pro Newsfeed：https://help.benzinga.com/en/articles/1413278-getting-started-newsfeed
- Benzinga Squawk 连接状态：https://help.benzinga.com/en/articles/2106004-what-is-squawk-and-how-do-i-use-it
- Koyfin Custom News Screens：https://www.koyfin.com/help/custom-news-screens/
- Koyfin Company News：https://www.koyfin.com/help/company-news-2/
- Koyfin 2026 导航更新：https://www.koyfin.com/help/release-notes/customizable-left-navigation/
- LSEG Workspace：https://www.lseg.com/en/data-analytics/products/workspace
- RavenPack News Analytics：https://marketing-prod.ravenpack.com/products/edge/data/news-analytics
- Dataminr for News：https://www.dataminr.com/products/dataminr-for-news/
- NewsLiquid 实时新闻产品：https://app.newsliquid.com/
- NewsLiquid 低延迟模型说明：https://app.newsliquid.com/blog/newsliquid-2-0-flash
