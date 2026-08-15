from __future__ import annotations

import streamlit as st

from app.web.common import (
    DEEP_LINK_STATE_KEY,
    api_request,
    header,
    install_style,
    no_trading_banner,
    render_api_error,
    render_primary_navigation,
    require_ui_role,
    status_strip,
)


st.set_page_config(page_title="复核入口 · Finance Radar", page_icon="◇", layout="wide")
install_style()
require_ui_role("reviewer")

page_targets = {
    "Event_Intelligence": "pages/1_Event_Intelligence.py",
    "Replay_Lab": "pages/2_Replay_Lab.py",
    "Adjudication_Studio": "pages/4_Adjudication_Studio.py",
    "Method_and_Boundaries": "pages/5_Method_and_Boundaries.py",
}
requested_page = st.query_params.get("_page")
if requested_page in page_targets:
    st.session_state[DEEP_LINK_STATE_KEY] = {
        "page": requested_page,
        "params": {
            key: value
            for key, value in st.query_params.to_dict().items()
            if key != "_page"
        },
    }
    st.query_params.clear()
    st.switch_page(page_targets[requested_page])

render_primary_navigation("reviewer_home")
header(
    "人工复核入口",
    "只处理证据核对、人工判断与双人盲审；不展示部署、备份或模型运行控制",
    "REVIEWER · LOOPBACK ONLY",
)
no_trading_banner()

try:
    overview = api_request("/api/v1/overview")
    adjudication = api_request("/api/v1/adjudication/status")
except Exception as exc:
    render_api_error(exc)
    st.stop()

status_strip(
    [
        ("待复核", int(overview.get("review_queue") or 0), "watch"),
        ("事件总数", int((overview.get("counts") or {}).get("canonical_events") or 0), ""),
        ("盲审待办", int((adjudication.get("status_counts") or {}).get("OPEN") or 0), "watch"),
        ("交易能力", "无", "ok"),
    ]
)

review, adjudicate, method = st.columns(3, gap="large")
with review:
    st.subheader("逐条核验证据")
    st.caption("查看原始引文、冲突和下一步提示，并留下有身份、有理由的人工记录。")
    if st.button("进入事件复核", type="primary", width="stretch"):
        st.switch_page("pages/1_Event_Intelligence.py")
with adjudicate:
    st.subheader("双人盲审")
    st.caption("独立提交判断轴；同伴答案和模型结果保持隐藏。")
    if st.button("进入双人盲审", width="stretch"):
        st.switch_page("pages/4_Adjudication_Studio.py")
with method:
    st.subheader("核对方法边界")
    st.caption("确认来源权威、时间口径和系统明确不做的事情。")
    if st.button("查看方法与边界", width="stretch"):
        st.switch_page("pages/5_Method_and_Boundaries.py")

st.caption("Reviewer 令牌不能运行模型、切换演示模式、查看备份路径或执行服务维护。")
