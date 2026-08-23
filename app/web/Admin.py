from __future__ import annotations

import streamlit as st

from app.web.admin_overview import (
    fetch_admin_read_snapshot,
    summarize_admin_read_snapshot,
)
from app.web.common import (
    DEEP_LINK_STATE_KEY,
    api_request,
    format_elapsed,
    header,
    install_style,
    no_trading_banner,
    render_primary_navigation,
    require_admin_ui,
    status_strip,
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


def display_value(value: object, suffix: str = "") -> str:
    if value is None or value == "":
        return "不可用"
    return f"{value}{suffix}"


snapshot = fetch_admin_read_snapshot(api_request)
owner = summarize_admin_read_snapshot(snapshot)
release = owner["release"]
worker = owner["worker"]
sources = owner["sources"]
interpretation = owner["interpretation"]
evidence = owner["evidence"]
backup = owner["backup"]
model = owner["model"]
audit = owner["audit"]

worker_age = worker["last_success_age_seconds"]
worker_state = (
    "ok"
    if worker.get("status") == "SUCCESS" and worker_age is not None and worker_age <= 900
    else "risk"
    if worker_age is not None and worker_age > 3600
    else "watch"
)
source_state = "ok" if sources.get("failures") == 0 else "risk" if sources.get("failures") else "watch"
backup_state = (
    "ok"
    if backup.get("status") == "FRESH" and backup.get("fresh") is True
    else "risk"
    if backup.get("status") in {"STALE", "MISSING", "MISSING_ARTIFACT", "UNAVAILABLE"}
    else "watch"
)
audit_state = (
    "ok"
    if audit.get("status") == "ok" and audit.get("boundary_violations") == 0
    else "risk"
    if audit.get("status") == "degraded" or (audit.get("boundary_violations") or 0) > 0
    else "watch"
)

st.subheader("老板总览")
st.caption(
    "只读汇总现有 API 的发布、采集、来源、证据、备份、模型与审计状态；"
    "缺失指标显示为“不可用”，不会用 0 或旧值代替。"
)
status_strip(
    [
        ("后端版本", display_value(release.get("service_version")), "ok" if release.get("service_version") else "watch"),
        ("Worker", display_value(worker.get("status")), worker_state),
        ("来源异常", display_value(sources.get("failures")), source_state),
        ("备份", display_value(backup.get("status")), backup_state),
        ("审计", display_value(audit.get("status")), audit_state),
    ]
)

top = st.columns(4, gap="medium")
top[0].metric("最近成功采集", format_elapsed(worker_age) if worker_age is not None else "不可用")
top[0].caption(display_value(worker.get("last_success_at")))
top[1].metric(
    "正式可引用",
    (
        f"{evidence['citation_ready']:,} / {evidence['total_events']:,}"
        if evidence.get("citation_ready") is not None and evidence.get("total_events") is not None
        else "不可用"
    ),
)
top[1].caption(
    "证据归档覆盖 "
    + (
        f"{evidence['archive_coverage_pct']:.1f}%"
        if evidence.get("archive_coverage_pct") is not None
        else "不可用"
    )
)
top[2].metric("来源异常", display_value(sources.get("failures")))
top[2].caption(
    f"共 {sources['total']} 个来源"
    if sources.get("total") is not None
    else "来源清单不可用"
)
blind_gate = model.get("external_blind_gate_pass")
top[3].metric(
    "盲测门禁",
    "通过" if blind_gate is True else "未通过" if blind_gate is False else "不可用",
)
top[3].caption(
    f"运行 {display_value(model.get('status'))} · "
    f"盲测样本 {display_value(model.get('external_blind_rows'))} · "
    f"{display_value(model.get('promotion_decision'))}"
)

left, right = st.columns(2, gap="large")
with left:
    with st.container(border=True):
        st.markdown("#### 数据与 API 解读")
        st.write(f"已记录解读运行：**{display_value(interpretation.get('recorded_runs'))}**")
        st.write(f"当前待处理 backlog：**{display_value(interpretation.get('pending_backlog'))}**")
        st.caption(interpretation["limitation"])
        if sources.get("failure_names"):
            st.warning("异常来源：" + "、".join(sources["failure_names"]))
        elif sources.get("failures") == 0:
            st.success("现有来源健康接口未报告异常。")
        else:
            st.info("来源健康明细不可用。")

    with st.container(border=True):
        st.markdown("#### 备份与恢复")
        st.write(f"当前备份快照：**{display_value(backup.get('status'))}**")
        st.write(
            "快照年龄：**"
            + (
                format_elapsed(backup.get("age_seconds"))
                if backup.get("age_seconds") is not None
                else "不可用"
            )
            + "**"
        )
        st.write(f"隔离恢复检查：**{display_value(backup.get('quick_check'))}**")
        st.caption(
            "实时路径可见性："
            + display_value(backup.get("artifact_visibility"))
            + " · 当前快照验证时间："
            + display_value(backup.get("verified_at"))
        )
        st.caption(
            "历史最近成功记录（不代表当前安全）："
            + display_value(backup.get("last_verified_record_status"))
            + " · "
            + display_value(backup.get("last_verified_record_at"))
        )

with right:
    with st.container(border=True):
        st.markdown("#### 模型运行 / 盲测状态")
        st.write(f"Shadow 模型状态：**{display_value(model.get('status'))}**")
        st.write(f"最近模型运行记录：**{display_value(model.get('recent_runs'))}**")
        st.write(
            "外部盲测：**"
            + ("通过" if blind_gate is True else "未通过" if blind_gate is False else "不可用")
            + "**"
        )
        st.caption(
            f"样本 {display_value(model.get('external_blind_rows'))} · "
            f"晋级结论 {display_value(model.get('promotion_decision'))}。"
            "不把模型输出称为事实、覆盖率或交易信号。"
        )

    with st.container(border=True):
        st.markdown("#### 最近审计")
        st.write(f"持久化审计对账：**{display_value(audit.get('status'))}**")
        st.write(
            "待对账 / 恢复冲突："
            f"**{display_value(audit.get('pending_reconciliation'))} / "
            f"{display_value(audit.get('recovery_conflicts'))}**"
        )
        st.write(f"硬边界违规：**{display_value(audit.get('boundary_violations'))}**")
        st.caption(audit["limitation"] + "；最近时间：" + display_value(audit.get("latest_at")))

if owner["unavailable"]:
    st.warning(
        "部分只读数据暂不可用：" + "、".join(owner["unavailable"])
        + "。其余卡片仍是现场读取结果。"
    )
st.caption(
    "当前 API 只暴露语义版本，未暴露不可变 release ID；老板总览不会根据版本号猜测发布提交。"
)

st.divider()
st.subheader("进入专业工作面")
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
