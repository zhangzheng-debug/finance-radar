from __future__ import annotations

from html import escape
from typing import Any

import streamlit as st

from app.web.common import api_request, install_style, render_primary_navigation, restore_deep_link


_CASE_COPY = {
    "sec_bankruptcy_verified": (
        "官方申报确认破产风险",
        "新闻线索与 SEC 原始申报形成两步证据链，展示风险信号如何随材料层级升级。",
    ),
    "positive_earnings_non_target": (
        "正面业绩不进入下行风险队列",
        "正面经营公告完整保留在事件档案中，同时被风险路由识别为非下行事件。",
    ),
    "rumor_correction_abstain": (
        "公司原文推翻破产传言",
        "匿名传言与公司正式公告形成冲突，展示高权威原文如何撤回风险信号。",
    ),
    "sec_filing_corrected_abstain": (
        "更正申报撤回早先风险披露",
        "同一官方来源发布更正，展示新原文如何替代过时披露。",
    ),
}

_OBSERVATION_TITLES = {
    "Company reportedly prepares a court-supervised restructuring": "媒体称公司可能进行法院监督下的重组",
    "Issuer files Form 8-K under Item 1.03": "公司提交涉及破产事项的 Form 8-K",
    "Company raises guidance after record revenue": "公司在收入创新高后上调业绩指引",
    "Unverified post claims imminent bankruptcy": "匿名账号声称公司即将破产",
    "Issuer denies bankruptcy rumor": "公司正式否认破产传言",
    "Corrected Form 8-K withdraws Item 1.03 disclosure": "更正后的 Form 8-K 撤回早先披露",
}

_OBSERVATION_SUMMARIES = {
    "Company reportedly prepares a court-supervised restructuring": "此时可读材料只有聚合报道，事实层级停留在来源说法。",
    "Issuer files Form 8-K under Item 1.03": "公司在官方申报中披露已启动美国破产法第 11 章程序，风险动作获得关键原文支持。",
    "Company raises guidance after record revenue": "公司披露收入与利润增长，并上调全年指引；这是一则正面经营消息。",
    "Unverified post claims imminent bankruptcy": "风险说法来自匿名社交账号，材料中未附可定位的一手文件。",
    "Issuer denies bankruptcy rumor": "公司正式公告直接否认破产与违约说法。",
    "Corrected Form 8-K withdraws Item 1.03 disclosure": "更正申报说明早先选中破产事项是操作错误，原披露被撤回。",
}

_SOURCE_LABELS = {
    "aggregated_news": "聚合新闻",
    "sec_current_filings": "SEC 官方申报",
    "issuer_release": "公司公告",
    "social_channel": "社交渠道",
}

_AUTHORITY_LABELS = {
    "P0": "官方原文",
    "P1": "发布主体原文",
    "P2": "聚合报道",
    "P3": "匿名线索",
}

_DECISION_LABELS = {
    "RISK_REVIEW": "高风险信号",
    "NON_TARGET": "非下行事件",
    "ABSTAIN": "仅展示来源",
}

_RISK_TERMS = (
    "bankruptcy",
    "chapter 11",
    "restructuring",
    "default",
    "voluntary petitions",
    "court-supervised",
)


def _public_case_title(case: dict[str, Any]) -> str:
    copy = _CASE_COPY.get(str(case.get("case_id")))
    return copy[0] if copy else "其他案例"


def _public_case_description(case: dict[str, Any]) -> str:
    copy = _CASE_COPY.get(str(case.get("case_id")))
    return copy[1] if copy else "这个案例展示来源材料如何改变风险信号。"


def _decision_label(value: str) -> str:
    return _DECISION_LABELS.get(value, "来源记录")


def _presentation_steps(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a deterministic, read-only presentation from frozen observations."""

    observed: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    observations = sorted(case.get("observations", []), key=lambda item: item.get("at_seconds", 0))
    for observation in observations:
        observed.append(observation)
        has_primary = any(item.get("authority_tier") == "P0" for item in observed)
        has_conflict = any(bool(item.get("contradicts")) for item in observed)
        combined_text = " ".join(
            f"{item.get('title', '')} {item.get('passage', '')}" for item in observed
        ).lower()
        has_risk_claim = any(term in combined_text for term in _RISK_TERMS)

        if has_conflict:
            decision = "ABSTAIN"
            signal_label = "证据冲突"
            explanation = "更高权威的新材料反驳先前说法，风险信号撤回。"
            evidence_summary = "来源冲突"
        elif has_risk_claim and not has_primary:
            decision = "ABSTAIN"
            signal_label = "来源线索"
            explanation = "保留风险说法及其出处，不把来源说法改写成事件事实。"
            evidence_summary = "聚合或匿名来源"
        elif has_risk_claim and has_primary:
            decision = "RISK_REVIEW"
            signal_label = "高风险信号"
            explanation = "官方原文支持该风险动作，进入高优先级研究队列。"
            evidence_summary = "官方原文支持"
        else:
            decision = "NON_TARGET"
            signal_label = "非下行事件"
            explanation = "材料描述正面或中性事项，不进入下行风险队列。"
            evidence_summary = "非下行材料"

        steps.append(
            {
                "seconds": int(observation.get("at_seconds", 0)),
                "observation": observation,
                "decision": decision,
                "signal_label": signal_label,
                "explanation": explanation,
                "evidence_summary": evidence_summary,
            }
        )
    return steps


st.set_page_config(page_title="案例 · Finance Radar", page_icon="▷", layout="wide")
install_style()
restore_deep_link("Replay_Lab")
render_primary_navigation("replay")
st.markdown(
    '<header class="public-reader-header">'
    '<div><span>FINANCE RADAR</span><h1>案例</h1></div>'
    '<p>看材料变化如何改变风险信号。</p>'
    '</header>',
    unsafe_allow_html=True,
)

try:
    payload = api_request("/api/v1/replays")
except Exception:
    st.error("案例读取失败。")
    st.stop()

cases = payload.get("items", [])
if not cases:
    st.info("案例库为空。")
    st.stop()

case = st.selectbox("选择案例", cases, format_func=_public_case_title)
steps = _presentation_steps(case)
if not steps:
    st.info("案例未包含证据节点。")
    st.stop()

case_key = str(case.get("case_id") or _public_case_title(case))
visible_key = f"readonly_demo_visible::{case_key}"
visible_steps = min(max(int(st.session_state.get(visible_key, 1)), 1), len(steps))

st.markdown(
    '<section class="replay-case-intro">'
    f'<h2>{escape(_public_case_title(case))}</h2>'
    f'<p>{escape(_public_case_description(case))}</p>'
    '</section>',
    unsafe_allow_html=True,
)

restart_col, next_col, all_col = st.columns([1, 1, 1.3], gap="small")
if restart_col.button("第一步", width="stretch", disabled=visible_steps == 1):
    st.session_state[visible_key] = 1
    visible_steps = 1
if next_col.button("下一步", type="primary", width="stretch", disabled=visible_steps >= len(steps)):
    st.session_state[visible_key] = visible_steps + 1
    visible_steps += 1
if all_col.button("完整过程", width="stretch", disabled=visible_steps >= len(steps)):
    st.session_state[visible_key] = len(steps)
    visible_steps = len(steps)

st.caption(f"证据节点 {visible_steps}/{len(steps)}")

shown_steps = steps[:visible_steps]
current = shown_steps[-1]
for index, step in enumerate(shown_steps, 1):
    observation = step["observation"]
    original_title = str(observation.get("title") or "")
    source = _SOURCE_LABELS.get(str(observation.get("source")), "公开来源")
    authority = _AUTHORITY_LABELS.get(str(observation.get("authority_tier")), "来源记录")
    title = _OBSERVATION_TITLES.get(original_title, "来自公开来源的新材料")
    summary = _OBSERVATION_SUMMARIES.get(
        original_title,
        "这个节点增加了一条来源材料，并据此更新风险信号。",
    )
    with st.container(border=True):
        st.markdown(
            '<div class="replay-step">'
            f'<div class="replay-step-meta">{index} · {escape(source)} · {escape(authority)}</div>'
            f'<h3>{escape(title)}</h3>'
            f'<p>{escape(summary)}</p>'
            '<div class="replay-step-output">'
            f'<strong>{escape(str(step["signal_label"]))}</strong>'
            f'<span>{escape(str(step["explanation"]))}</span>'
            '</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        with st.expander("原文", expanded=False):
            if original_title:
                st.caption(original_title)
            st.write(observation.get("passage") or "该节点仅包含来源标题。")

if visible_steps == len(steps):
    st.success(f"案例结论：{_decision_label(current['decision'])}")

st.caption("冻结案例仅用于说明证据变化；页面不写入生产数据。")
