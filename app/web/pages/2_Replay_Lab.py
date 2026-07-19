from __future__ import annotations

import pandas as pd
import streamlit as st

from app.web.common import (
    api_request,
    header,
    install_style,
    no_trading_banner,
    render_api_error,
    restore_deep_link,
    section_header,
    status_strip,
)


st.set_page_config(page_title="Replay Lab · Finance Radar", page_icon="▷", layout="wide")
install_style()
restore_deep_link("Replay_Lab")
header("Replay Lab", "冻结事件时钟保证现场没有新 SEC 事件时仍能证明完整链路", "REPLAY")
no_trading_banner()

try:
    payload = api_request("/api/v1/replays")
except Exception as exc:
    render_api_error(exc)
    st.stop()

cases = payload["items"]
if not cases:
    st.warning("没有可用的冻结回放案例。")
    st.stop()

case = st.selectbox("冻结案例", cases, format_func=lambda item: f"{item['title']} · {item['case_id']}")
status_strip(
    [
        ("案例", case["case_id"], ""),
        ("步骤", case["observation_count"], ""),
        ("预期结果", case["expected_label"], "watch"),
        ("外部网络", "OFF", "ok"),
    ]
)
st.caption(case["description"])

run_col, next_col, all_col, reset_col, _ = st.columns([1.15, .9, .9, .9, 2.25], gap="small")
if run_col.button("运行冻结回放", type="primary", width="stretch"):
    try:
        st.session_state["last_replay"] = api_request(f"/api/v1/replays/{case['case_id']}/run", method="POST")
        st.session_state["replay_visible_steps"] = 1
    except Exception as exc:
        render_api_error(exc)

result = st.session_state.get("last_replay")
is_current_result = bool(result and result["case_id"] == case["case_id"])
total_steps = len(result["steps"]) if is_current_result else 0
visible_steps = min(max(int(st.session_state.get("replay_visible_steps", 1)), 1), total_steps) if total_steps else 0
if next_col.button(
    "下一步",
    width="stretch",
    disabled=not is_current_result or visible_steps >= total_steps,
):
    st.session_state["replay_visible_steps"] = min(visible_steps + 1, total_steps)
    visible_steps = st.session_state["replay_visible_steps"]
if all_col.button(
    "展开全部",
    width="stretch",
    disabled=not is_current_result or visible_steps >= total_steps,
):
    st.session_state["replay_visible_steps"] = total_steps
    visible_steps = total_steps
if reset_col.button("清空历史", width="stretch"):
    try:
        reset_result = api_request(f"/api/v1/replays/{case['case_id']}/reset", method="POST")
        st.session_state.pop("last_replay", None)
        st.session_state.pop("replay_visible_steps", None)
        result = None
        is_current_result = False
        st.success(f"已删除 {reset_result['deleted_runs']} 条历史回放")
    except Exception as exc:
        render_api_error(exc)

if is_current_result:
    visible_steps = min(max(int(st.session_state.get("replay_visible_steps", 1)), 1), total_steps)
    shown_steps = result["steps"][:visible_steps]
    current_decision = shown_steps[-1]["shadow_decision"]
    replay_complete = visible_steps == total_steps
    result_state = "ok" if replay_complete and result["expectation_met"] else "risk" if replay_complete else "watch"
    status_strip(
        [
            ("进度", f"{visible_steps}/{total_steps}", result_state),
            ("当前判断", current_decision["label"], result_state),
            ("预期匹配", "MET" if replay_complete and result["expectation_met"] else "MISMATCH" if replay_complete else "PENDING", result_state),
            ("时钟", "SIMULATED", "ok"),
            ("交易", "DISABLED", "ok"),
        ]
    )
    st.caption(f"run_id={result['run_id']} · 生产路由一致={result['same_downstream_router']}")
    if not replay_complete:
        st.info("演示已暂停在当前证据状态。点击“下一步”观察新证据如何改变判断，或“展开全部”查看完整审计链。")
    section_header("证据变化时间线", "FROZEN EVENT CLOCK")
    for index, step in enumerate(shown_steps, 1):
        observation = step["observation"]
        evidence = step["evidence_state"]
        decision = step["shadow_decision"]
        with st.container(border=True):
            clock_col, evidence_col, decision_col = st.columns([.36, 1.18, .9], gap="small")
            with clock_col:
                st.caption(f"步骤 {index:02d}")
                st.metric("模拟时钟", f"T+{step['simulated_at_seconds']}s")
                st.code(observation["authority_tier"], language=None)
            with evidence_col:
                st.caption(f"{observation['source']} · {observation['title']}")
                if observation.get("revision_kind"):
                    revision = observation["revision_kind"]
                    supersedes = observation.get("supersedes_step")
                    suffix = f" · supersedes STEP {supersedes:02d}" if supersedes else ""
                    st.code(f"REVISION {revision}{suffix}", language=None)
                st.write(observation["passage"])
                st.caption(
                    f"Evidence: {evidence['status']} · primary={evidence['has_primary']} · "
                    f"conflict={evidence['has_conflict']} · passages={evidence['passage_count']}"
                )
            with decision_col:
                state = "ALERT ELIGIBLE" if decision["alert_eligible"] else "NO ALERT"
                st.caption("影子路由 + 证据门")
                st.metric("当前判断", decision["label"])
                st.code(state, language=None)
                st.caption(f"{decision['decision_reason']} · {float(decision['confidence']):.0%}")

section_header("最近回放证据", "LAST 10 RUNS")
try:
    recent = api_request("/api/v1/replays")["recent_runs"]
except Exception:
    recent = payload["recent_runs"]
if recent:
    recent_rows = [
        {
            "回放ID": item["run_id"],
            "案例": item["case_id"],
            "状态": item["status"],
            "模型": item["model_version"],
            "开始时间": item["started_at"],
            "结束时间": item["finished_at"],
        }
        for item in recent
    ]
    st.dataframe(pd.DataFrame(recent_rows), width="stretch", hide_index=True)
else:
    st.info("还没有持久化回放记录。")
