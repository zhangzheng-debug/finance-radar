# Streamlit 样式补丁 v2 · 集成说明（给 codex）

目标：在**不改任何 DOM 结构、不加任何依赖、不动任何页面逻辑**的前提下，
把现有五页终端升级到 Obsidian v2 观感。全部改动集中在
`app/web/common.py` 的 `install_style()` 一个函数内。

## 第一步 · CSS 整体替换（必做，纯字符串替换）

把 `install_style()` 中第一个 `st.markdown("""<style>…</style>""")` 的
样式内容整体替换为 [style_v2.css](style_v2.css) 的内容。

- 选择器与现有版本一一对应，只更新了 token 色值、字号阶、圆角与过渡；
  `ACCESSIBILITY_CSS`、`ACCESSIBILITY_JS`、所有组件 Python 代码不动。
- 新增了三个**可选**类 `.feed-tag.tier-p0/p1/p2`（权威层级 chip）与
  `.evidence-card.conflict`（冲突证据卡红边）。不加对应 markup 时无任何影响。
- 验证方式：现有 AppTest 结构回归应全绿（无 DOM 变化）；
  按 spec P1_UI_QA 要求刷新真实浏览器视觉/交互/无障碍矩阵。

对比度已复核：`--fr-muted #6d8196` 在 `--fr-panel #0b1220` 上 ≈ 4.6:1，
其余正文/状态色均 ≥ 4.5:1，满足现有 a11y 机审门（0 contrast failures）。

## 第二步 · 两处小 markup 增强（可选，低风险）

1. **权威 chip**：`components.py` 的 `event_feed_row()` 中，authority 那个
   `feed-tag` 增加层级类：
   ```python
   f'<span class="feed-tag tier-{escape(authority.lower())}">{escape(authority)}</span>'
   ```
2. **冲突证据卡**：渲染证据卡处，当 `evidence_status` 含 conflict/contradict 时
   给 `.evidence-card` 追加 `conflict` 类。

这两处只增加 class，不改变文本与结构，AppTest 按文本断言的用例不受影响。

## 第三步 · 图表配色（可选）

Home/Operations 页所有 Streamlit 原生图表（默认浅蓝）改为主题序列：
`["#38c7ec", "#2fd08f", "#f5b453", "#ff6b7a", "#a08bff"]`，背景透明。
若使用 `st.bar_chart` 可传 `color` 参数；Altair/Plotly 则设置 range。

## 明确不做的事

- 不引入 React / 前端构建链（spec v5.1 停止规则）。
- 不动 `saved_flow` / `event_keyboard` 组件的 JS 行为。
- 不改任何 API 合同或页面信息架构。
- 顶部导航 + 三栏 Workbench 的目标形态见 `../prototype/index.html`，
  属于 P0/P1 全绿后的第三批工作，不在本补丁范围。

## 提交后必须刷新的验收项（spec 对应）

- `public_browser_interactions`（基线 6/6 → 需按当前 release 重跑）
- `public_accessibility_machine_audit`（5 页 0 blockers 门）
- 1920×1080 / 1366×768 / 390×844 三档截图矩阵
