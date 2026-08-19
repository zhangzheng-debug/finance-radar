from __future__ import annotations

import hmac
import os
from html import escape

import streamlit as st

from app.web.common import (
    UI_ROLE,
    api_request,
    header,
    install_style,
    no_trading_banner,
    query_path,
    render_api_error,
    render_primary_navigation,
    require_ui_role,
    restore_deep_link,
    section_header,
    status_strip,
)


st.set_page_config(page_title="双人盲审 · Finance Radar", page_icon="◇", layout="wide")
install_style()
require_ui_role("reviewer", "admin")
restore_deep_link("Adjudication_Studio")
render_primary_navigation("adjudication")
header(
    "双人盲审",
    "V3 预冻结双人盲标：只提交独立判断轴，系统随后派生路由标签",
    "HUMAN ONLY",
)
no_trading_banner()

review_ui_enabled = os.getenv("FINANCE_RADAR_REVIEW_UI_ENABLED") == "1"
review_access_code = os.getenv("FINANCE_RADAR_REVIEW_ACCESS_CODE", "")
if not review_ui_enabled:
    st.info(
        "公网部署当前为只读观察模式。审核写入默认关闭；内部标注时需由运维显式启用独立访问门。"
    )
    st.stop()
if not review_access_code:
    st.error("审核写入已请求启用，但服务器未配置独立访问码；系统拒绝开放。")
    st.stop()
supplied_access_code = st.text_input("内部审核访问码", type="password")
if not hmac.compare_digest(supplied_access_code, review_access_code):
    st.info("输入内部审核访问码后才会加载原文任务和写入控件。")
    st.stop()

supplied_reviewer_credential = st.text_input(
    "个人审核凭据",
    type="password",
    key="adjudication_personal_credential",
    help="由运维分别发给每名 Reviewer/Arbiter；只驻留当前 Streamlit 会话。",
)
if len(supplied_reviewer_credential.strip()) < 24:
    st.info("输入个人审核凭据后才会读取你的独立队列；不要共享凭据。")
    st.stop()


def adjudication_api(path: str, **kwargs):
    return api_request(
        path,
        reviewer_credential=supplied_reviewer_credential,
        **kwargs,
    )


try:
    progress = adjudication_api("/api/v1/adjudication/status")
except Exception as exc:
    render_api_error(exc)
    st.stop()

status_strip(
    [
        ("样本", progress.get("samples", 0), ""),
        ("待审核", (progress.get("status_counts") or {}).get("OPEN", 0), "watch"),
        (
            "冲突",
            (progress.get("status_counts") or {}).get("CONFLICT", 0),
            "risk" if (progress.get("status_counts") or {}).get("CONFLICT", 0) else "ok",
        ),
        ("有效双审", progress.get("valid_annotations", 0), "ok"),
        ("冻结门禁", progress.get("status", "NOT READY"), "ok" if str(progress.get("status", "")).startswith("READY") else "watch"),
    ]
)
st.caption(
    "同一样本的两名审核者互不可见答案；页面不展示影子模型输出、既有标签或事件后行情。"
    "两份判断一致后自动派生标签，不一致则进入第三人裁决。"
)

workflow_col, gate_col = st.columns([1.35, 1], gap="large")
with workflow_col:
    section_header("双人盲审流程", "LOCAL OPERATOR SETUP")
    stages = [
        ("01", "UNLABELED", "账本原文与证据段落生成样本；不预置目标标签"),
        ("02", "REVIEW A / B", "两名审核者分别提交三个判断轴；同伴答案不可见"),
        ("03", "ARBITRATION", "只在轴判断不一致时，交给第三个独立身份"),
        ("04", "PRE-FREEZE", "系统派生标签并验证合同；split 仍保持 UNASSIGNED"),
    ]
    for number, title, copy in stages:
        with st.container(border=True):
            stage_number, stage_copy = st.columns([.16, 1], gap="small")
            stage_number.code(number, language=None)
            stage_copy.markdown(f"**{title}**")
            stage_copy.caption(copy)
with gate_col:
    section_header("冻结准备度", "PRE-FREEZE GATE")
    deficits = progress.get("label_deficits") or {}
    freeze_cols = st.columns(3, gap="small")
    for column, label in zip(freeze_cols, ["RISK_REVIEW", "NON_TARGET", "ABSTAIN"]):
        column.metric(label, f"−{int(deficits.get(label, 0))}", help="达到预注册最低数量前保持未冻结")
    with st.container(border=True):
        st.caption("完整性门禁")
        st.write("✓ 审核者不直接提交目标路由标签")
        st.write("✓ 模型输出与事件后行情结果隐藏")
        st.write("✓ 公网写入控件默认关闭")
        st.write(
            f"○ 独立来源家族 {int(progress.get('source_families') or 0)} / "
            f"{int(progress.get('minimum_source_families') or progress.get('minimum_source_groups') or 4)}"
        )
        st.code("production_changed=false · blind_v2_frozen=false", language=None)

identity_col, refresh_col = st.columns([2.5, .7], gap="small")
identity_col.caption("审核身份和角色由当前独立凭据在服务端绑定；页面不能自报或切换身份。")
refresh_col.write("")
if refresh_col.button("刷新", width="stretch"):
    st.rerun()

if UI_ROLE == "admin":
    with st.expander("将账本事件加入未标注队列"):
        event_id = st.text_input("事件 ID", placeholder="FR-LIVE-…")
        if st.button("创建来源遮蔽样本", disabled=not event_id.strip()):
            try:
                result = api_request(
                    f"/api/v1/adjudication/samples/from-event/{event_id.strip()}", method="POST"
                )
                st.success(
                    f"sample={result['sample_id']} · {'已创建' if result['created'] else '已存在'} · "
                    "目标路由标签保持未设置"
                )
                st.rerun()
            except Exception as exc:
                render_api_error(exc)

try:
    queue = adjudication_api(
        query_path(
            "/api/v1/adjudication/queue",
            limit=100,
        )
    )
except Exception as exc:
    render_api_error(exc)
    st.stop()

role = str(queue.get("role") or "REVIEWER")
reviewer_principal = str(queue.get("reviewer_principal") or "credential-bound")
st.caption(f"当前凭据身份：{reviewer_principal} · 角色：{role}")
items = queue.get("items") or []
if not items:
    if role == "ARBITER":
        st.success("当前没有需要第三人裁决的冲突样本。")
    else:
        st.success("当前没有分配给该身份的未完成样本；可以切换审核者或加入新事件。")
    st.stop()

sample = st.selectbox(
    "独立审核队列",
    items,
    format_func=lambda item: (
        f"{item['sample_id']} · {item['content'].get('headline', '')[:72]} · "
        f"{item['review_count']}/2"
    ),
)
status_strip(
    [
        ("样本", sample["sample_id"], ""),
        ("来源代号", sample["source_token"], ""),
        ("权威级别", sample["authority_context"], ""),
        ("同伴答案", "HIDDEN" if sample["peer_answers_hidden"] else "CONFLICT ONLY", "ok"),
        ("模型 / 行情", "HIDDEN", "ok"),
    ]
)

left, right = st.columns([1.45, 1], gap="large")
with left:
    content = sample["content"]
    st.markdown(f"### {escape(str(content.get('headline') or '未命名事件'))}")
    if content.get("summary"):
        st.write(content["summary"])
    confirmed = content.get("confirmed_facts") or []
    if confirmed:
        st.caption("正文抽取事实 · 不是目标路由标签")
        for fact in confirmed:
            st.markdown(f"- {escape(str(fact))}")
    section_header("精确证据段落", "SOURCE-MASKED INPUT")
    for index, passage in enumerate(content.get("passages") or [], 1):
        st.markdown(
            '<div class="evidence-card">'
            f'<div class="evidence-meta">PASSAGE {index:02d} · '
            f'{escape(str(passage.get("authority_class") or "CONTEXT"))} · '
            f'{escape(str(passage.get("document_type") or "document"))}</div>'
            f'<div class="evidence-passage">{escape(str(passage.get("passage") or ""))}</div>'
            "</div>",
            unsafe_allow_html=True,
        )
    st.caption(f"content_sha256={sample['text_sha256']}")

with right:
    if role == "ARBITER" and sample.get("conflict_options"):
        st.warning("两份独立判断不一致。以下仅展示冲突内容，不展示审核者身份。")
        for index, option in enumerate(sample["conflict_options"], 1):
            with st.container(border=True):
                st.code(
                    f"OPTION {index}\n{option['materiality']}\n{option['polarity']}\n{option['evidence_state']}",
                    language=None,
                )
                st.caption(option["rationale"])
    with st.form(f"adjudication-{sample['sample_id']}-{role}"):
        materiality = st.selectbox(
            "重大性",
            ["UNCLEAR", "MATERIAL_ADVERSE", "NOT_MATERIAL_ADVERSE"],
            help="是否构成需要人工风险复核的重大不利事实；不要判断涨跌。",
        )
        polarity = st.selectbox(
            "事件极性",
            ["UNCLEAR", "ADVERSE", "POSITIVE", "NEUTRAL", "MIXED"],
        )
        evidence_state = st.selectbox(
            "证据状态",
            [
                "INSUFFICIENT",
                "PRIMARY_SUPPORTED",
                "MULTI_SOURCE_SUPPORTED",
                "DISCOVERY_ONLY",
                "CONFLICTED",
            ],
        )
        rationale = st.text_area(
            "判断依据",
            placeholder="至少20字符；引用具体事实与证据状态，不写买卖建议。",
            height=150,
        )
        submitted = st.form_submit_button(
            "提交独立判断轴" if role == "REVIEWER" else "提交第三人裁决",
            type="primary",
            width="stretch",
        )
    if submitted:
        try:
            result = adjudication_api(
                f"/api/v1/adjudication/samples/{sample['sample_id']}/reviews",
                method="POST",
                json_body={
                    "materiality": materiality,
                    "polarity": polarity,
                    "evidence_state": evidence_state,
                    "rationale": rationale,
                },
            )
            message = f"审核={result['review_id']} · 流程={result['status']}"
            if result.get("derived_label"):
                message += f" · 派生路由={result['derived_label']} ({result['resolution']})"
            st.success(message)
            st.rerun()
        except Exception as exc:
            render_api_error(exc)

section_header("冻结准备度", "BLIND V3 STATUS")
st.json(
    {
        "status": progress.get("status"),
        "derived_label_counts": progress.get("label_counts"),
        "remaining_minimums": progress.get("label_deficits"),
        "source_groups": progress.get("source_groups"),
        "source_families": progress.get("source_families"),
        "split": progress.get("split"),
        "production_changed": progress.get("production_changed"),
    },
    expanded=False,
)
