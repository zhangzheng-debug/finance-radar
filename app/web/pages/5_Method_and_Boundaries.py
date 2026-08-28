from __future__ import annotations

import streamlit as st

from app.web.common import (
    UI_ROLE,
    header,
    install_style,
    no_trading_banner,
    render_primary_navigation,
    section_header,
    situation_brief,
)


st.set_page_config(page_title="方法与边界 · Finance Radar", page_icon="◎", layout="wide")
install_style()
render_primary_navigation("method")

if UI_ROLE == "public":
    st.markdown(
        '<header class="public-reader-header">'
        '<div><span>FINANCE RADAR</span><h1>方法</h1></div>'
        '<p>从事件发现到市场反应的可追溯研究链路。</p>'
        '</header>',
        unsafe_allow_html=True,
    )
    section_header("核心流程", "")
    st.markdown(
        """
1. **自动发现**：持续读取监管机构、交易所、公司公告与公开新闻来源。
2. **风险排序**：仅在当前事件存在有效且获批的模型结果时，显示方向与下行强度。
3. **材料核对**：事件卡片直接连接来源文本与关键原文，事实判断以原文为准。
4. **价格审计**：按消息公开时间观察固定窗口内的市场变化，用于事后检验信号质量。
"""
    )

    section_header("页面标签", "")
    label_left, label_right = st.columns(2, gap="large")
    with label_left:
        st.markdown(
            """
**风险信号**

仅在有效且获批结果存在时显示方向与下行强度，用于排序研究注意力。

**材料层级**

原文支持 / 一手来源 / 来源可查 / 来源已保存。
"""
        )
    with label_right:
        st.markdown(
            """
**时间**

事件日、来源发布与数据更新时间分别记录，统一使用 UTC。

**AI 解读**

只解释来源文本；风险评级由独立模型处理。
"""
        )

    st.caption(
        "只读事件研究工具 · 来源材料用于事实核对 · 风险信号仅在有效结果存在时显示"
    )
    st.stop()

header("方法与边界", "看清证据从哪里来、何时发生，以及系统没有替你做什么")
no_trading_banner()
situation_brief(
    "先看证据，再形成判断",
    "Finance Radar 把公开事件、原始来源和判断边界放在同一条时间线上。"
    "页面帮助你更快定位值得阅读的材料，但最终判断仍应回到原始文件与上下文。",
    focus_label="使用原则",
    focus_value="可追溯 · 可复核 · 不代替人",
    focus_state="ok",
)

source_col, time_col, confidence_col = st.columns(3, gap="large")
with source_col:
    with st.container(border=True):
        st.subheader("来源")
        st.markdown(
            "优先展示监管机构、交易所、法院、公司公告等原始材料。"
            "聚合页面只用于发现线索；重要结论应回到可打开、可引用的原文。"
        )
with time_col:
    with st.container(border=True):
        st.subheader("时间")
        st.markdown(
            "事件日、来源发布时间、系统发现时间、最后更新时间和核验记录时间可能不同。"
            "页面会把它们分开标示；旧事件因修订重新出现时，不会被包装成刚发生的新事实。"
        )
with confidence_col:
    with st.container(border=True):
        st.subheader("置信度")
        st.markdown(
            "置信度表示当前证据是否足以支持一段陈述，不代表价格方向或收益概率。"
            "证据缺失、来源冲突或主体不清时，系统会保留不确定性。"
        )

section_header("状态与新鲜度", "数字之间如何对应")
with st.container(border=True):
    st.markdown(
        """
页面把每个事件放入一个、且只放入一个状态，因此五项之和始终等于全部事件：

- **待核验**：尚未完成粗审或正式核验。
- **已粗审**：完成快速筛查，但不等于正式核验。
- **证据不足**：现有材料不能支持确定结论；这不是已核验事实。
- **已核验**：当前记录通过了正式证据流程。
- **已排除**：线索不成立、主体不匹配或不应作为有效事件保留。

“采集状态 / 最近成功采集”只说明数据管道是否仍在工作；“当前粗审阶段”只统计仍停在快速筛查的事件；“正式处置状态”把已核验、证据不足和已排除分开汇总。它们是不同时间线，不能用采集正常来推断核验已经完成。
"""
    )

section_header("你在页面上看到什么", "公开阅读说明")
guide_left, guide_right = st.columns(2, gap="large")
with guide_left:
    st.markdown(
        """
- **事件摘要**：便于快速定位主题，不替代原始文件。
- **证据段落**：尽量保留直接支持事实的原文上下文。
- **核验引用证据 ID**：只在存在核验记录时显示本次实际引用的材料；它不等于全部关联证据。
- **来源等级**：帮助区分原始来源、可靠转述与待确认线索。
"""
    )
with guide_right:
    st.markdown(
        """
- **状态标签**：描述核验进度，不是买卖信号。
- **下一步提示**：告诉读者还缺什么证据，不会自动执行操作。
- **时间卡片**：明确区分事件、发布、发现、更新与核验，避免把“最后更新”误读为事件发生日。
- **演示案例**：用于说明判断过程，不代表当前市场全貌。
"""
    )

section_header("明确边界", "产品不会替你做什么")
boundary_left, boundary_right = st.columns(2, gap="large")
with boundary_left:
    st.warning(
        "本系统不提供投资建议，不预测收益，不代替专业法律、财务或投资判断。"
    )
    st.markdown(
        "它不会连接交易账户、创建订单、调整仓位，也不会因为模型分数而自动核验事件。"
    )
with boundary_right:
    st.info(
        "公开界面是只读的。人工复核、运行控制、模型管理和内部审计属于独立管理环境。"
    )
    st.markdown(
        "公开演示只展示理解方法所需的信息，不展示内部运行细节、原始日志或管理能力。"
    )

st.caption("阅读任何重要事件时，请打开原始来源，核对主体、日期、版本和精确上下文。")
