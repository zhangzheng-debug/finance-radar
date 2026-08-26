# 公开层真实用户体验实测 · 2026-08-26

- 目标版本：`2026.08.25.1`
- 提交：`f775ae4306ed396c7ba9ee2b5761aae7eaa7b7ab`
- 方式：Chromium（Playwright 驱动）真实点击，非快照比对
- 视口：`1440x1000` / `820x1180` / `390x844`
- 账本规模：本次实采 **37 条**（生产为 14,506 条）
- 结论性质：**体验评审，不是发布验收**。本文不更新 `CURRENT_STATE.md`，不代表生产健康状态。

> 本报告面向代码审查。每条结论都附实测数字与复现方式，`measurements.json` 为机器可读版本。
> 结论若与数据量相关，正文单独标注。

---

## 0. 公网没有进去：先说清楚边界

**没有测到生产公网。** `edge.zb1og.cn` 现在由 Cloudflare 前置并启用 managed challenge：

```
GET https://edge.zb1og.cn/radar/
→ HTTP 403,  cf-mitigated: challenge,  server: cloudflare
   页面停在「正在进行安全验证」
```

真人浏览器通常两秒通过。本测试会话没有通过，原因是**测试环境的出网策略**，不是产品缺陷：

```
REQFAIL  https://brunhild.challenges.cloudflare.com/cdn-cgi/challenge-platform/...
         net::ERR_TUNNEL_CONNECTION_FAILED
proxy    gateway answered 502 to CONNECT   ← 出网网关拒绝该域名
```

`brunhild.challenges.cloudflare.com` 是 Turnstile 完成验证必须访问的域名。它被拒绝 → 拿不到 `cf_clearance` → 页面永远停在验证页。**没有绕过该限制**（尝试过 origin 直连、Host 头改写、`/etc/hosts` 固定，均未采用或不生效；出网代理自行解析 DNS，`/etc/hosts` 不影响它）。

**替代方案：** 用生产**同一提交**在本地起 API + Streamlit，跑一轮真实采集后用真浏览器点。

```
VERSION 文件            2026.08.25.1
API /health 自报        2026.08.25.1
git rev-parse HEAD      f775ae4306ed396c7ba9ee2b5761aae7eaa7b7ab   ← 与截图中生产部署提交一致
```

采集为真实外网响应，非 mock：Federal Reserve(20) · SEC current filings(9) · BLS(4) · CFTC(4) · FDA MedWatch(17) · FTC(8) · SEC litigation(8) · SEC trading suspensions(1) · FDIC(9) · NVIDIA IR(20) · ECB(9) · ECB statistical(4) · EIA(3) · opennews(150) → 抽出 **37 条 canonical 事件**。

**因此：代码路径与生产一致，数据真实，仅规模不同（37 vs 14,506）。**

---

## 问题清单（按对使用体验的影响排序）

| # | 问题 | 影响 | 性质 | 建议处理 |
|---|---|---|---|---|
| 01 | 事件标题是公司名，不是事件 | 高 | 数据契约 / 浏览主循环 | 需产品决策 |
| 02 | 翻页后滚动位置被重置到顶部 | 高 | 前端 Bug | 可直接修 |
| 03 | 每页 19.5% 正文是重复样板话 | 中 | 信息密度 | 需产品决策 |
| 04 | 详情页把同一句捕获文本渲染两遍 | 中 | 渲染 Bug | 可直接修（一行） |
| 05 | 卡片左栏 `UTC` 与事件标题重叠 | 中 | CSS Bug | 可直接修 |
| 06 | 首屏 1,058px 全是外壳，无事件 | 中 | 信息密度 | 需产品决策 |
| 07 | 音乐会通告被归入「宏观政策」 | 中 | 抽取质量 | 需规则补充 |
| 08 | 慢的是前端，后端在毫秒级 | — | 性能归因 | 影响优化排序 |

---

## 01 · 事件标题是公司名，不是事件 【高】

首屏往下滑，卡片标题依次是：`Iran` / `Iran` / `Iran` / `Federal Reserve` / `Federal Reserve` / `European Central Bank` / `European Central Bank` …

```
GET /api/v1/events?limit=40
37 条事件 → 仅 17 个不同标题
  9x  Iran
  9x  Federal Reserve
  4x  European Central Bank
  2x  Federal Deposit Insurance Corporation
```

证据：`event_feed_repeated_titles_1440x1000.png`

**根因在数据契约，不在 CSS。** 卡片标题取 `company_name`。而 `SOURCE_CAPTURED` 状态的事件：

```
public_fact_summary  = null      （37/37 全为 null）
claim_subject        = null
claim_action         = null
claim_stage          = null
unverified_capture_excerpt = "UK Chancellor on Iran Sanctions - Call on Iran to Cease…"
summary_basis        = "UNVERIFIED_CAPTURE_EXCERPT"
```

唯一有信息量的字段被放进了正文，标题只剩实体名。列表的作用本是让人不必逐条阅读，现在必须逐条读正文才能区分。

**建议：** 无 `public_fact_summary` 时，标题回退到 `unverified_capture_excerpt` 前 40–50 字（截断加省略号），把实体名降级为 chip，与「宏观政策」「监管动态」并排。

**为什么这样不破坏证据门：** 标题文本来源仍是 `UNVERIFIED_CAPTURE_EXCERPT`，卡片上「仅捕获来源」标签不变，`citation_ready` 不变，不产生任何新的事实断言——只是把已经展示在正文里的同一段文字挪到更容易扫读的位置。

**需要 Codex 判断的点：** 标题位置承载未核验文本，是否违反现有公开语义合同？若违反，替代方案是标题保持 `company_name` 但强制附加 `event_type` 中文标签 + 事件日（如「欧洲央行 · 统计发布 · 08-11」），至少让同名条目可区分。

---

## 02 · 翻页后滚动位置被重置到顶部 【高】

滚到底部点「下一页 →」，数据正确（第 2 页 13 条），但滚动位置归零：

```
点击前   section.stMain.scrollTop = 4238    （「下一页」在视口 y=826）
点击后   section.stMain.scrollTop = 0
         「事件浏览」表头落在视口 y=1058   → 视口高 1000，已在屏幕外
URL      ...&preview_page=2#live-events    （锚点在，但未被执行）
```

证据：`after_pagination_scroll_reset_1440x1000.png`

用户每翻一页要重新下滑 1,000+ px 才能看到刚翻出来的内容。

**测量陷阱（重要）：** Streamlit 使用内部滚动容器 `section.stMain`，`window.scrollY` **恒为 0**。若用 `window.scrollY` 判定会得出「深链接也坏了」的错误结论——实测深链接是**正常**的：带 `preview_event_id` 的 URL 在新标签页能正确定位到事件预览。坏的只有翻页这一条路径。

**建议：** 二选一
1. rerun 后显式 `document.querySelector('section.stMain')` 滚动到 `#live-events`；
2. 分页控件在列表**顶部**再放一份——顺带解决「翻页必须先滑到底」。

方案 2 更稳（不依赖 rerun 时序），建议优先。

---

## 03 · 每页 19.5% 正文是重复样板话 【中】

```
「这只说明系统采集到了什么，不等于原文已经支持一条确定事实。」            × 24  (29 字)
「为什么关注：先确认具体动作、阶段和原始来源，再判断这条⟨类别⟩线索是否值得关注。」 × 24  (40 字)

列表区正文 8,496 字 · 样板 1,658 字 → 19.5%
```

两句话本身正确，态度也对。问题是它们对**每一条都成立**，因此放在每一条上等于零信息量，只是把真正有差别的摘录往下挤。

第二句更值得注意：标题叫「为什么关注」，内容却是通用流程指引，读完并不知道这条为什么值得关注。它占据的是卡片上最贵的位置。

**建议：** 第一句提到列表表头讲一次（「以下均为仅捕获来源」），卡片只保留已经很清楚的「仅捕获来源」标签；第二句要么按 `event_type` 给出真正不同的理由，要么删除。

---

## 04 · 详情页把同一句捕获文本渲染两遍 【中】

```
GET /api/v1/events/FR-LIVE-fdc003557677de1b1a2c40167e10eaeb/capture-explanation
  state          : ELIGIBLE_NOT_QUEUED
  source_title   : "UK Chancellor on Iran Sanctions - Call on Iran to Cease Its…"
  source_excerpt : "UK Chancellor on Iran Sanctions - Call on Iran to Cease Its…"
  IDENTICAL: True
```

证据：`event_preview_duplicate_capture_text_1440x1000.png`

`app/web/Home.py:197-199` 无条件同时渲染两个字段：

```python
f'<h3>{escape(source_title)}</h3>'
f'<p>{escape(source_excerpt)}</p>'
```

对通讯社类捕获，标题即全文，两字段本就相等 → 同一句话打印两遍。

**建议：** 渲染前比较，相等则只渲染其一。一行判断。

---

## 05 · 卡片左栏 `UTC` 与事件标题重叠 【中】

1440px 下，左侧「最后更新 / 08-26 02:06 / UTC」的第三行 `UTC` 与事件标题起始位置视觉重叠。在抓到的每张卡片上均可复现，非偶发。

证据：`card_utc_title_overlap_1440x1000.png`

**建议：** 给左侧 meta 栏足够 `min-width`，或把 `UTC` 并入时间行（`white-space: nowrap` + 更小字号）。

---

## 06 · 首屏 1,058px 全是外壳 【中】

```
整页高                5,238 px
第一条事件出现在      1,058 px    → 1440x1000 屏幕首屏看不到任何事件
```

四层标题带堆叠：侧栏 logo → 主标题带 → no-trading 横幅 → EVIDENCE DESK 带。同一数字反复陈述：

```
「37」在首屏出现 5 次：
  · 账本中的 37 条事件现在全部可以浏览…其余 37 条…
  · 当前可见性 / 全部可浏览 37
  · 事件总量 37 条
  · 正式可引用 / 其他证据姿态 0 / 37
  · 全部可浏览 37（黄框）

「0 条达到正式引用条件」换 3 种说法各讲一遍
```

另：侧栏底部「当前工作面 / 态势总览 / 浏览事件、证据摘要与更新状态」与上方已高亮的导航项**逐字重复**，可直接删除。

证据：`home_1440x1000.png`

**建议：** 四层标题带压成一层（产品名 + 当前页 + 一句边界声明）；计数只保留 `0 / 37` 一组。目标：第一条事件出现在 500px 以内。

---

## 07 · 音乐会通告被归入「宏观政策」 【中】

事件流中实际出现，归类为「宏观政策 · 欧洲央行公告」：

```
ECB and Frankfurt Radio Symphony invite the public to
Europa Open Air concert on 20 August 2026
```

来源确为 ECB 官方 RSS，抓取无误，但不应进入金融事件雷达主流。同轮还有例行统计发布、活动通告等若干条。

本轮 `candidate_extraction` 已拦下 `subject_filtered: 47`，说明该层存在，只是动作词表覆盖不足。

**建议：** 抽取阶段增加「无金融动作」判定——标题中不含任何可映射到 event family 的动作词（发布/处罚/裁定/暂停/收购/破产/召回/制裁…）则直接丢弃，而非落成 `SOURCE_CAPTURED` 事件。

---

## 08 · 慢的是前端，后端在毫秒级 【性能归因】

```
API（37 条账本，三次取中位）
  GET /api/v1/overview          3.7 ms
  GET /api/v1/events?limit=24   6.9 ms
  GET /api/v1/events/facets     2.4 ms

浏览器实际感受
  首屏完全可用                  6.8 s
  切换到证据演示                5.6 s
  翻一页                        4.5 s
```

后端毫秒级，用户感受秒级，中间**三个数量级**消耗在 Streamlit rerun + websocket 重绘。

**含义：** 继续优化 SQL 索引与查询对上述数字帮助有限；真正的杠杆是减少每次交互触发的全页重算量（分页、筛选、翻页目前均走 rerun）。

**规模口径：** 这是 37 条账本的数字。生产 14,506 条时 API 侧必然更慢，本测试无法验证。但「前端开销远大于后端」这一结论在两个量级下都成立。

---

## 通过的检查

| 检查 | 结果 |
|---|---|
| 外部 CDN 请求 | **0**（含字体，全部本地托管；对国内访问是实打实优势） |
| 失败请求 | 0 |
| Material Symbols 字体加载 | 已加载（`document.fonts.check` 为 true；`innerText` 中出现 `keyboard_arrow_right` 是 ligature 文本的正常现象，**不是 Bug**） |
| 搜索 `SEC` | 7 条，筛选准确 |
| 搜索不存在词 | 0 条，空态诚实：「当前筛选没有匹配事件」+「系统不会用演示数据填充空结果」 |
| 深链接 | 正常，新标签页可定位到指定事件 |
| 390px 横向溢出 | 无（`scrollWidth == clientWidth`，超宽元素 0），且提供 skip-to-content 入口 |
| 820px 横向溢出 | 无 |
| 证据姿态与模型研判分离 | 全程未松口 |
| 四种时间口径 | 事件日 / 来源发布 / 系统发现 / 最后更新，未合并 |
| Replay Lab 教学结构 | 完整，逐步证据节点 + 明确「本页不读取真实系统输出」 |

证据：`search_empty_state_1440x1000.png` · `home_390x844.png` · `replay_lab_1440x1000.png`

**特别指出：** 证据姿态（采到了什么）与事实确认（核验了什么）的分离，是这个产品最有价值的设计决策，一路点下来没有一处含糊。上述所有建议都以不破坏该分离为前提。

---

## 口径与边界

**数据量不同。** 本地 37 条 vs 生产 14,506 条。所有与规模相关的结论（API 延迟、分页性能）不能直接套用到生产。

**「产品质量指标」不要当真。** 页面显示 发现延迟 P95 `26.8 天` / 事实闭合率 `0.0%` / 可引用证据覆盖 `51.4%` / 待复核年龄 P95 `3.9 分钟`。这是刚建库、只跑过一轮采集的冷实例数字——ECB 若干 8 月 7 日旧条目今日首次入库，P95 自然被拉高。生产真实值本测试读不到。

**但顺带一个产品观察：** 这三个内部质量指标是**直接展示给公开访客**的。「事实闭合率 0.0%」出现在陌生访客屏幕上，传达的信息可能与预期的「诚实感」不一致。是否应保留在公开层，值得单独讨论。

**未测部分：**
- Reviewer / Operator / Admin 三个内部面（回环 + 令牌，无凭据）
- DeepSeek 捕获解读实际产出（本地队列未启，页面显示「尚未进入后台队列」）
- 生产 Cloudflare 前置对真实用户首屏的影响
- 生产规模（14,506 条）下的分页与 API 延迟

---

## 附带发现（与体验无关）

`.gitignore` 未包含 `.venv`（仅覆盖 `.env`、`.env.*`）。README 指引贡献者在仓库根目录创建 `.venv`，因此按 README 操作会产生约 900 MB 未跟踪文件。建议补一行 `.venv/`。

---

## 文件清单

| 文件 | 内容 |
|---|---|
| `README.md` | 本报告 |
| `measurements.json` | 全部实测数字，机器可读 |
| `home_1440x1000.png` | 首屏（问题 06） |
| `event_feed_repeated_titles_1440x1000.png` | 同名标题连续出现（问题 01） |
| `after_pagination_scroll_reset_1440x1000.png` | 翻页后滚动重置（问题 02） |
| `event_preview_duplicate_capture_text_1440x1000.png` | 捕获文本重复渲染（问题 04） |
| `card_utc_title_overlap_1440x1000.png` | UTC 与标题重叠（问题 05） |
| `search_empty_state_1440x1000.png` | 空态表现（通过项） |
| `home_390x844.png` | 移动端（通过项） |
| `replay_lab_1440x1000.png` | 证据演示（通过项） |

---

## 给审查者的复现方式

```bash
git checkout f775ae4306ed396c7ba9ee2b5761aae7eaa7b7ab
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock

# 建库（LedgerRepository 无 initialize()，schema 由 scripts.event_ledger.open_ledger 建立，
#       且该函数要求 Path 对象，传 str 会抛 AttributeError）
.venv/bin/python -c "
from pathlib import Path; import sys; sys.path.insert(0,'.')
from scripts.event_ledger import open_ledger
from app.storage import OperationsRepository
open_ledger(Path('data/finance_radar.sqlite3')).close()
OperationsRepository('data/finance_radar_operations.sqlite3').initialize()"

export SEC_USER_AGENT="your-name your@email"
.venv/bin/python -m app.workers.continuous --once --timeout 300
.venv/bin/python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 &
.venv/bin/python -m streamlit run app/web/Home.py --server.port 8501
```

注意：公开事件路由是 `/api/v1/events`，**不是** `/api/v1/public/events`（后者 404）。
