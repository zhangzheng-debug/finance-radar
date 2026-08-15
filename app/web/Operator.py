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


st.set_page_config(page_title="运维入口 · Finance Radar", page_icon="▦", layout="wide")
install_style()
require_ui_role("operator")

page_targets = {
    "Operations_and_Model": "pages/3_Operations_and_Model.py",
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

render_primary_navigation("operator_home")
header(
    "运行维护入口",
    "查看服务、来源、备份和模型状态；不进行人工复核或盲审标注",
    "OPERATOR · LOOPBACK ONLY",
)
no_trading_banner()

try:
    health = api_request("/api/v1/health")
    model = api_request("/api/v1/model/status")
except Exception as exc:
    render_api_error(exc)
    st.stop()

latest_cycle = (health.get("operations") or {}).get("latest_worker_cycle") or {}
latest_backup = (health.get("operations") or {}).get("latest_backup") or {}
status_strip(
    [
        ("API", str(health.get("status") or "unknown").upper(), "ok" if health.get("status") == "ok" else "risk"),
        ("Worker", str(latest_cycle.get("status") or "NO DATA"), "ok" if latest_cycle.get("status") == "SUCCESS" else "watch"),
        ("备份", str(latest_backup.get("status") or "NO DATA"), "ok" if latest_backup.get("status") == "VERIFIED" else "watch"),
        ("模型", f"{model.get('status', 'unknown')} · SHADOW", "watch"),
        ("交易能力", "无", "ok"),
    ]
)

st.subheader("当前唯一工作面")
st.caption("运行诊断包含来源游标、备份状态、Worker 窗口和 Shadow 模型；维护动作仍需显式确认并留痕。")
if st.button("进入运行与模型", type="primary", width="stretch"):
    st.switch_page("pages/3_Operations_and_Model.py")

st.caption("Operator 令牌不能提交人工覆盖或双人盲审结论；管理员仍是单独的紧急全权入口。")
