from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from math import ceil
from urllib.parse import quote, urlencode, urlsplit

import streamlit as st

from app.web.common import (
    DEEP_LINK_STATE_KEY,
    UI_ROLE,
    api_request,
    cached_api_get,
    format_elapsed,
    header,
    install_style,
    no_trading_banner,
    pulse_grid,
    query_path,
    render_primary_navigation,
    render_api_error,
    section_header,
    situation_brief,
    status_strip,
)
from app.web.components import (
    EVENT_FAMILY_LABELS,
    FLOW_PRESETS,
    PUBLIC_AUTHORITY_LABELS,
    PUBLIC_SOURCE_LABELS,
    facet_counts,
    facet_values,
    event_anchor_id,
    focus_event_preview,
    next_action_guidance,
    public_event_copy,
    public_event_evidence_posture,
    public_event_quality,
    public_event_risk_assessment,
    public_event_state,
    render_evidence_route,
    render_event_feed,
    render_command_palette,
    render_flow_shortcuts,
    render_next_action_prompt,
    render_saved_public_flow_manager,
    source_health_state,
    terminal_search_state,
)


PUBLIC_PERIODS = {
    "全部时间": None,
    "最近 24 小时": 1,
    "最近 7 天": 7,
    "最近 30 天": 30,
    "最近 90 天": 90,
}
PUBLIC_SORTS = {
    "最近发现": "latest",
    "事件日期": "event_date",
    "主体名称": "subject",
}
PUBLIC_EVENT_SEEN_STATE_KEY = "public_event_seen_v1"


def bounded_int(value: object, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def query_choice(value: object, choices: set[str] | frozenset[str], default: str = "") -> str:
    normalized = str(value or "")
    return normalized if normalized in choices else default


def public_family_label(value: str, counts: dict[str, int]) -> str:
    if not value:
        return "全部类别"
    label = EVENT_FAMILY_LABELS.get(value, "其他公司事件")
    return f"{label} · {counts.get(value, 0):,}"


def public_source_label(value: str, counts: dict[str, int]) -> str:
    if not value:
        return "全部来源"
    label = PUBLIC_SOURCE_LABELS.get(value, "其他公开来源")
    return f"{label} · {counts.get(value, 0):,}"


def render_public_reading_prompt(event: dict[str, object], evidence: list[dict[str, object]]) -> None:
    """Give public readers a useful next step without exposing review tooling."""
    state = public_event_state(event)
    posture = public_event_evidence_posture(event)
    quality = public_event_quality(event, evidence)
    if state == "excluded":
        title = "这是一条异常处置记录"
        reason = "系统保留它是为了说明曾捕获过什么；这项处置不等于一条有效事件事实。"
        steps = ("查看最初捕获内容", "核对处置原因", "只有出现新的高权威材料时再重新判断")
        tone = "ok"
    elif posture["key"] == "PRIMARY_SUPPORTED":
        title = "先读支持当前事实的官方原文"
        reason = f"当前结构化事实已关联 {len(evidence)} 条可读证据；摘要仍不能替代完整上下文。"
        steps = ("打开官方原始来源", "核对主体、日期和文件版本", "区分已发生事实与前瞻性表述")
        tone = "ok"
    elif posture["key"] == "PRIMARY_SOURCE_AVAILABLE":
        title = "一手材料已经找到，事实槽仍需补齐"
        missing = posture["gap_labels"] or quality["gaps"]
        reason = "当前缺口：" + "、".join(missing or ["结构化事实尚未闭合"]) + "。"
        steps = (
            "先确认是谁、做了什么以及处于哪个阶段",
            "从现有一手材料中定位支持该事实的精确段落",
            "事实槽补齐前，不把分类标签当作事件结论",
        )
        tone = "watch"
    elif posture["key"] == "SOURCE_CAPTURED":
        title = "先读来源捕获，再寻找一手原文"
        reason = "当前只有系统捕获到的来源内容，它可以解释线索来自哪里，但不能单独确认事件事实。"
        steps = ("阅读捕获原文与自动解读", "寻找监管、交易所或公司原始文件", "不要从风险路由推断事件真假")
        tone = "watch"
    else:
        title = "当前还没有可供核对的来源"
        reason = "页面只保留规范化事件记录；在来源补齐前，不应把类别、主体或模型标签写成确定事实。"
        steps = ("确认来源采集是否遗漏", "优先寻找官方原始文件", "来源补齐前保留不确定性")
        tone = "risk"
    step_markup = "".join(f"<li>{escape(step)}</li>" for step in steps)
    st.markdown(
        f'<section class="next-action next-action-{tone}" aria-labelledby="public-reading-title">'
        '<div class="next-action-topline"><span>阅读提示</span><strong>只读</strong></div>'
        f'<h2 id="public-reading-title">{escape(title)}</h2>'
        f'<p>{escape(reason)}</p><ol>{step_markup}</ol>'
        '<div class="next-action-boundary"><span>不构成投资建议 · 不触发任何外部操作</span></div>'
        '</section>',
        unsafe_allow_html=True,
    )


def public_source_url(evidence: list[dict[str, object]]) -> str | None:
    """Return an explicitly safe public evidence link, if one is available."""
    if not evidence:
        return None
    value = str(evidence[0].get("evidence_url") or "").strip()
    if len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def public_capture_url(sources: list[dict[str, object]]) -> str | None:
    """Return a validated discovery-source link without treating it as evidence."""
    if not sources:
        return None
    value = str(
        sources[0].get("source_url") or sources[0].get("canonical_url") or ""
    ).strip()
    if len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else None


def render_capture_explanation_payload(payload: dict[str, object]) -> None:
    """Render the compact zero-evidence boundary without the legacy AI template."""

    if not payload.get("display"):
        return
    source = payload.get("source")
    source = source if isinstance(source, dict) else {}
    source_title = " ".join(
        str(source.get("source_title") or "已捕获一条 API 来源文本").split()
    )
    source_excerpt = " ".join(
        str(source.get("source_excerpt") or "来源没有提供更多摘要。").split()
    )
    if len(source_excerpt) > 900:
        source_excerpt = source_excerpt[:897].rstrip() + "…"
    st.markdown(
        '<section class="capture-ai-boundary" aria-label="无证据事件的自动解释">'
        '<div class="capture-ai-boundary-head">'
        '<span>AI 自动解释（仅在无证据时启用）</span>'
        '<strong>AI 阅读辅助 · 非证据</strong>'
        '</div>'
        '<p>因为该事件目前完全没有关联证据，系统才启动外部 AI，对已捕获的 API '
        '标题、摘要或正文片段进行自动解释。该内容只帮助理解来源文本，不证明事件真实发生，'
        '不改变事件状态、重大性、极性、风险分流或价格审计，也不会触发交易。</p>'
        '<div class="capture-ai-source">'
        '<small>系统捕获文本 · 不是 P0/P1 原始证据</small>'
        f'<h3>{escape(source_title)}</h3>'
        f'<p>{escape(source_excerpt)}</p>'
        '</div>'
        '</section>',
        unsafe_allow_html=True,
    )
    source_url = public_capture_url([source]) if source else None
    if source_url:
        st.link_button(
            "查看这条发现来源（非核验证据）",
            source_url,
            width="stretch",
        )

    state = str(payload.get("state") or "PENDING")
    interpretation = payload.get("item")
    interpretation = interpretation if isinstance(interpretation, dict) else None
    if state == "READY" and interpretation:
        st.markdown("#### AI 对捕获文本的解释")
        st.markdown(str(interpretation.get("one_line_zh") or "自动解释已完成。"))
        claims = interpretation.get("what_source_says") or []
        if claims:
            st.markdown("**捕获文本明确表达**")
            for claim in claims[:4]:
                if not isinstance(claim, dict):
                    continue
                st.markdown(
                    f"- {escape(str(claim.get('text_zh') or ''))}  "
                    f"\n  原文：{escape(str(claim.get('quote') or ''))}"
                )
        missing = interpretation.get("missing_to_change_state_zh") or []
        if missing:
            st.markdown("**仍缺少什么**")
            for item in missing[:3]:
                st.markdown(f"- {escape(str(item))}")
        st.caption("完整结果已通过引文、数字、版本和提示词合同校验后一次性展示。")
    elif state == "FAILED_RETRYING":
        st.warning("AI 自动解释暂不可用；原始捕获仍保留，系统不会用猜测内容替代。")
    else:
        st.info("事件主体和捕获文本已加载；AI 自动解释正在后台生成，不影响其他内容阅读。")


@st.fragment(run_every="2s")
def render_capture_explanation_fragment(
    event_path_id: str,
    event_id: str,
    initial_payload: dict[str, object],
) -> None:
    """Poll only the cache-only explanation panel for at most thirty seconds."""

    final_key = f"capture_explanation_final:{event_id}"
    started_key = f"capture_explanation_started:{event_id}"
    payload = st.session_state.get(final_key)
    if not isinstance(payload, dict):
        started = st.session_state.get(started_key)
        if not isinstance(started, datetime):
            started = datetime.now(timezone.utc)
            st.session_state[started_key] = started
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if elapsed > 30:
            payload = {
                "display": True,
                "state": "PENDING",
                "source": None,
                "item": None,
            }
        else:
            try:
                payload = api_request(
                    f"/api/v1/events/{event_path_id}/capture-explanation",
                    timeout_seconds=3,
                )
            except Exception:
                payload = dict(initial_payload)
                payload["display"] = True
                payload["state"] = "FAILED_RETRYING"
                payload.setdefault("item", None)
        if str(payload.get("state") or "") in {
            "READY",
            "FAILED_RETRYING",
            "NOT_APPLICABLE",
            "NO_CAPTURE_TEXT",
            "REFETCH_PRIMARY_SOURCE",
        }:
            st.session_state[final_key] = payload
    render_capture_explanation_payload(payload)


def public_evidence_sort_key(item: dict[str, object]) -> tuple[int, int, float, str]:
    eligibility_rank = 0 if int(item.get("reader_eligible") or 0) == 1 else 1
    authority = str(item.get("authority_tier") or "P9").upper()
    try:
        rank = int(authority[1:]) if authority.startswith("P") else 9
    except ValueError:
        rank = 9
    try:
        passage_score = float(item.get("passage_score") or 0)
    except (TypeError, ValueError):
        passage_score = 0.0
    return eligibility_rank, rank, -passage_score, str(item.get("evidence_id") or "")


def public_event_snapshot(
    event: dict[str, object],
    detail: dict[str, object],
    evidence: list[dict[str, object]],
) -> dict[str, object]:
    version = detail.get("current_version") or {}
    version_number = version.get("version") if isinstance(version, dict) else None
    semantic_input = dict(event)
    if not isinstance(semantic_input.get("risk_assessment"), dict):
        semantic_input["risk_assessment"] = detail.get("risk_assessment")
    if not isinstance(semantic_input.get("semantic_assessment"), dict):
        semantic_input["semantic_assessment"] = detail.get("semantic_assessment")
    posture = public_event_evidence_posture(semantic_input)
    risk = public_event_risk_assessment(semantic_input)
    legacy_state = public_event_state(event)
    return {
        "last_updated_at": str(event.get("last_updated_at") or ""),
        "evidence_posture": posture["key"],
        "citation_ready": posture["citation_ready"],
        "risk_route": risk["route"],
        "risk_model_version": risk["model_version"],
        "disposition": legacy_state if legacy_state == "excluded" else "",
        "version": version_number or event.get("current_version"),
        "evidence_ids": sorted(
            str(item.get("evidence_id") or "")
            for item in evidence
            if str(item.get("evidence_id") or "")
        ),
    }


def public_event_changes(
    previous: dict[str, object] | None,
    current: dict[str, object],
) -> list[str]:
    if not previous:
        return []
    changes: list[str] = []
    if previous.get("evidence_posture") != current.get("evidence_posture"):
        changes.append(
            "证据姿态："
            f"{previous.get('evidence_posture') or '未记录'} → "
            f"{current.get('evidence_posture') or '未记录'}"
        )
    if previous.get("citation_ready") != current.get("citation_ready"):
        changes.append("正式引用条件发生变化。")
    if (
        previous.get("risk_route") != current.get("risk_route")
        or previous.get("risk_model_version") != current.get("risk_model_version")
    ):
        changes.append("模型研判或模型版本发生变化。")
    if previous.get("disposition") != current.get("disposition"):
        changes.append("异常处置记录发生变化。")
    if previous.get("version") != current.get("version"):
        changes.append(f"事件版本：{previous.get('version') or '未记录'} → {current.get('version') or '未记录'}")
    previous_ids = set(previous.get("evidence_ids") or [])
    current_ids = set(current.get("evidence_ids") or [])
    added = len(current_ids - previous_ids)
    removed = len(previous_ids - current_ids)
    if added or removed:
        changes.append(f"关联证据：新增 {added} 条，移除 {removed} 条")
    if previous.get("last_updated_at") != current.get("last_updated_at") and not changes:
        changes.append("事件记录的最后更新时间发生变化；证据姿态与关联材料未变。")
    return changes


def public_time_value(value: object, *, date_only: bool = False) -> str:
    """Format a declared timestamp while preserving the difference between date and time."""
    normalized = " ".join(str(value or "").split())
    if not normalized:
        return "未记录"
    if date_only or len(normalized) == 10:
        return normalized[:10]
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return normalized[:80]
    if parsed.tzinfo is None:
        return parsed.strftime("%Y-%m-%d %H:%M") + "（时区未记录）"
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def public_time_markup(event: dict[str, object], detail: dict[str, object]) -> str:
    """Render event/source clocks without exposing internal review workflow."""
    version = detail.get("current_version") or {}
    facts = version.get("facts") or {} if isinstance(version, dict) else {}
    if not isinstance(facts, dict):
        facts = {}
    preferred = detail.get("preferred_source") or {}
    if not isinstance(preferred, dict):
        preferred = {}
    published_at = (
        preferred.get("source_published_at")
        or facts.get("source_published_at")
        or facts.get("published_at")
    )
    values = (
        ("事件日", public_time_value(event.get("event_date"), date_only=True)),
        ("来源发布", public_time_value(published_at)),
        ("系统发现", public_time_value(event.get("first_seen_at"))),
        ("最后更新", public_time_value(event.get("last_updated_at"))),
    )
    cells = "".join(
        '<div class="event-time-cell"><span>{}</span><strong>{}</strong></div>'.format(
            escape(label), escape(value)
        )
        for label, value in values
    )
    return (
        '<section class="event-time-facts" aria-label="事件与来源时间">'
        '<div class="event-time-heading">时间口径 · 事件本身、来源发布、系统处理不是同一个时间</div>'
        f'<div class="event-time-grid">{cells}</div>'
        '</section>'
    )

st.set_page_config(page_title="态势总览 · Finance Radar", page_icon="◎", layout="wide")
install_style()
render_primary_navigation("home")

page_targets = {
    "Replay_Lab": "pages/2_Replay_Lab.py",
    "Method_and_Boundaries": "pages/5_Method_and_Boundaries.py",
}
if UI_ROLE == "admin":
    page_targets.update(
        {
            "Event_Intelligence": "pages/1_Event_Intelligence.py",
            "Operations_and_Model": "pages/3_Operations_and_Model.py",
            "Adjudication_Studio": "pages/4_Adjudication_Studio.py",
        }
    )
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

header(
    "态势总览",
    "事件记录、证据姿态与模型研判" if UI_ROLE != "admin" else "事件流、复核队列与运行态总览",
)
no_trading_banner()
overview_loading = st.empty()
overview_loading.caption("正在读取采集状态与证据概览…")
try:
    overview, overview_cache = cached_api_get(
        "/api/v1/overview",
        ttl_seconds=15,
        stale_if_error_seconds=120,
        # The API serves a precomputed projection.  Keep a generous first-read
        # ceiling for process restarts and transient proxy scheduling delays.
        timeout_seconds=20,
    )
except Exception as exc:
    overview_loading.empty()
    render_api_error(exc)
    st.stop()
else:
    overview_loading.empty()
if overview_cache.stale:
    st.warning(
        "概览接口本次刷新失败；当前显示的是 "
        f"{format_elapsed(overview_cache.age_seconds)} 前的进程内快照。事件列表仍会单独读取。"
    )

public_timing = overview.get("timing") or {}
public_worker_age = public_timing.get("latest_worker_success_age_seconds")
try:
    public_worker_age_seconds = float(public_worker_age)
except (TypeError, ValueError):
    public_worker_age_seconds = None
public_worker_fresh = (
    public_worker_age_seconds is not None and public_worker_age_seconds <= 30 * 60
)
public_worker_stale = UI_ROLE != "admin" and public_worker_age_seconds is not None and not public_worker_fresh
public_worker_unknown = UI_ROLE != "admin" and public_worker_age_seconds is None

if public_worker_stale:
    st.error(
        "完整数据处理周期已中断：最近一次完整成功为 "
        f"{format_elapsed(public_worker_age)} 前。部分来源可能仍已采集，"
        "但尚未完成核验、路由与索引；以下只展示已经持久化的历史记录，不是实时信息。"
    )
elif public_worker_unknown:
    st.warning(
        "数据更新状态无法确认：没有获得最近一次成功采集时间。"
        "以下内容是事件记录，不能视为实时信息。"
    )

active_flow = str(st.query_params.get("preview_flow") or "全部事件")
if active_flow not in FLOW_PRESETS:
    active_flow = "全部事件"
preview_query = str(st.query_params.get("preview_query") or "")
# Legacy public workflow filters hid most of the canonical inventory.  Ignore
# and remove them on public pages; workflow filtering remains admin-only.
if UI_ROLE != "admin":
    st.query_params.pop("preview_state", None)
public_family = str(st.query_params.get("preview_family") or "")
public_source = str(st.query_params.get("preview_source") or "")
public_period = query_choice(
    st.query_params.get("preview_period"),
    frozenset(PUBLIC_PERIODS),
    "全部时间",
)
public_sort = query_choice(
    st.query_params.get("preview_sort"),
    frozenset(PUBLIC_SORTS.values()),
    "latest",
)
public_page_size = bounded_int(
    st.query_params.get("preview_page_size"),
    24,
    minimum=12,
    maximum=48,
)
if public_page_size not in {12, 24, 48}:
    public_page_size = 24
public_page = bounded_int(st.query_params.get("preview_page"), 1, minimum=1, maximum=10000)


def public_filter_return_url(event_id: str) -> str:
    params = {
        "preview_query": preview_query,
        "preview_family": public_family,
        "preview_source": public_source,
        "preview_period": public_period if public_period != "全部时间" else "",
        "preview_sort": public_sort,
        "preview_page_size": public_page_size,
        "preview_page": public_page if public_page > 1 else "",
    }
    query = urlencode({key: value for key, value in params.items() if value not in (None, "")})
    return f"./{'?' + query if query else ''}#{event_anchor_id(event_id)}"

counts = overview["counts"]
event_status = overview["event_status"]
rough_reviewed = int(
    overview.get("rough_reviewed")
    or (overview.get("job_status") or {}).get("COMPLETED_AUTHORIZED_ROUGH_REVIEW")
    or 0
)
canonical_public_funnel = overview.get("public_funnel") or {
    "total": int(counts.get("canonical_events") or 0),
    "verified": int(event_status.get("verified") or 0),
    "excluded": int(event_status.get("rejected") or 0),
    "insufficient": int(event_status.get("weak") or 0),
    "rough_reviewed": rough_reviewed,
    "pending_verification": max(
        0,
        int(event_status.get("candidate") or 0) - rough_reviewed,
    ),
}
public_funnel = canonical_public_funnel
reader_funnel = overview.get("reader_funnel") or {}
reader_ready_count = max(0, int(reader_funnel.get("total") or 0))
canonical_inventory_count = max(
    reader_ready_count,
    int(canonical_public_funnel.get("total") or counts.get("canonical_events") or 0),
)
evidence_gap_inventory = max(0, canonical_inventory_count - reader_ready_count)
timing = overview.get("timing", {})
event_age = timing.get("latest_new_event_age_seconds", timing.get("latest_event_age_seconds"))
worker_age = timing.get("latest_worker_success_age_seconds")
cycle_duration = timing.get("worker_cycle_duration_seconds") if UI_ROLE == "admin" else None
if UI_ROLE == "admin":
    states = [source_health_state(source) for source in overview.get("source_health", [])]
    source_ok = sum(state[1] == "OK" for state in states)
    source_watch = sum(state[1] == "WATCH" for state in states)
    source_error = sum(state[1] == "ERROR" for state in states)
    p0_sources = sum(
        source.get("authority_tier") == "P0" for source in overview.get("source_health", [])
    )
    attention_sources = source_watch + source_error

review_queue = int(overview.get("review_queue") or 0)
if UI_ROLE != "admin":
    brief_copy = f"账本中的 {canonical_inventory_count:,} 条事件现在全部可以浏览。"
    brief_copy += (
        f"其中 {reader_ready_count:,} 条达到正式引用条件；"
        f"其余 {evidence_gap_inventory:,} 条按一手材料、来源捕获或尚无来源分级展示。"
    )
elif review_queue:
    brief_copy = f"有 {review_queue:,} 条可读事件仍在等待证据或规则核验。"
    brief_copy += "先在当前页预览证据与下一步行动，需要完整工具时再进入人工复核。"
else:
    brief_copy = "当前没有等待复核的事件。可以浏览最新事件流，或按需展开运行健康与来源状态。"
situation_brief(
    "先看需要判断的事件" if UI_ROLE == "admin" else "全部事件，分级展示",
    brief_copy,
    focus_label="当前优先级" if UI_ROLE == "admin" else "当前可见性",
    focus_value=(
        f"{review_queue:,} 待复核"
        if UI_ROLE == "admin" and review_queue
        else ("队列已清" if UI_ROLE == "admin" else f"全部可浏览 {canonical_inventory_count:,}")
    ),
    focus_state=(
        "watch"
        if (UI_ROLE == "admin" and review_queue)
        or (UI_ROLE != "admin" and evidence_gap_inventory)
        else "ok"
    ),
)
if UI_ROLE != "admin":
    st.markdown(
        '<a class="mobile-primary-action" href="#live-events" target="_self">'
        '查看事件与证据 <span aria-hidden="true">↓</span></a>',
        unsafe_allow_html=True,
    )
    workspace_cols = st.columns(3, gap="small")
    workspace_actions = (
        ("今日新增", {"preview_period": "最近 24 小时", "preview_sort": "latest"}),
        ("最近 7 天", {"preview_period": "最近 7 天", "preview_sort": "latest"}),
        ("全部事件", {"preview_period": "", "preview_sort": "latest"}),
    )
    for column, (label, updates) in zip(workspace_cols, workspace_actions, strict=True):
        if column.button(label, width="stretch", key=f"public-workspace-{label}"):
            for key in (
                "preview_period",
                "preview_event_id",
                "preview_page",
            ):
                st.query_params.pop(key, None)
            for key, value in updates.items():
                st.query_params[key] = value
            st.rerun()
    st.caption("三个时间入口只改变发生时间范围；证据姿态和模型研判会显示在每条事件上。")
if UI_ROLE == "admin":
    with st.form("terminal-global-search", border=False):
        search_col, submit_col = st.columns([5.4, .8], gap="small", vertical_alignment="bottom")
        terminal_query = search_col.text_input(
            "全终端检索",
            placeholder="搜索公司、Ticker、事件类型或 Event ID",
            label_visibility="collapsed",
            value=preview_query,
        )
        search_submitted = submit_col.form_submit_button("检索 /", width="stretch")
    if search_submitted:
        search_state = terminal_search_state(terminal_query)
        st.query_params.clear()
        st.query_params["preview_flow"] = "全部事件"
        if search_state["q"]:
            st.query_params["preview_query"] = search_state["q"]
        st.rerun()
    render_saved_public_flow_manager(
        "",
        public_family,
        preview_query,
        public_period if public_period != "全部时间" else "",
        public_sort,
        public_page_size,
        source=public_source,
    )
    status_items = [
        ("待复核", f"{review_queue:,}", "watch" if review_queue else "ok"),
        ("已核验证据", f"{event_status.get('verified', 0):,}", "ok"),
        (
            "需关注来源",
            f"{attention_sources:,}",
            "risk" if source_error else ("watch" if source_watch else "ok"),
        ),
        ("最新事件", format_elapsed(event_age), ""),
    ]
else:
    latest_worker = overview.get("latest_worker_cycle") or {}
    worker_status = str(latest_worker.get("status") or "").upper()
    worker_has_finished_time = public_worker_age_seconds is not None
    collector_ok = worker_status in {"SUCCESS", "COMPLETED", "OK"} and public_worker_fresh
    collector_label = (
        "正常"
        if collector_ok
        else (
            "更新状态未知"
            if not worker_has_finished_time
            else ("更新已中断" if not public_worker_fresh else "最近采集异常")
        )
    )
    collector_state = "ok" if collector_ok else ("risk" if public_worker_stale or public_worker_unknown else "watch")
    status_items = [
        ("采集状态", collector_label, collector_state),
        ("最近成功采集", format_elapsed(worker_age), collector_state),
        (
            "事件总量",
            f"{canonical_inventory_count:,} 条",
            "ok" if canonical_inventory_count else "watch",
        ),
        (
            "正式可引用 / 其他证据姿态",
            f"{reader_ready_count:,} / {evidence_gap_inventory:,}",
            "watch" if evidence_gap_inventory else "ok",
        ),
    ]
status_strip(status_items)
if UI_ROLE != "admin":
    st.markdown(
        '<p class="public-health-explainer">'
        f'采集、证据归类与模型研判是独立时间线（最近发现新事件：{escape(format_elapsed(event_age))} 前）。'
        '全部规范事件均可浏览；“正式可引用”只统计已有官方原文支持的结构化事实。'
        '其余记录按证据姿态展示，不会因为内部流程进度从事件流中消失。'
        '</p>',
        unsafe_allow_html=True,
    )
if UI_ROLE == "admin":
    render_evidence_route(event_status, review_queue)
    render_flow_shortcuts(event_status, public=False)

facets: dict[str, object] = {"families": [], "sources": []}
feed_loading = st.empty()
if UI_ROLE != "admin":
    feed_loading.markdown(
        '<section class="fr-loading-state" role="status" aria-live="polite">'
        '<strong class="fr-state-title">正在加载当前筛选的事件</strong>'
        '<p class="fr-state-copy">筛选条件和当前页面会被保留；不会切换到其他页面。</p>'
        '</section>',
        unsafe_allow_html=True,
    )
try:
    facets, facets_cache = cached_api_get(
        "/api/v1/events/facets",
        ttl_seconds=30,
        stale_if_error_seconds=300,
        timeout_seconds=5,
    )
except Exception:
    pass
else:
    if facets_cache.stale:
        st.caption(
            "筛选项刷新失败；暂用 "
            f"{format_elapsed(facets_cache.age_seconds)} 前的筛选快照。"
        )

if UI_ROLE != "admin":
    family_options = facet_values(facets, "families", public_family)
    source_options = facet_values(facets, "sources", public_source)
    family_counts = facet_counts(facets, "families")
    source_counts = facet_counts(facets, "sources")
    selected_period_label = public_period
    selected_sort_label = next(
        (label for label, value in PUBLIC_SORTS.items() if value == public_sort),
        "最近发现",
    )
    with st.form("public-event-filters", border=False):
        search_col, submit_col = st.columns([5.2, 1], gap="small", vertical_alignment="bottom")
        terminal_query = search_col.text_input(
            "搜索事件",
            placeholder="搜索公司、Ticker、事件类别或 Event ID",
            value=preview_query,
        )
        search_submitted = submit_col.form_submit_button("应用筛选", width="stretch")
        family_col, source_col, period_col, sort_col, size_col = st.columns(
            [1.4, 1.5, 1.05, 1.05, .8],
            gap="small",
        )
        selected_family = family_col.selectbox(
            "事件类别",
            family_options,
            index=family_options.index(public_family),
            format_func=lambda value: public_family_label(value, family_counts),
        )
        selected_source = source_col.selectbox(
            "信息来源",
            source_options,
            index=source_options.index(public_source),
            format_func=lambda value: public_source_label(value, source_counts),
        )
        selected_period_label = period_col.selectbox(
            "发生时间",
            list(PUBLIC_PERIODS),
            index=list(PUBLIC_PERIODS).index(public_period),
        )
        selected_sort_label = sort_col.selectbox(
            "排序方式",
            list(PUBLIC_SORTS),
            index=list(PUBLIC_SORTS).index(selected_sort_label),
        )
        selected_page_size = size_col.selectbox(
            "每页",
            [12, 24, 48],
            index=[12, 24, 48].index(public_page_size),
            format_func=lambda value: f"{value} 条",
        )
    if search_submitted:
        normalized_query = terminal_search_state(terminal_query)["q"]
        updates = {
            "preview_query": normalized_query,
            "preview_family": selected_family,
            "preview_source": selected_source,
            "preview_period": selected_period_label if selected_period_label != "全部时间" else "",
            "preview_sort": PUBLIC_SORTS[selected_sort_label],
            "preview_page_size": str(selected_page_size),
        }
        for key, value in updates.items():
            if value:
                st.query_params[key] = value
            else:
                st.query_params.pop(key, None)
        st.query_params.pop("preview_event_id", None)
        st.query_params.pop("preview_page", None)
        st.rerun()

feed_error: Exception | None = None
try:
    if UI_ROLE == "admin":
        feed_result = api_request(
            query_path(
                "/api/v1/events",
                status=FLOW_PRESETS[active_flow]["status"],
                q=preview_query,
                limit=12,
            )
        )
    else:
        period_days = PUBLIC_PERIODS[public_period]
        date_from = (
            (datetime.now(timezone.utc).date() - timedelta(days=period_days)).isoformat()
            if period_days
            else None
        )
        feed_path = query_path(
                "/api/v1/events",
                family=public_family,
                source=public_source,
                q=preview_query,
                date_from=date_from,
                sort=public_sort,
                limit=public_page_size,
                offset=(public_page - 1) * public_page_size,
            )
        feed_result, feed_cache = cached_api_get(
            feed_path,
            ttl_seconds=20,
            stale_if_error_seconds=180,
            timeout_seconds=20,
        )
        if feed_cache.stale:
            st.caption(
                "事件流刷新失败；暂用 "
                f"{format_elapsed(feed_cache.age_seconds)} 前的安全快照。"
            )
    live_feed = list(feed_result.get("items") or [])
    live_total = int(feed_result.get("total") or 0)
    if UI_ROLE != "admin":
        seen_events = st.session_state.get(PUBLIC_EVENT_SEEN_STATE_KEY, {})
        if not isinstance(seen_events, dict):
            seen_events = {}
        for item in live_feed:
            previous = seen_events.get(str(item.get("event_id") or ""))
            if not isinstance(previous, dict):
                continue
            feed_posture = public_event_evidence_posture(item)
            feed_risk = public_event_risk_assessment(item)
            legacy_state = public_event_state(item)
            current_feed_state = {
                "last_updated_at": str(item.get("last_updated_at") or ""),
                "evidence_posture": feed_posture["key"],
                "citation_ready": feed_posture["citation_ready"],
                "risk_route": feed_risk["route"],
                "risk_model_version": feed_risk["model_version"],
                "disposition": legacy_state if legacy_state == "excluded" else "",
                "version": item.get("current_version"),
            }
            item["_changed_since_view"] = any(
                previous.get(key) != current_feed_state.get(key)
                for key in current_feed_state
            )
except Exception as exc:
    # A failed filtered request must not silently display the overview's
    # unrelated recent-event sample under the active filters.
    live_feed = []
    live_total = 0
    feed_error = exc
finally:
    feed_loading.empty()

preview_event_id = str(st.query_params.get("preview_event_id") or "")
if preview_event_id:
    preview_event_path_id = quote(preview_event_id, safe="")
    st.markdown('<div id="event-preview" class="event-preview-focus"></div>', unsafe_allow_html=True)
    focus_event_preview(preview_event_id)
    preview_loading = st.empty()
    preview_loading.markdown(
        '<section class="fr-loading-state fr-loading-compact" role="status" aria-live="polite">'
        '<strong class="fr-state-title">正在打开当前页事件预览</strong>'
        '<p class="fr-state-copy">筛选、排序和分页会保持不变。</p>'
        '</section>',
        unsafe_allow_html=True,
    )
    try:
        preview_sources: list[dict[str, object]] = []
        preview_knowledge: dict[str, object] = {}
        preview_capture_explanation: dict[str, object] = {}
        preview_load_error: Exception | None = None
        preview_cache_stale = False
        preview_sources_error: Exception | None = None
        if UI_ROLE != "admin":
            try:
                dossier, dossier_cache = cached_api_get(
                    f"/api/v1/events/{preview_event_path_id}/dossier",
                    ttl_seconds=60,
                    stale_if_error_seconds=900,
                    timeout_seconds=20,
                )
                preview_cache_stale = dossier_cache.stale
            except Exception as dossier_exc:
                # A timeout or service failure is not evidence that the legacy
                # endpoints will be healthier.  Retrying four sequential reads
                # used to turn one 20-second timeout into a much longer wait and
                # then replace the selected event with a global outage card.
                # Keep the compatibility path only for an old deployment (404)
                # and for local test doubles that do not expose the dossier.
                message = str(dossier_exc)
                use_legacy_endpoints = "API 404" in message or not message.startswith("API ")
                if not use_legacy_endpoints:
                    feed_preview = next(
                        (
                            dict(item)
                            for item in live_feed
                            if str(item.get("event_id") or "") == preview_event_id
                        ),
                        None,
                    )
                    if feed_preview is None:
                        raise
                    preview_load_error = dossier_exc
                    preview_sources_error = dossier_exc
                    preview_detail = {
                        "event": feed_preview,
                        "current_version": {"facts": {}},
                    }
                    preview_evidence = []
                else:
                    preview_detail = api_request(f"/api/v1/events/{preview_event_path_id}")
                    preview_evidence = sorted(
                        api_request(f"/api/v1/events/{preview_event_path_id}/evidence")["items"],
                        key=public_evidence_sort_key,
                    )
                    try:
                        knowledge_response = api_request(
                            f"/api/v1/events/{preview_event_path_id}/knowledge"
                        )
                        if isinstance(knowledge_response, dict):
                            preview_knowledge = knowledge_response
                    except Exception:
                        preview_knowledge = {}
                    try:
                        source_response = api_request(
                            f"/api/v1/events/{preview_event_path_id}/sources"
                        )
                        source_items = source_response.get("items", [])
                        if isinstance(source_items, list):
                            preview_sources = [
                                item for item in source_items if isinstance(item, dict)
                            ]
                    except Exception as exc:
                        preview_sources_error = exc
                    if preview_sources:
                        try:
                            explanation_response = api_request(
                                f"/api/v1/events/{preview_event_path_id}/capture-explanation",
                                timeout_seconds=3,
                            )
                            if isinstance(explanation_response, dict):
                                preview_capture_explanation = explanation_response
                        except Exception:
                            preview_capture_explanation = {
                                "display": not bool(preview_evidence),
                                "state": "PENDING",
                                "source": preview_sources[0],
                            }
            else:
                preview_detail = dossier.get("detail") or {}
                preview_evidence = sorted(
                    (dossier.get("evidence") or {}).get("items", []),
                    key=public_evidence_sort_key,
                )
                knowledge_response = dossier.get("knowledge") or {}
                if isinstance(knowledge_response, dict):
                    preview_knowledge = knowledge_response
                explanation_state = dossier.get("capture_explanation") or {}
                if isinstance(explanation_state, dict):
                    preview_capture_explanation = explanation_state
            preview_evidence = [
                item
                for item in preview_evidence
                if int(item.get("reader_eligible") or 0) == 1
            ]
        else:
            preview_detail = api_request(f"/api/v1/events/{preview_event_path_id}")
            preview_evidence = sorted(
                api_request(f"/api/v1/events/{preview_event_path_id}/evidence")["items"],
                key=public_evidence_sort_key,
            )
            try:
                knowledge_response = api_request(
                    f"/api/v1/events/{preview_event_path_id}/knowledge"
                )
                if isinstance(knowledge_response, dict):
                    preview_knowledge = knowledge_response
            except Exception:
                preview_knowledge = {}
            try:
                source_response = api_request(
                    f"/api/v1/events/{preview_event_path_id}/sources"
                )
                source_items = source_response.get("items", [])
                if isinstance(source_items, list):
                    preview_sources = [
                        item for item in source_items if isinstance(item, dict)
                    ]
            except Exception as exc:
                preview_sources_error = exc
    except Exception as exc:
        render_api_error(exc)
    else:
        preview_event = preview_detail["event"]
        preview_version = preview_detail.get("current_version") or {}
        if not isinstance(preview_version, dict):
            preview_version = {}
        preview_facts = preview_version.get("facts") or {}
        if not isinstance(preview_facts, dict):
            preview_facts = {}
        preview_model = (
            preview_detail.get("model_shadow_output") or {} if UI_ROLE == "admin" else {}
        )
        if UI_ROLE == "admin":
            preview_company = preview_event.get("company_name") or preview_event_id
            preview_type = str(preview_event.get("event_type") or "event").replace("_", " ")
            preview_summary = (
                preview_facts.get("evidence_summary")
                or preview_event.get("evidence_excerpt")
                or "尚无结构化事件摘要。"
            )
            preview_summary = " ".join(str(preview_summary).split())
        else:
            copy_input = dict(preview_event)
            copy_input["facts"] = preview_facts
            if not isinstance(copy_input.get("risk_assessment"), dict):
                copy_input["risk_assessment"] = preview_detail.get("risk_assessment")
            if not isinstance(copy_input.get("semantic_assessment"), dict):
                copy_input["semantic_assessment"] = preview_detail.get(
                    "semantic_assessment"
                )
            copy_input.setdefault("citable_evidence_count", len(preview_evidence))
            copy_input.setdefault("captured_source_count", len(preview_sources))
            if preview_evidence and not copy_input.get("credibility_tier"):
                copy_input["credibility_tier"] = preview_evidence[0].get("authority_tier")
            public_copy = public_event_copy(copy_input)
            preview_company = public_copy["subject"]
            preview_type = public_copy["family"]
            seen_events = st.session_state.get(PUBLIC_EVENT_SEEN_STATE_KEY, {})
            if not isinstance(seen_events, dict):
                seen_events = {}
            previous_snapshot = seen_events.get(preview_event_id)
            current_snapshot = public_event_snapshot(
                preview_event,
                preview_detail,
                preview_evidence,
            )
            changes_since_view = public_event_changes(
                previous_snapshot if isinstance(previous_snapshot, dict) else None,
                current_snapshot,
            )
            seen_events[preview_event_id] = current_snapshot
            st.session_state[PUBLIC_EVENT_SEEN_STATE_KEY] = seen_events
        with st.container(border=True):
            section_header(
                "当前页事件预览",
                "留在态势总览 · 核对原始证据与上下文"
                if UI_ROLE != "admin"
                else "留在态势总览 · 需要时再进入人工复核",
            )
            if preview_load_error is not None and UI_ROLE != "admin":
                st.warning(
                    "事件详情读取超时；当前先展示事件流中已经加载的摘要。"
                    "系统没有用旧证据或猜测内容补位，请稍后重试原始证据。"
                )
            elif preview_cache_stale and UI_ROLE != "admin":
                st.caption("详情刷新暂时失败；当前展示最近一次成功读取的只读快照。")
            if UI_ROLE == "admin":
                st.markdown(
                    '<div class="home-event-preview">'
                    f'<div class="event-kicker">{escape(str(preview_event.get("event_date") or "—"))} · '
                    f'{escape(str(preview_event.get("ticker_at_event") or "NO TICKER"))} · '
                    f'{escape(preview_type.upper())}</div>'
                    f'<div class="event-headline">{escape(str(preview_company))}</div>'
                    f'<div class="event-summary">{escape(str(preview_summary))}</div>'
                    '</div>',
                    unsafe_allow_html=True,
                )
            else:
                evidence_authority = (
                    PUBLIC_AUTHORITY_LABELS.get(
                        str(preview_evidence[0].get("authority_tier") or "P?"),
                        "来源待核实",
                    )
                    if preview_evidence
                    else "尚无可引用证据"
                )
                citation_copy = (
                    "达到正式引用条件"
                    if public_copy["citation_ready"]
                    else "尚未达到正式引用条件"
                )
                gap_copy = (
                    f" · 当前缺口：{escape(str(public_copy['evidence_gaps']))}"
                    if public_copy["evidence_gaps"]
                    else ""
                )
                disposition_copy = (
                    f" · {escape(str(public_copy['disposition_label']))}"
                    if public_copy["disposition_label"]
                    else ""
                )
                risk_meta = ""
                if public_copy["risk_confidence"]:
                    risk_meta += f" · 置信度 {escape(str(public_copy['risk_confidence']))}"
                if public_copy["risk_model_version"]:
                    risk_meta += f" · 模型 {escape(str(public_copy['risk_model_version']))}"
                st.markdown(
                    '<section class="event-answer" aria-label="事件阅读摘要">'
                    '<div class="event-answer-meta">'
                    f'<span>{escape(str(preview_event.get("event_date") or "日期待确认"))}</span>'
                    f'<span>{escape(str(preview_event.get("ticker_at_event") or "无证券代码"))}</span>'
                    f'<span>{escape(public_copy["family"])}</span>'
                    '</div>'
                    f'<h2>{escape(preview_company)}</h2>'
                    '<div class="event-answer-grid">'
                    '<article><span>发生了什么</span>'
                    f'<p>{escape(public_copy["summary"])}</p></article>'
                    '<article><span>为什么关注</span>'
                    f'<p>{escape(public_copy["relevance"])}</p></article>'
                    '<article><span>证据与引用</span>'
                    f'<p><strong>{escape(str(public_copy["evidence_label"]))}</strong> · {citation_copy}'
                    f'{gap_copy}{disposition_copy} · {len(preview_evidence):,} 条可读证据 · {escape(evidence_authority)}'
                    + (
                        f' · {len(preview_sources):,} 条采集来源记录'
                        if preview_sources
                        else ''
                    )
                    + '</p></article>'
                    f'<article><span>{escape(str(public_copy["risk_heading"]))}</span>'
                    f'<p><strong>{escape(str(public_copy["risk_label"]))}</strong>{risk_meta} · '
                    f'{escape(str(public_copy["risk_explanation"]))}</p></article>'
                    '</div>'
                    '</section>',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    public_time_markup(preview_event, preview_detail),
                    unsafe_allow_html=True,
                )
                if previous_snapshot is None:
                    st.caption("本次浏览会话首次查看；系统已记住当前版本，后续变化会在这里说明。")
                elif changes_since_view:
                    st.markdown("**自上次查看后的变化**")
                    for change in changes_since_view:
                        st.markdown(f"- {escape(change)}")
                else:
                    st.caption("与本次浏览会话中的上次查看相比，证据姿态、模型研判和材料集合没有变化。")
            if preview_evidence and UI_ROLE == "admin":
                top_evidence = preview_evidence[0]
                top_passage = (
                    top_evidence.get("evidence_passage")
                    or top_evidence.get("observation_summary")
                    or "暂无精确证据段落"
                )
                top_passage = " ".join(str(top_passage).split())
                authority = str(top_evidence.get("authority_tier") or "P?")
                evidence_label = f"最高权威 {authority}"
                st.markdown(
                    '<div class="preview-evidence">'
                    f'<span>{escape(evidence_label)}</span>'
                    f'<p>{escape(top_passage)}</p>'
                    '</div>',
                    unsafe_allow_html=True,
                )
            elif preview_evidence:
                top_evidence = preview_evidence[0]
                top_passage = " ".join(
                    str(
                        top_evidence.get("evidence_passage")
                        or top_evidence.get("observation_summary")
                        or "暂无精确证据段落"
                    ).split()
                )
                if len(top_passage) > 900:
                    top_passage = top_passage[:897].rstrip() + "…"
                with st.expander("查看原始证据节选（可能为英文）", expanded=False):
                    st.markdown(
                        '<div class="preview-evidence raw-evidence">'
                        '<span>原始证据 · 请结合完整文件阅读</span>'
                        f'<p>{escape(top_passage)}</p>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
            if preview_sources and UI_ROLE == "admin":
                source = preview_sources[0]
                source_title = " ".join(
                    str(
                        source.get("source_title")
                        or source.get("title")
                        or "已捕获一条来源记录"
                    ).split()
                )
                source_excerpt = " ".join(
                    str(
                        source.get("source_excerpt")
                        or source.get("summary")
                        or "来源没有提供更多摘要。"
                    ).split()
                )
                if len(source_excerpt) > 900:
                    source_excerpt = source_excerpt[:897].rstrip() + "…"
                with st.expander(
                    "查看来源捕获（非正式证据）",
                    expanded=not bool(preview_evidence),
                ):
                    st.markdown(
                        '<div class="preview-evidence raw-evidence discovery-capture">'
                        '<span>API 发现载荷 · 不参与正式结论</span>'
                        f'<h3>{escape(source_title)}</h3>'
                        f'<p>{escape(source_excerpt)}</p>'
                        '<small>该记录仅说明系统当时收到了什么；它不是 P0/P1 权威证据，'
                        '也不会改变事件事实、异常处置或触发交易。'
                        + (
                            f' 当前仅展示前 {int(source.get("source_excerpt_original_length") or 0):,} 字中的限长节选。'
                            if source.get("source_excerpt_truncated")
                            else ''
                        )
                        + '</small>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
            elif preview_sources_error and UI_ROLE == "admin":
                st.warning(
                    "采集来源记录暂时无法读取；这不表示原始输入为空，也不改变事件状态。"
                )
            if (
                UI_ROLE != "admin"
                and preview_capture_explanation.get("display") is True
            ):
                render_capture_explanation_fragment(
                    preview_event_path_id,
                    preview_event_id,
                    preview_capture_explanation,
                )
            if preview_knowledge.get("covered"):
                with st.expander("金融专业规则与核验清单", expanded=False):
                    st.markdown(
                        f"**为什么重要：** {escape(str(preview_knowledge.get('why_it_matters') or ''))}"
                    )
                    facts = preview_knowledge.get("facts_to_confirm") or []
                    if facts:
                        st.markdown("**需要确认的事实**")
                        for item in facts:
                            st.markdown(f"- {escape(str(item))}")
                    missing = preview_knowledge.get("still_missing_when") or []
                    if missing:
                        st.markdown("**出现以下情况时仍不能下结论**")
                        for item in missing:
                            st.markdown(f"- {escape(str(item))}")
                    counterexamples = preview_knowledge.get("what_would_change_the_view") or []
                    if counterexamples:
                        st.markdown("**常见反例 / 会改变当前看法的情况**")
                        for item in counterexamples:
                            st.markdown(f"- {escape(str(item))}")
                    st.caption("规则卡只帮助核验，不会自动改变事件状态，也不构成投资建议。")
            if UI_ROLE == "admin":
                render_next_action_prompt(
                    next_action_guidance(preview_event, preview_evidence, preview_model)
                )
            else:
                render_public_reading_prompt(copy_input, preview_evidence)
            if UI_ROLE == "admin":
                full_col, close_col = st.columns([1.25, 1], gap="small")
                if full_col.button(
                    "进入人工复核（切换工作区）",
                    type="primary",
                    width="stretch",
                    key="open-full-event-workbench",
                ):
                    st.session_state[DEEP_LINK_STATE_KEY] = {
                        "page": "Event_Intelligence",
                        "params": {"flow": active_flow, "event_id": preview_event_id},
                    }
                    st.switch_page(page_targets["Event_Intelligence"])
            else:
                method_col, close_col = st.columns([1.25, 1], gap="small")
                source_url = public_source_url(preview_evidence)
                capture_url = public_capture_url(preview_sources)
                if source_url:
                    method_col.link_button(
                        "直达本条原始来源（外部网站）",
                        source_url,
                        width="stretch",
                    )
                elif capture_url:
                    method_col.link_button(
                        "查看这条发现来源（非核验证据）",
                        capture_url,
                        width="stretch",
                    )
                else:
                    method_col.page_link(
                        "pages/5_Method_and_Boundaries.py",
                        label="如何理解证据与置信度",
                        width="stretch",
                    )
            if UI_ROLE == "admin":
                if close_col.button(
                    "收起当前页预览",
                    width="stretch",
                    key="close-home-event-preview",
                ):
                    st.query_params.pop("preview_event_id", None)
                    st.rerun()
            else:
                close_col.markdown(
                    '<a class="return-filter-link" target="_self" href="{}">返回原筛选位置</a>'.format(
                        escape(public_filter_return_url(preview_event_id), quote=True)
                    ),
                    unsafe_allow_html=True,
                )
    finally:
        preview_loading.empty()

if UI_ROLE != "admin" and feed_error is not None:
    st.markdown('<div id="live-events"></div>', unsafe_allow_html=True)
    section_header("事件浏览", "当前筛选的数据暂时不可用 · 未显示任何替代事件")
    render_api_error(feed_error)
    st.caption("已保留当前筛选、排序和分页设置；恢复后请刷新重新读取。")
elif UI_ROLE != "admin":
    total_pages = max(1, ceil(live_total / public_page_size))
    if live_total and public_page > total_pages:
        st.query_params["preview_page"] = str(total_pages)
        st.query_params.pop("preview_event_id", None)
        st.rerun()
    st.markdown(
        '<section class="queue-card queue-card-horizontal" aria-label="事件可见性与证据层级">'
        '<div>'
        '<div class="queue-card-label">全部可浏览</div>'
        f'<div class="queue-card-value">{canonical_inventory_count:,}</div>'
        '</div>'
        '<div class="queue-card-copy">'
        f'其中 {reader_ready_count:,} 条达到正式引用条件；'
        f'{evidence_gap_inventory:,} 条按“一手材料待补、仅捕获来源或尚无来源”如实展示。'
        '<div class="queue-card-next">下一步 · 打开事件，先读中文说明，再核对捕获来源或原始证据</div>'
        '</div>'
        '</section>',
        unsafe_allow_html=True,
    )
    st.markdown('<div id="live-events"></div>', unsafe_allow_html=True)
    section_header(
        "事件浏览",
        f"本页 {len(live_feed):,} 条 · 共 {live_total:,} 条 · 第 {public_page}/{total_pages} 页 · UTC",
    )
    if live_feed:
        link_context = {
            "preview_query": preview_query,
            "preview_family": public_family,
            "preview_source": public_source,
            "preview_period": public_period if public_period != "全部时间" else "",
            "preview_sort": public_sort,
            "preview_page_size": public_page_size,
            "preview_page": public_page,
        }
        render_event_feed(
            live_feed,
            flow=active_flow,
            public=True,
            link_context=link_context,
        )
    else:
        st.markdown(
            '<section class="fr-empty-state" role="status">'
            '<strong>当前筛选没有匹配事件</strong>'
            '<p>可以扩大时间范围、清除来源或类别筛选；系统不会用演示数据填充空结果。</p>'
            '</section>',
            unsafe_allow_html=True,
        )

    def public_page_url(page_number: int) -> str:
        params = {
            "preview_query": preview_query,
            "preview_family": public_family,
            "preview_source": public_source,
            "preview_period": public_period if public_period != "全部时间" else "",
            "preview_sort": public_sort,
            "preview_page_size": public_page_size,
            "preview_page": page_number if page_number > 1 else "",
        }
        return "./?" + urlencode(
            {key: value for key, value in params.items() if value not in (None, "")}
        ) + "#live-events"

    previous_link = (
        f'<a href="{escape(public_page_url(public_page - 1), quote=True)}" target="_self" aria-label="上一页">← 上一页</a>'
        if public_page > 1
        else '<span aria-disabled="true">← 上一页</span>'
    )
    next_link = (
        f'<a href="{escape(public_page_url(public_page + 1), quote=True)}" target="_self" aria-label="下一页">下一页 →</a>'
        if public_page < total_pages
        else '<span aria-disabled="true">下一页 →</span>'
    )
    st.markdown(
        '<div class="fr-pagination" role="group" aria-label="事件分页">'
        f'{previous_link}'
        f'<span class="fr-pagination-status">第 {public_page} / {total_pages} 页</span>'
        f'{next_link}'
        '<a href="./#live-events" target="_self">清除全部筛选</a>'
        '</div>',
        unsafe_allow_html=True,
    )
else:
    left, right = st.columns([1.78, .82], gap="large")
    with left:
        st.markdown('<div id="live-events"></div>', unsafe_allow_html=True)
        if feed_error is not None:
            section_header("实时事件流", f"{active_flow} · 当前筛选的数据暂时不可用")
            render_api_error(feed_error)
            st.caption("已保留当前筛选设置；恢复后请刷新重新读取。")
        else:
            feed_context = f"{active_flow} · 当前页预览"
            if preview_query:
                feed_context += f" · 搜索 {preview_query}"
            section_header("实时事件流", f"最新 {len(live_feed)} 条 · UTC · {feed_context}")
            if live_feed:
                render_event_feed(live_feed, flow=active_flow, public=False)
            else:
                st.info("暂无事件。")

    with right:
        section_header("系统复核队列", "证据复核")
        st.markdown(
            '<div class="queue-card">'
            '<div class="queue-card-label">等待人工复核</div>'
            f'<div class="queue-card-value">{review_queue:,}</div>'
            '<div class="queue-card-copy">'
            '模型只做 shadow 分流；证据不足、来源冲突或规则未闭合的事件都留给人判断。'
            '</div>'
            '<div class="queue-card-next">'
            '下一步 · 先在当前页预览，需要完整工具时再切换工作区'
            '</div>'
            '</div>',
            unsafe_allow_html=True,
        )
        if st.button("在当前页查看待复核事件", width="stretch"):
            st.query_params["preview_flow"] = "待复核"
            st.query_params.pop("preview_query", None)
            st.query_params.pop("preview_event_id", None)
            st.rerun()
        st.caption("完整筛选、证据代理和复核记录位于左侧“人工复核”。")

        with st.expander("系统与来源健康", expanded=False):
            pulse_grid(
                [
                    ("健康来源", source_ok, "ok"),
                    ("观察", source_watch, "watch" if source_watch else ""),
                    ("异常", source_error, "risk" if source_error else "ok"),
                    ("P0 来源", p0_sources, ""),
                    ("事件总数", f"{counts['canonical_events']:,}", ""),
                    ("证据边", f"{counts['event_evidence']:,}", ""),
                    (
                        "Worker",
                        f"{cycle_duration:.2f} s" if cycle_duration is not None else "—",
                        "ok",
                    ),
                    ("Schema", overview["schema_version"], ""),
                ]
            )
            audit = overview["audit"]
            if sum(audit.values()) == 0:
                st.markdown(
                    '<div class="boundary-ok" role="status">硬边界审计 0 违规 · NO TRADING / NO AUTO VERIFY / NO LEAKAGE</div>',
                    unsafe_allow_html=True,
                )
            else:
                st.error("内部边界审计需要处理。")
            st.caption(f"SQLite quick_check={overview['quick_check']}")

        with st.expander("其他工作面", expanded=False):
            render_command_palette(facets)

if UI_ROLE != "admin":
    # Product KPIs are useful context, but they must never delay the primary
    # event feed. Load them only after the current event page has rendered.
    product_quality: dict[str, object] | None = None
    product_quality_cache = None
    try:
        product_quality, product_quality_cache = cached_api_get(
            "/api/v1/product/metrics",
            ttl_seconds=60,
            stale_if_error_seconds=300,
            timeout_seconds=3,
        )
    except Exception:
        product_quality = None
    with st.expander("产品质量指标 · 30 天窗口", expanded=False):
        if product_quality is None:
            st.caption("当前无法测量产品质量指标；事件与证据浏览不受影响。")
        else:
            if product_quality_cache is not None and product_quality_cache.stale:
                st.caption(
                    "指标刷新失败；以下为 "
                    f"{format_elapsed(product_quality_cache.age_seconds)} 前的快照。"
                )
            metric_map = {
                str(item.get("id")): item
                for item in (product_quality.get("metrics") or [])
                if isinstance(item, dict)
            }

            def metric_text(metric_id: str, *, seconds: bool = False) -> str:
                metric = metric_map.get(metric_id) or {}
                if metric.get("status") != "MEASURED":
                    return "未测量"
                value = metric.get("value")
                if seconds:
                    return format_elapsed(value)
                if metric.get("unit") == "percent":
                    return f"{float(value or 0):.1f}%"
                return str(value)

            quality_cols = st.columns(4, gap="small")
            quality_cols[0].metric("发现延迟 P95", metric_text("capture_latency_p95", seconds=True))
            quality_cols[1].metric("可引用证据覆盖", metric_text("citable_evidence_coverage"))
            quality_cols[2].metric("事实闭合率", metric_text("evidence_closure_rate"))
            quality_cols[3].metric("待复核年龄 P95", metric_text("review_queue_age_p95", seconds=True))
            unavailable_count = sum(
                item.get("status") != "MEASURED" for item in metric_map.values()
            )
            st.caption(
                f"样本量与数据源随每项指标返回；{unavailable_count} 项当前明确标记为未测量。"
                "测试通过数和服务健康不替代用户价值。"
            )

st.caption(
    "J/K 在人工复核中切换事件 · / 聚焦检索 · 所有行情只读 · 所有模型输出均为 shadow"
    if UI_ROLE == "admin"
    else "全部事件均可浏览 · 证据姿态与模型研判分开表达 · 时间统一为 UTC · 原始材料可能保留发布语言"
)
