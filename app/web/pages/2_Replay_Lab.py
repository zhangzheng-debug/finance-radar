from __future__ import annotations

from typing import Any

import streamlit as st

from app.web.common import (
    api_request,
    header,
    install_style,
    no_trading_banner,
    render_primary_navigation,
    restore_deep_link,
    section_header,
    status_strip,
)


_CASE_COPY = {
    "sec_bankruptcy_verified": (
        "官方申报确认破产风险",
        "先出现聚合新闻线索，随后由 SEC 原始申报确认。观察系统为何要等到一手证据出现后，才把事件送入风险复核。",
    ),
    "positive_earnings_non_target": (
        "正面业绩不进入下行风险队列",
        "一则普通的正面业绩公告应被完整保留，但不会因为系统过度解读而变成下行风险告警。",
    ),
    "rumor_correction_abstain": (
        "公司澄清推翻破产传言",
        "低可信度传言随后被公司原文否认。证据发生冲突时，系统应停止告警并交给人工复核。",
    ),
    "sec_filing_corrected_abstain": (
        "更正申报撤回早先风险披露",
        "同一官方来源发布更正并撤回早先披露。演示结论为何必须跟随新证据变化，而不能保留已经过时的告警。",
    ),
}

_OBSERVATION_TITLES = {
    "Company reportedly prepares a court-supervised restructuring": "媒体称公司可能进行法院监督下的重组",
    "Issuer files Form 8-K under Item 1.03": "公司提交涉及破产事项的 Form 8-K",
    "Company raises guidance after record revenue": "公司在收入创新高后上调业绩指引",
    "Unverified post claims imminent bankruptcy": "未经核实的帖子声称公司即将破产",
    "Issuer denies bankruptcy rumor": "公司正式否认破产传言",
    "Corrected Form 8-K withdraws Item 1.03 disclosure": "更正后的 Form 8-K 撤回早先披露",
}

_OBSERVATION_SUMMARIES = {
    "Company reportedly prepares a court-supervised restructuring": "聚合新闻称公司可能进入法院监督下的重组，但当时还没有官方文件可以确认。",
    "Issuer files Form 8-K under Item 1.03": "公司在官方申报中披露已经启动美国破产法第 11 章程序，风险说法首次获得一手材料支持。",
    "Company raises guidance after record revenue": "公司披露收入与利润增长，并上调全年指引；这是一则正面经营消息，不属于下行风险。",
    "Unverified post claims imminent bankruptcy": "匿名社交账号声称公司即将破产并违约，但没有提供可核实的一手文件。",
    "Issuer denies bankruptcy rumor": "公司通过正式公告否认破产与违约说法，直接反驳了先前的匿名传言。",
    "Corrected Form 8-K withdraws Item 1.03 disclosure": "更正申报说明早先选中破产事项是操作错误，公司并未申请破产，原披露被撤回。",
}

_SOURCE_LABELS = {
    "aggregated_news": "聚合新闻",
    "sec_current_filings": "SEC 官方申报",
    "issuer_release": "公司公告",
    "social_channel": "社交渠道线索",
}

_AUTHORITY_LABELS = {
    "P0": "官方原始文件",
    "P1": "发布主体原文",
    "P2": "聚合报道",
    "P3": "未经核实的线索",
}

_DECISION_LABELS = {
    "RISK_REVIEW": "进入风险复核",
    "NON_TARGET": "不属于下行风险",
    "ABSTAIN": "暂不下结论",
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
    return copy[0] if copy else "其他冻结案例"


def _public_case_description(case: dict[str, Any]) -> str:
    copy = _CASE_COPY.get(str(case.get("case_id")))
    return copy[1] if copy else "这个冻结案例用于解释证据如何改变判断。"


def _decision_label(value: str) -> str:
    return _DECISION_LABELS.get(value, "等待更多证据")


def _presentation_steps(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Build a deterministic, read-only explanation from the frozen observations.

    This deliberately does not call the model or a write endpoint.  It mirrors only
    the public evidence-gate story needed by the presentation.
    """

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
            explanation = "新证据与早先说法冲突，系统停止告警并等待人工复核。"
            evidence_summary = "出现相互冲突的证据"
        elif has_risk_claim and not has_primary:
            decision = "ABSTAIN"
            explanation = "目前只有发现线索，还需要官方原始文件确认。"
            evidence_summary = "只有发现线索，尚无官方确认"
        elif has_risk_claim and has_primary:
            decision = "RISK_REVIEW"
            explanation = "官方原始文件支持风险描述，事件进入风险复核。"
            evidence_summary = "官方原始文件已经到位"
        else:
            decision = "NON_TARGET"
            explanation = "当前材料不属于下行风险目标，不进入告警队列。"
            evidence_summary = "材料完整，但不构成下行风险"

        steps.append(
            {
                "seconds": int(observation.get("at_seconds", 0)),
                "observation": observation,
                "decision": decision,
                "explanation": explanation,
                "evidence_summary": evidence_summary,
            }
        )
    return steps


st.set_page_config(page_title="证据演示 · Finance Radar", page_icon="▷", layout="wide")
install_style()
restore_deep_link("Replay_Lab")
render_primary_navigation("replay")
header("证据演示", "用冻结案例解释判断如何随证据变化；整个过程只在当前浏览会话中翻页", "只读演示")
no_trading_banner()

st.info("这是固定案例的只读讲解，不会触发实时采集、模型运行或数据库写入，也不会改变任何历史记录。")
status_strip(
    [
        ("发生什么", "冻结案例逐步展开", ""),
        ("证据如何变化", "按时间顺序查看", ""),
        ("最终结论", "走完案例后明确显示", "watch"),
        ("仍不做什么", "不写入、不交易", "ok"),
    ]
)

try:
    payload = api_request("/api/v1/replays")
except Exception:
    st.error("演示案例暂时无法加载，请稍后再试。实时事件与本页演示互不影响。")
    st.stop()

cases = payload.get("items", [])
if not cases:
    st.warning("目前没有可展示的冻结案例。")
    st.stop()

case = st.selectbox("选择一个演示案例", cases, format_func=_public_case_title)
steps = _presentation_steps(case)
if not steps:
    st.warning("这个案例还没有可展示的证据节点。")
    st.stop()

case_key = str(case.get("case_id") or _public_case_title(case))
visible_key = f"readonly_demo_visible::{case_key}"
visible_steps = min(max(int(st.session_state.get(visible_key, 1)), 1), len(steps))

section_header("发生什么", "选中的冻结案例")
st.markdown(f"**{_public_case_title(case)}**")
st.write(_public_case_description(case))
progress_slot = st.empty()

restart_col, next_col, all_col, _ = st.columns([1.0, 1.0, 1.25, 3.2], gap="small")
if restart_col.button(
    "重新开始",
    width="stretch",
    disabled=visible_steps == 1,
    help="只把当前页面的展示进度回到第一步，不会修改任何历史记录。",
):
    st.session_state[visible_key] = 1
    visible_steps = 1
if next_col.button("下一步", type="primary", width="stretch", disabled=visible_steps >= len(steps)):
    st.session_state[visible_key] = visible_steps + 1
    visible_steps += 1
if all_col.button("查看完整过程", width="stretch", disabled=visible_steps >= len(steps)):
    st.session_state[visible_key] = len(steps)
    visible_steps = len(steps)

# The placeholder stays above the controls but is filled only after a button
# has updated session state.  This keeps the visible progress strip and the
# final conclusion in the same render pass.
with progress_slot:
    status_strip(
        [
            ("证据节点", str(len(steps)), ""),
            (
                "当前进度",
                f"{visible_steps}/{len(steps)}",
                "watch" if visible_steps < len(steps) else "ok",
            ),
            ("外部网络", "未使用", "ok"),
            ("数据写入", "无", "ok"),
        ]
    )

shown_steps = steps[:visible_steps]
current = shown_steps[-1]
complete = visible_steps == len(steps)
status_strip(
    [
        ("当前判断", _decision_label(current["decision"]), "ok" if complete else "watch"),
        ("演示状态", "已完整展示" if complete else "等待下一步", "ok" if complete else "watch"),
        ("告警动作", "仅进入人工复核" if current["decision"] == "RISK_REVIEW" else "不发出告警", ""),
        ("交易功能", "始终关闭", "ok"),
    ]
)
if not complete:
    st.info("演示停在当前证据状态。点击“下一步”，观察新证据如何改变判断。")

section_header("证据如何改变判断", "冻结时间线 · 只读演示")
for index, step in enumerate(shown_steps, 1):
    observation = step["observation"]
    original_title = str(observation.get("title") or "")
    source = _SOURCE_LABELS.get(str(observation.get("source")), "公开来源")
    authority = _AUTHORITY_LABELS.get(str(observation.get("authority_tier")), "来源待核实")
    title = _OBSERVATION_TITLES.get(original_title, "来自公开来源的新证据")
    summary = _OBSERVATION_SUMMARIES.get(original_title, "这个节点出现了一条新证据，系统将依据来源与内容更新判断。")
    with st.container(border=True):
        clock_col, evidence_col, decision_col = st.columns([0.36, 1.2, 0.9], gap="small")
        with clock_col:
            st.caption(f"第 {index} 步")
            st.metric("相对时间", f"开始后 {step['seconds']} 秒")
            st.caption(authority)
        with evidence_col:
            st.caption(source)
            st.markdown(f"**{title}**")
            revision_kind = observation.get("revision_kind")
            if revision_kind == "CORRECTION":
                st.warning("这是更正信息：它会替代案例中较早的相关说法。")
            elif revision_kind == "INITIAL":
                st.caption("首次披露")
            st.write(summary)
            st.caption(f"证据状态：{step['evidence_summary']}")
            with st.expander("查看英文原始证据（次级）", expanded=False):
                if original_title:
                    st.caption(original_title)
                st.write(observation.get("passage") or "这个节点没有附带英文原始摘录。")
        with decision_col:
            st.caption("此刻系统应该怎样做")
            st.metric("当前判断", _decision_label(step["decision"]))
            st.write(step["explanation"])
            if step["decision"] == "RISK_REVIEW":
                st.caption("进入人工风险复核，不触发交易。")
            else:
                st.caption("保持克制，不发出风险告警。")

section_header("最终结论", "完成全部证据节点后显示")
if complete:
    st.success(f"演示完成：最终判断为“{_decision_label(current['decision'])}”。")
    with st.container(border=True):
        st.markdown(f"**最终结论：{_decision_label(current['decision'])}**")
        st.write(current["explanation"])
        if current["decision"] == "RISK_REVIEW":
            st.write("接下来只进入人工风险复核；这个演示本身不会发布告警，也不会触发交易。")
        else:
            st.write("案例到此结束；系统保持克制，不发布风险告警，也不触发交易。")
        if len(steps) > 1:
            st.caption("想重新讲解时，可点击上方“重新开始”回到第一步。")
else:
    st.info(f"还剩 {len(steps) - visible_steps} 个证据节点。走完后，这里会明确显示最终结论。")

section_header("仍不做什么", "演示边界始终不变")
with st.container(border=True):
    st.write("不把冻结案例冒充实时事件，不运行模型或采集器，不写入数据库或演示历史，也不触发告警、交易或其他外部动作。")
    st.caption("本页唯一会变化的是当前浏览会话中的显示进度；关闭页面后无需清理任何生产数据。")

st.caption("演示边界：页面只读取冻结案例并在浏览会话中控制显示进度，不调用模型、不保存演示过程、不修改生产数据。")
