from __future__ import annotations

from html import escape
from urllib.parse import urlsplit

import pandas as pd
import streamlit as st

from app.web.common import (
    SHOW_DEBUG,
    api_request,
    header,
    install_style,
    no_trading_banner,
    query_path,
    render_api_error,
    render_primary_navigation,
    require_admin_ui,
    restore_deep_link,
    section_header,
)
from app.web.components import (
    EVENT_FAMILY_LABELS,
    FLOW_PRESETS,
    adjacent_event_id,
    evidence_summary,
    event_button_label,
    facet_counts,
    facet_values,
    family_option_label,
    install_event_keyboard_navigation,
    next_action_guidance,
    render_next_action_prompt,
    render_saved_flow_manager,
    render_score_rail,
    render_market_context,
    score_dimensions,
    source_option_label,
)


st.set_page_config(page_title="事件工作台 · Finance Radar", page_icon="◇", layout="wide")
install_style()
require_admin_ui()
restore_deep_link("Event_Intelligence")
render_primary_navigation("events")

header(
    "事件工作台",
    "发现、判断与核验在同一工作面完成；仅受控写入证据代理审计、关联证据与人工复核记录，模型只做 shadow 分流",
    "内部 · 受控写入",
)
no_trading_banner()

flow_names = list(FLOW_PRESETS)
if st.query_params.get("reset") == "1":
    for state_key in (
        "event_flow",
        "event_family_filter",
        "event_source_filter",
        "event_global_query",
        "event_limit",
        "selected_event_id",
        "_event_filter_url_signature",
    ):
        st.session_state.pop(state_key, None)
    st.query_params.clear()
    st.query_params["flow"] = "待复核"
requested_flow = st.query_params.get("flow") or "待复核"
if requested_flow not in flow_names:
    requested_flow = "待复核"
requested_family = st.query_params.get("family") or ""
requested_source = st.query_params.get("source") or ""
requested_query = st.query_params.get("q") or ""
try:
    requested_limit = int(st.query_params.get("limit") or 25)
except (TypeError, ValueError):
    requested_limit = 25
limit_options = [15, 25, 50, 100]
if requested_limit not in limit_options:
    requested_limit = 25

incoming_filter_signature = (
    requested_flow,
    requested_family,
    requested_source,
    requested_query,
    str(requested_limit),
    str(st.query_params.get("event_id") or ""),
)
if st.session_state.get("_event_filter_url_signature") != incoming_filter_signature:
    st.session_state["event_flow"] = requested_flow
    st.session_state["event_family_filter"] = requested_family
    st.session_state["event_source_filter"] = requested_source
    st.session_state["event_global_query"] = requested_query
    st.session_state["event_limit"] = requested_limit
    if incoming_filter_signature[-1]:
        st.session_state["selected_event_id"] = incoming_filter_signature[-1]
    st.session_state["_event_filter_url_signature"] = incoming_filter_signature


def reset_event_filters() -> None:
    st.query_params.clear()
    st.query_params["reset"] = "1"


try:
    facets = api_request("/api/v1/events/facets")
except Exception:
    facets = {"families": [], "sources": []}
family_counts = facet_counts(facets, "families")
source_counts = facet_counts(facets, "sources")
family_options = facet_values(facets, "families", requested_family)
source_options = facet_values(facets, "sources", requested_source)

filter_cols = st.columns([1.0, 1.45, 1.35, 2.15, .65], gap="small")
flow = filter_cols[0].selectbox(
    "信息流",
    flow_names,
    key="event_flow",
    format_func=lambda value: {
        "已核验": "证据核验",
        "弱证据": "证据不足",
    }.get(str(value), str(value)),
)
family = filter_cols[1].selectbox(
    "事件族",
    family_options,
    key="event_family_filter",
    format_func=lambda value: family_option_label(str(value), family_counts),
    accept_new_options=True,
    filter_mode="fuzzy",
)
source = filter_cols[2].selectbox(
    "来源",
    source_options,
    key="event_source_filter",
    format_func=lambda value: source_option_label(str(value), source_counts),
    accept_new_options=True,
    filter_mode="fuzzy",
)
query = filter_cols[3].text_input(
    "全局检索",
    placeholder="公司 / Ticker / 类型 / 来源 / Event ID",
    key="event_global_query",
)
limit = filter_cols[4].selectbox(
    "数量",
    limit_options,
    key="event_limit",
)
st.query_params["flow"] = flow
if family:
    st.query_params["family"] = family
else:
    st.query_params.pop("family", None)
if source:
    st.query_params["source"] = source
else:
    st.query_params.pop("source", None)
if query:
    st.query_params["q"] = query
else:
    st.query_params.pop("q", None)
st.query_params["limit"] = str(limit)
render_saved_flow_manager(flow, family, query, limit, source=source)

try:
    events = api_request(
        query_path(
            "/api/v1/events",
            status=FLOW_PRESETS[flow]["status"],
            family=family,
            source=source,
            q=query,
            limit=limit,
        )
    )
except Exception as exc:
    render_api_error(exc)
    st.stop()

items = events["items"]
if not items:
    st.session_state["_event_filter_url_signature"] = (
        flow,
        family,
        source,
        query,
        str(limit),
        str(st.query_params.get("event_id") or ""),
    )
    st.info("当前视图没有符合条件的事件。切换视图或清除筛选后重试。")
    st.button("重置为待复核视图", type="primary", on_click=reset_event_filters)
    st.stop()

valid_ids = {item["event_id"] for item in items}
requested_id = st.query_params.get("event_id")
selected_id = st.session_state.get("selected_event_id") or requested_id
if selected_id not in valid_ids:
    selected_id = items[0]["event_id"]
st.session_state["selected_event_id"] = selected_id
st.query_params["event_id"] = selected_id
st.session_state["_event_filter_url_signature"] = (
    flow,
    family,
    source,
    query,
    str(limit),
    selected_id,
)
event_ids = [item["event_id"] for item in items]
install_event_keyboard_navigation(event_ids, selected_id)

list_col, evidence_col, context_col = st.columns([.95, 1.45, .8], gap="small")

with list_col:
    st.caption(f"{flow} · {events['total']:,} 条事件")
    selected_index = event_ids.index(selected_id)
    previous_id = adjacent_event_id(event_ids, selected_id, -1)
    next_id = adjacent_event_id(event_ids, selected_id, 1)
    nav_cols = st.columns(2, gap="small")
    if nav_cols[0].button(
        "K / ↑ 上一条",
        disabled=selected_index == 0,
        width="stretch",
        help="键盘 K 或向上方向键",
    ):
        st.session_state["selected_event_id"] = previous_id
        st.query_params["event_id"] = previous_id
        st.rerun()
    if nav_cols[1].button(
        "J / ↓ 下一条",
        disabled=selected_index == len(event_ids) - 1,
        width="stretch",
        help="键盘 J 或向下方向键",
    ):
        st.session_state["selected_event_id"] = next_id
        st.query_params["event_id"] = next_id
        st.rerun()
    st.caption("J/K 或 ↑/↓ 切换事件 · / 聚焦检索；输入框中不劫持按键")
    # Keep the queue independently scrollable. On narrow screens Streamlit stacks
    # columns, so an unbounded 25-row queue would push the evidence panel several
    # screens below the selected event.
    with st.container(height=480, border=False):
        for item in items:
            is_selected = item["event_id"] == selected_id
            if st.button(
                event_button_label(item),
                key=f"event-row-{item['event_id']}",
                type="primary" if is_selected else "secondary",
                width="stretch",
            ):
                st.session_state["selected_event_id"] = item["event_id"]
                st.query_params["event_id"] = item["event_id"]
                st.rerun()

try:
    detail = api_request(f"/api/v1/events/{selected_id}")
    evidence = api_request(f"/api/v1/events/{selected_id}/evidence")["items"]
except Exception as exc:
    with evidence_col:
        render_api_error(exc)
    st.stop()

timeline: list[dict[str, object]] = []
trace: dict[str, object] = {}
timeline_error: Exception | None = None
trace_error: Exception | None = None
try:
    timeline = api_request(f"/api/v1/events/{selected_id}/timeline")["items"]
except Exception as exc:
    timeline_error = exc
try:
    trace = api_request(f"/api/v1/events/{selected_id}/trace")
except Exception as exc:
    trace_error = exc

event = detail["event"]
version = detail.get("current_version") or {}
facts = version.get("facts") or {}
model = detail.get("model_shadow_output") or {}
company = event.get("company_name") or event.get("event_id")
event_type = str(event.get("event_type") or "event").replace("_", " ")
summary = facts.get("evidence_summary") or event.get("evidence_excerpt") or "尚无结构化事件摘要。"

with evidence_col:
    st.markdown(
        f'<div class="event-kicker">{escape(str(event.get("event_date") or "—"))} · '
        f'{escape(str(event.get("ticker_at_event") or "NO TICKER"))} · '
        f'{escape(event_type.upper())}</div>'
        f'<div class="event-headline">{escape(str(company))}</div>'
        f'<div class="event-summary">{escape(str(summary))}</div>',
        unsafe_allow_html=True,
    )

    section_header("证据矩阵", "AUTHORITY · STATUS · EXACT PASSAGE")
    if evidence:
        for index, item in enumerate(evidence, 1):
            passage = item.get("evidence_passage") or item.get("observation_summary") or "暂无精确证据段落"
            st.markdown(
                '<div class="evidence-card">'
                f'<div class="evidence-meta">E{index:02d} · {escape(str(item.get("authority_tier") or "P?"))} · '
                f'{escape(str(item.get("source_name") or "Unknown source"))} · '
                f'{escape(str(item.get("evidence_status") or "unknown")).upper()}</div>'
                f'<div class="evidence-passage">{escape(str(passage))}</div>'
                '</div>',
                unsafe_allow_html=True,
            )
            if item.get("evidence_url"):
                source_url = str(item["evidence_url"])
                parsed_source = urlsplit(source_url)
                if parsed_source.scheme in {"http", "https"} and parsed_source.netloc:
                    st.markdown(
                        '<div class="evidence-source-actions">'
                        '<span>精确证据段落已在本页显示</span>'
                        f'<a href="{escape(source_url, quote=True)}" target="_blank" '
                        'rel="noopener noreferrer">'
                        f'确需核对时打开外部原文 E{index:02d} ↗</a>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
    else:
        st.warning("没有证据边：系统必须保持待复核或弃权，不能自动升级。")

    if timeline_error or trace_error:
        st.info("附加审计追踪暂不可用；当前事件摘要、原始证据和复核提示仍来自核心账本记录。")

    decisions = trace.get("agent_decisions") or []

    # 日常审核者先看事件、证据和行动建议；运行时、原始对象和流程记录仅在
    # 需要复现或取证时展开，避免把开发状态误读为正式结论。
    if SHOW_DEBUG:
        with st.expander("审计追踪（开发/取证）", expanded=False):
            st.caption("仅供开发排障、证据取证和审计复盘；不构成日常审核结论。")
            if decisions:
                latest = decisions[0]
                output = latest.get("output") or {}
                with st.container(border=True):
                    st.caption(
                        f"EVIDENCE AGENT · {latest.get('status')} · provider={latest.get('model_provider')} · "
                        f"llm_used={output.get('llm_used')}"
                    )
                    st.write(output.get("cited_summary") or "暂无带引文摘要")
                    claims = output.get("claims") or []
                    edges = output.get("evidence_edges") or []
                    if claims:
                        st.dataframe(pd.DataFrame(claims), width="stretch", hide_index=True)
                    if edges:
                        edge_rows = [
                            {
                                "claim": edge.get("claim_id"),
                                "relation": edge.get("relation"),
                                "tier": edge.get("authority_tier"),
                                "exact_excerpt": edge.get("exact_excerpt"),
                                "sha256": edge.get("object_sha256"),
                                "source": edge.get("source_url"),
                            }
                            for edge in edges
                        ]
                        st.dataframe(
                            pd.DataFrame(edge_rows),
                            width="stretch",
                            hide_index=True,
                            column_config={"source": st.column_config.LinkColumn()},
                        )

            if timeline_error:
                st.info("版本时间线暂不可用；不会影响当前事件与证据的阅读。")
            else:
                with st.expander(f"版本时间线 · {len(timeline)} 条"):
                    for item in timeline:
                        st.caption(f"{item.get('at')} · {item.get('kind')}")
                        st.json(item.get("payload") or {}, expanded=False)

            if trace_error:
                st.info("流水线追踪暂不可用；不会把它当作事件或证据缺失。")
            else:
                with st.expander("流水线追踪与原始事件"):
                    trace_tabs = st.tabs(["流水线", "告警", "证据对象", "原始数据"])
                    with trace_tabs[0]:
                        st.dataframe(pd.DataFrame(trace.get("pipeline_jobs") or []), width="stretch", hide_index=True)
                    with trace_tabs[1]:
                        st.dataframe(pd.DataFrame(trace.get("alerts") or []), width="stretch", hide_index=True)
                    with trace_tabs[2]:
                        st.dataframe(pd.DataFrame(trace.get("evidence_objects") or []), width="stretch", hide_index=True)
                    with trace_tabs[3]:
                        st.json(detail, expanded=False)

            with st.expander("内部标识、模型与运行时"):
                st.caption("以下标识与运行数据仅用于复现、排障和审计，不是日常复核结论。")
                st.code(event["event_id"], language=None)
                family_key = str(event.get("event_family") or "")
                st.write(f"事件族：{EVENT_FAMILY_LABELS.get(family_key, family_key or '—')}")
                st.write(f"版本 {event.get('current_version')} · 人工等级 {event.get('manual_grade') or '—'}")
                st.write(f"模型 `{model.get('model_version')}`")
                st.caption(f"运行时={model.get('runtime')} · 延迟={float(model.get('latency_ms') or 0):.2f}ms")
                st.caption("判断维度（内部复盘）· 各维度相互独立")
                render_score_rail(score_dimensions(event, evidence, model, current_version=version))

            if detail.get("market_snapshots") or detail.get("market_metrics"):
                with st.expander("行情审计原始记录"):
                    st.caption("原始快照和事件后指标仅用于审计，不用于训练特征或交易触发。")
                    if detail.get("market_snapshots"):
                        st.dataframe(pd.DataFrame(detail["market_snapshots"]), width="stretch", hide_index=True)
                    if detail.get("market_metrics"):
                        st.dataframe(pd.DataFrame(detail["market_metrics"]), width="stretch", hide_index=True)

with context_col:
    workflow_status = str(event.get("status") or "candidate").lower()
    review_state_label = {
        "candidate": "待复核",
        "weak": "证据不足",
        "verified": "已核验",
        "rejected": "已排除",
    }.get(workflow_status, "待复核")
    review_state_copy = {
        "candidate": "尚未形成正式结论；请先核对原始证据与事件事实。",
        "weak": "现有材料不足以闭合事实，需要补充或缩小事件陈述。",
        "verified": "正式核验已完成；仅在来源修订或出现新增事实时重新打开。",
        "rejected": "当前已排除；仅在出现新增高权威证据时重新打开。",
    }.get(workflow_status, "请依据原始证据进行人工复核。")
    review_evidence = evidence_summary(evidence)
    with st.container(border=True):
        st.caption("当前复核状态")
        st.write(f"**{review_state_label}** · {review_state_copy}")
        state_cols = st.columns(2, gap="small")
        state_cols[0].caption(
            f"关联证据：{review_evidence['count']} 条 · 最高权威 {review_evidence['highest_authority']}"
        )
        state_cols[1].caption(
            "证据冲突：发现冲突，优先人工核对"
            if review_evidence["conflict"]
            else "证据冲突：当前未发现"
        )

    render_next_action_prompt(next_action_guidance(event, evidence, model, trace=trace))

    section_header("只读行情上下文", "POST-EVENT · NO TRADING")
    render_market_context(detail)
    st.caption("T+窗口从首个真实观察快照起算；错过采集即显示 MISSED，不用最新报价回填，也不证明因果或交易方向。")

    def run_evidence_agent(*, evidence_change_confirmed: bool) -> None:
        try:
            result = api_request(
                f"/api/v1/events/{selected_id}/agent/run",
                method="POST",
                json_body={
                    "audit_write_confirmed": True,
                    "evidence_change_confirmed": evidence_change_confirmed,
                },
            )
            st.success(f"{result['status']} · {result['trace_id']}")
            st.rerun()
        except Exception as exc:
            st.error(str(exc))

    if workflow_status in {"verified", "rejected"}:
        with st.expander("有新增或修订证据时重新取证（受控写入）", expanded=False):
            st.caption(
                "当前已有正式结论。重新运行会写入新的代理审计与关联证据记录，"
                "但不会自动改写正式结论或触发交易。"
            )
            with st.form(f"rerun-evidence-agent-{selected_id}"):
                evidence_change_confirmed = st.checkbox(
                    "我确认已有新增或修订证据，需要创建新的审计记录",
                    key=f"rerun-evidence-confirmed-{selected_id}",
                )
                if st.form_submit_button("确认重新运行证据代理（受控写入）", width="stretch"):
                    if not evidence_change_confirmed:
                        st.warning("请先确认确有新增或修订证据，避免对已完成结论重复写入。")
                    else:
                        run_evidence_agent(evidence_change_confirmed=True)
    else:
        with st.form(f"run-evidence-agent-{selected_id}"):
            st.caption("运行会写入可追溯的代理审计和关联证据记录，不会改写事件结论，也不触发交易。")
            audit_write_confirmed = st.checkbox(
                "我确认本次运行会新增审计与证据记录，且仅用于当前事件的证据判断。",
                key=f"run-evidence-confirmed-{selected_id}",
            )
            if st.form_submit_button("确认运行证据代理（受控写入）", type="primary", width="stretch"):
                if not audit_write_confirmed:
                    st.warning("请先确认本次受控写入的用途。")
                else:
                    run_evidence_agent(evidence_change_confirmed=False)

    if decisions:
        latest = decisions[0]
        with st.expander("人工复核记录"):
            st.caption("会写入不可变的人工复核记录；不触发交易，也不会自动改写事件的正式结论。")
            with st.form(f"human-override-{selected_id}"):
                actor = st.text_input(
                    "审核者标识（姓名、工号或组织 ID）",
                    placeholder="例如：li.ming / R-042",
                )
                review_status = st.selectbox(
                    "复核结果", ["REVIEWED_NO_CHANGE", "HUMAN_REVIEW", "INSUFFICIENT"]
                )
                reason = st.text_area(
                    "复核理由（请说明核对了哪条证据，以及为何得出本次结论）",
                    placeholder="例如：核对 SEC 8-K 第 1.01 项原文与事件摘要；未发现与当前结论相冲突的主证据。",
                    max_chars=1000,
                )
                reviewer_attestation = st.checkbox(
                    "我确认以上身份和理由由本人填写，将作为不可变审计记录保存。",
                    key=f"reviewer-attestation-{selected_id}",
                )
                if st.form_submit_button("记录人工复核（会写入）", width="stretch"):
                    if not reviewer_attestation:
                        st.warning("请先确认身份和事件特定理由，再写入人工复核记录。")
                    else:
                        try:
                            saved = api_request(
                                f"/api/v1/events/{selected_id}/human-override",
                                method="POST",
                                json_body={
                                    "actor": actor,
                                    "reason": reason,
                                    "review_status": review_status,
                                    "reviewer_attestation": True,
                                },
                            )
                            st.success(f"已记录 {saved['override_id']}")
                        except Exception as exc:
                            st.error(str(exc))

    overrides = trace.get("human_overrides") or []
    if overrides:
        with st.expander(f"人工覆盖历史 · {len(overrides)} 条"):
            st.dataframe(pd.DataFrame(overrides), width="stretch", hide_index=True)
