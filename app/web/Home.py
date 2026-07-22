from __future__ import annotations

import streamlit as st

from app.web.common import (
    DEEP_LINK_STATE_KEY,
    api_request,
    header,
    install_style,
    no_trading_banner,
    pulse_grid,
    render_api_error,
    section_header,
    status_strip,
)
from app.web.components import (
    render_event_feed,
    render_command_palette,
    render_flow_shortcuts,
    source_health_state,
    terminal_search_state,
)


st.set_page_config(page_title="Finance Radar · Situation Room", page_icon="◎", layout="wide")
install_style()

page_targets = {
    "Event_Intelligence": "pages/1_Event_Intelligence.py",
    "Replay_Lab": "pages/2_Replay_Lab.py",
    "Operations_and_Model": "pages/3_Operations_and_Model.py",
    "Adjudication_Studio": "pages/4_Adjudication_Studio.py",
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

try:
    overview = api_request("/api/v1/overview")
except Exception as exc:
    header("Finance Radar", "多源证据链金融事件情报终端")
    render_api_error(exc)
    st.stop()

header("Situation Room", "事件流、证据健康、复核队列与运行态总览", overview["demo_mode"])
no_trading_banner()

with st.form("terminal-global-search", border=False):
    search_col, submit_col = st.columns([5.4, .8], gap="small", vertical_alignment="bottom")
    terminal_query = search_col.text_input(
        "全终端检索",
        placeholder="搜索公司、Ticker、事件类型或 Event ID",
        label_visibility="collapsed",
    )
    search_submitted = submit_col.form_submit_button("检索 /", width="stretch")
if search_submitted:
    search_state = terminal_search_state(terminal_query)
    if search_state["q"]:
        st.session_state[DEEP_LINK_STATE_KEY] = {
            "page": "Event_Intelligence",
            "params": search_state,
        }
        st.switch_page(page_targets["Event_Intelligence"])
    else:
        st.warning("请输入公司、Ticker、事件类型或 Event ID。")

counts = overview["counts"]
event_status = overview["event_status"]
timing = overview.get("timing", {})
event_age = timing.get("latest_event_age_seconds")
cycle_duration = timing.get("worker_cycle_duration_seconds")
status_strip(
    [
        ("事件", f"{counts['canonical_events']:,}", ""),
        ("证据核验", f"{event_status.get('verified', 0):,}", "ok"),
        ("待复核", f"{overview['review_queue']:,}", "watch" if overview["review_queue"] else "ok"),
        ("证据边", f"{counts['event_evidence']:,}", ""),
        ("最新事件", f"{event_age / 60:.1f} min" if event_age is not None else "—", ""),
        ("Worker", f"{cycle_duration:.2f} s" if cycle_duration is not None else "—", "ok"),
    ]
)
render_flow_shortcuts(event_status)
try:
    facets = api_request("/api/v1/events/facets")
except Exception:
    facets = {"families": [], "sources": []}
render_command_palette(facets)

left, right = st.columns([1.65, .85], gap="medium")
with left:
    recent = overview["recent_events"]
    section_header("实时事件流", f"LATEST {len(recent)} · UTC · 点击进入证据工作台")
    if recent:
        render_event_feed(recent)
    else:
        st.info("暂无事件。")

with right:
    section_header("系统复核队列", "EVIDENCE REVIEW")
    st.markdown(
        '<div class="queue-card">'
        '<div class="queue-card-label">等待证据或规则复核</div>'
        f'<div class="queue-card-value">{overview["review_queue"]:,}</div>'
        '<div class="queue-card-copy">模型只做 shadow 分流；无充分证据的事件不会自动升级。</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    st.page_link("pages/1_Event_Intelligence.py", label="打开事件工作台")
    pulse_grid(
        [
            ("证据核验", f"{event_status.get('verified', 0):,}", "ok"),
            ("候选", f"{event_status.get('candidate', 0):,}", "watch"),
            ("证据不足", f"{event_status.get('weak', 0):,}", "watch"),
            ("已拒绝", f"{event_status.get('rejected', 0):,}", ""),
        ]
    )

    section_header("来源脉搏", "COLLECTOR HEALTH")
    states = [source_health_state(source) for source in overview["source_health"]]
    source_ok = sum(state[1] == "OK" for state in states)
    source_watch = sum(state[1] == "WATCH" for state in states)
    source_error = sum(state[1] == "ERROR" for state in states)
    p0_sources = sum(source.get("authority_tier") == "P0" for source in overview["source_health"])
    pulse_grid(
        [
            ("健康", source_ok, "ok"),
            ("观察", source_watch, "watch" if source_watch else ""),
            ("异常", source_error, "risk" if source_error else "ok"),
            ("P0 来源", p0_sources, ""),
        ]
    )

    audit = overview["audit"]
    if sum(audit.values()) == 0:
        st.markdown(
            '<div class="boundary-ok" role="status">硬边界审计 0 违规 · NO TRADING / NO AUTO VERIFY / NO LEAKAGE</div>',
            unsafe_allow_html=True,
        )
    else:
        st.error(f"审计异常：{audit}")
    st.caption(f"Ledger Schema {overview['schema_version']} · SQLite quick_check={overview['quick_check']}")

st.caption("J/K 在事件工作台切换事件 · / 聚焦检索 · 所有行情只读 · 所有模型输出均为 shadow")
