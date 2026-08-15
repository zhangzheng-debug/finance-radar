from __future__ import annotations

import streamlit as st

from app.web.common import (
    DEEP_LINK_STATE_KEY,
    header,
    install_style,
    no_trading_banner,
    render_primary_navigation,
    require_admin_ui,
)


st.set_page_config(page_title="管理入口 · Finance Radar", page_icon="◇", layout="wide")
install_style()
require_admin_ui()

page_targets = {
    "Event_Intelligence": "pages/1_Event_Intelligence.py",
    "Replay_Lab": "pages/2_Replay_Lab.py",
    "Operations_and_Model": "pages/3_Operations_and_Model.py",
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

render_primary_navigation("admin_home")

header(
    "内部管理入口",
    "人工复核、运行诊断、模型治理与双人盲审仅在本机隧道内开放",
    "ADMIN · LOOPBACK ONLY",
)
no_trading_banner()

st.info(
    "此界面只监听服务器回环地址，不经过公网 Nginx。使用完毕后请停止 "
    "finance-radar-admin 服务并关闭 SSH 隧道。"
)

review, operations, adjudication = st.columns(3, gap="large")
with review:
    st.subheader("人工复核")
    st.caption("逐条核验事件、证据和人工判断。")
    if st.button("进入人工复核", key="admin-review", width="stretch"):
        st.switch_page("pages/1_Event_Intelligence.py")
with operations:
    st.subheader("运行与模型")
    st.caption("查看服务、Worker、来源、备份与模型治理状态。")
    if st.button("进入运行诊断", key="admin-operations", width="stretch"):
        st.switch_page("pages/3_Operations_and_Model.py")
with adjudication:
    st.subheader("双人盲审")
    st.caption("进行独立标注、分歧处理与晋级审查。")
    if st.button("进入双人盲审", key="admin-adjudication", width="stretch"):
        st.switch_page("pages/4_Adjudication_Studio.py")

st.divider()
st.caption("管理入口不提供交易、下单、仓位或资金操作能力。")
