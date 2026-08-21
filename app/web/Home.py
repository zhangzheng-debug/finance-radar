from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from math import ceil
from urllib.parse import urlencode, urlsplit

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
    PUBLIC_STATE_LABELS,
    facet_counts,
    facet_values,
    event_anchor_id,
    focus_event_preview,
    next_action_guidance,
    public_event_copy,
    public_event_quality,
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


PUBLIC_STATES = frozenset(PUBLIC_STATE_LABELS)
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
    quality = public_event_quality(event, evidence)
    if state == "excluded":
        title = "保留排除结果，等待真正的新证据"
        reason = "当前线索已被排除；采集到的来源记录可解释系统为什么曾捕获它，但不构成事实证据。"
        steps = ("查看最初捕获内容", "核对排除理由", "只有新增高权威材料时才重新判断")
        tone = "ok"
    elif not quality["reader_ready"]:
        title = "这还不是一条可读事件"
        reason = "当前只是一条发现线索：" + "、".join(quality["gaps"]) + "。"
        steps = (
            "先确认是谁、做了什么以及处于哪个阶段",
            "找到监管、交易所或公司原始文件中的精确段落",
            "补齐中文事实摘要前，不把分类标签当作事件结论",
        )
        tone = "risk"
    elif state == "verified":
        title = "回到原始来源核对上下文"
        reason = f"当前事件已关联 {len(evidence)} 条证据；摘要仍不能替代完整原文。"
        steps = ("打开原始来源", "核对主体、日期和版本", "区分已发生事实与前瞻性表述")
        tone = "ok"
    elif state == "insufficient":
        title = "证据不足，暂不形成事实结论"
        reason = f"现有 {len(evidence)} 条关联材料仍不能闭合事实链。"
        steps = ("确认缺失的是主体、日期还是关键事实", "优先寻找官方原始文件", "补证前保留不确定性")
        tone = "risk"
    elif state == "rough_reviewed":
        title = "粗审已完成，继续核对正式证据"
        reason = f"现有 {len(evidence)} 条关联材料已完成快速筛查，但粗审不等于正式核验。"
        steps = ("先读最高权威证据段落", "核对主体、日期与文件版本", "不要把粗审状态理解为事实确认")
        tone = "watch"
    else:
        title = "把它当作待核验线索"
        reason = f"当前事件已关联 {len(evidence)} 条证据，但事实链仍未完全闭合。"
        steps = ("先读最高权威证据段落", "核对是否存在修订或来源冲突", "不要从核验状态推断价格方向")
        tone = "watch"
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
    return {
        "last_updated_at": str(event.get("last_updated_at") or ""),
        "status": public_event_state(event),
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
    if previous.get("status") != current.get("status"):
        changes.append(f"状态：{previous.get('status') or '未记录'} → {current.get('status') or '未记录'}")
    if previous.get("version") != current.get("version"):
        changes.append(f"事件版本：{previous.get('version') or '未记录'} → {current.get('version') or '未记录'}")
    previous_ids = set(previous.get("evidence_ids") or [])
    current_ids = set(current.get("evidence_ids") or [])
    added = len(current_ids - previous_ids)
    removed = len(previous_ids - current_ids)
    if added or removed:
        changes.append(f"关联证据：新增 {added} 条，移除 {removed} 条")
    if previous.get("last_updated_at") != current.get("last_updated_at") and not changes:
        changes.append("事件记录的最后更新时间发生变化；事实状态与证据集合未变。")
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


def has_recorded_public_verification(verification: dict[str, object] | None) -> bool:
    """Return true only for a public verification receipt with a timestamp.

    A canonical event may be marked ``verified`` by an older historical import
    without carrying a current verification receipt.  The public UI must not
    turn that status alone into a claim that a formal verification record is
    available.
    """

    if not isinstance(verification, dict):
        return False
    reviewed_at = " ".join(str(verification.get("reviewed_at") or "").split())
    return bool(reviewed_at)


def public_time_markup(event: dict[str, object], detail: dict[str, object], verification: dict[str, object] | None) -> str:
    """Render the five reader-facing clocks for a selected event."""
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
    verified_at = (verification or {}).get("reviewed_at") or event.get("reviewed_at")
    historical_verified_without_receipt = (
        public_event_state(event) == "verified"
        and not has_recorded_public_verification(verification)
    )
    values = (
        ("事件日", public_time_value(event.get("event_date"), date_only=True)),
        ("来源发布", public_time_value(published_at)),
        ("系统发现", public_time_value(event.get("first_seen_at"))),
        ("最后更新", public_time_value(event.get("last_updated_at"))),
        (
            "核验留痕" if historical_verified_without_receipt else "核验记录",
            "历史记录未存档"
            if historical_verified_without_receipt
            else public_time_value(verified_at),
        ),
    )
    cells = "".join(
        '<div class="event-time-cell"><span>{}</span><strong>{}</strong></div>'.format(
            escape(label), escape(value)
        )
        for label, value in values
    )
    return (
        '<section class="event-time-facts" aria-label="事件与核验时间">'
        '<div class="event-time-heading">时间口径 · 事件本身、来源发布、系统处理不是同一个时间</div>'
        f'<div class="event-time-grid">{cells}</div>'
        '</section>'
    )


def public_verification_markup(
    state: str,
    verification: dict[str, object],
    evidence: list[dict[str, object]],
) -> str:
    """Explain a light-verification record without implying every record passed."""
    score = verification.get("score")
    score_copy = f"评分 {escape(str(score))}" if score not in (None, "") else "未记录评分"
    state_copy = {
        "verified": "本轮引用材料通过轻量细核，当前状态为“已核验”。",
        "insufficient": "本轮轻量细核没有形成已核验结论，当前仍为“证据不足”。",
        "excluded": "本轮记录不改变已排除状态。",
        "rough_reviewed": "本轮记录不替代后续正式核验。",
        "pending_verification": "本轮记录不等于正式核验完成。",
    }.get(state, "本轮记录仅说明曾进行限定检查。")
    raw_ids = verification.get("evidence_ids") or []
    known_ids = {
        str(item.get("evidence_id") or "").strip()
        for item in evidence
        if str(item.get("evidence_id") or "").strip()
    }
    evidence_ids = []
    for value in raw_ids if isinstance(raw_ids, list) else []:
        normalized = " ".join(str(value or "").split())[:96]
        if normalized and normalized not in evidence_ids:
            evidence_ids.append(normalized)
    chips = "".join(
        '<code class="evidence-id-chip{}">{}</code>'.format(
            " is-linked" if item in known_ids else "", escape(item)
        )
        for item in evidence_ids[:8]
    )
    remaining = len(evidence_ids) - min(len(evidence_ids), 8)
    ids_copy = (
        f'<div class="verification-evidence-ids"><span>本轮引用证据 ID</span>{chips}'
        f'{f"<em>另有 {remaining} 条</em>" if remaining else ""}'
        '<small>这些是本次限定核验实际引用的材料，不等于全部关联证据。</small></div>'
        if evidence_ids
        else '<div class="verification-evidence-ids"><span>本轮引用证据 ID 未记录</span>'
        '<small>因此不能把这条核验记录理解为完整证据链。</small></div>'
    )
    return (
        '<div class="verification-method" role="note">'
        '<strong>核验记录 · 轻量细核</strong>'
        f'<span>{state_copy} {score_copy}；模型不负责改写正式结论。</span>'
        f'{ids_copy}'
        '</div>'
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
    "事件记录、原始证据与核验进度" if UI_ROLE != "admin" else "事件流、复核队列与运行态总览",
)
no_trading_banner()
overview_loading = st.empty()
overview_loading.caption("正在读取采集状态与核验概览…")
try:
    overview, overview_cache = cached_api_get(
        "/api/v1/overview",
        ttl_seconds=15,
        stale_if_error_seconds=120,
        # Large migrated ledgers can need several seconds for the first public
        # funnel projection.  A five-second client cutoff produced a false
        # outage even while /overview was healthy and completed at ~9 seconds.
        timeout_seconds=15,
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
        "数据更新已中断：最近一次成功采集为 "
        f"{format_elapsed(public_worker_age)} 前。以下内容是历史事件记录，不是实时信息。"
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
public_state = query_choice(st.query_params.get("preview_state"), PUBLIC_STATES)
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
        "preview_state": public_state,
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
public_funnel = overview.get("reader_funnel") or canonical_public_funnel
reader_hidden_inventory = max(
    0,
    int(
        overview.get("reader_hidden_inventory", overview.get("discovery_backlog"))
        or 0
    ),
)
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

review_queue = int(
    (
        overview.get("review_queue")
        if UI_ROLE == "admin"
        else overview.get("reader_review_queue", overview.get("review_queue"))
    )
    or 0
)
if review_queue:
    brief_copy = f"有 {review_queue:,} 条可读事件仍在等待证据或规则核验。"
    brief_copy += (
        "先在当前页预览证据与下一步行动，需要完整工具时再进入人工复核。"
        if UI_ROLE == "admin"
        else "你可以先看原始证据与上下文，不必把未闭合的信息当成确定事实。"
    )
else:
    brief_copy = (
        "当前没有等待复核的事件。可以浏览最新事件流，或按需展开运行健康与来源状态。"
        if UI_ROLE == "admin"
        else "当前没有满足公共阅读门槛、同时仍等待核验的事件。"
    )
if UI_ROLE != "admin" and reader_hidden_inventory:
    brief_copy += (
        f"另有 {reader_hidden_inventory:,} 条历史或发现记录尚未达到公开可读标准，"
        "已与当前事件流分开。"
    )
situation_brief(
    "先看需要判断的事件" if UI_ROLE == "admin" else "先看证据是否足够",
    brief_copy,
    focus_label="当前优先级",
    focus_value=(
        f"{review_queue:,} {'待复核' if UI_ROLE == 'admin' else '待核验'}"
        if review_queue
        else "队列已清"
    ),
    focus_state="watch" if review_queue else "ok",
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
        ("需要关注", {"preview_state": "pending_verification", "preview_sort": "latest"}),
        ("继续跟进", {"preview_state": "rough_reviewed", "preview_sort": "latest"}),
    )
    for column, (label, updates) in zip(workspace_cols, workspace_actions, strict=True):
        if column.button(label, width="stretch", key=f"public-workspace-{label}"):
            for key in (
                "preview_state",
                "preview_period",
                "preview_event_id",
                "preview_page",
            ):
                st.query_params.pop(key, None)
            for key, value in updates.items():
                st.query_params[key] = value
            st.rerun()
    st.caption("三个入口分别回答：今天发生了什么、哪些事实仍待核验、哪些粗审线索需要继续补证。")
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
        public_state,
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
    formally_verified = max(0, int(public_funnel.get("verified") or 0))
    formally_excluded = max(0, int(public_funnel.get("excluded") or 0))
    review_needed = sum(
        max(0, int(public_funnel.get(key) or 0))
        for key in ("pending_verification", "rough_reviewed", "insufficient")
    )
    status_items = [
        ("采集状态", collector_label, collector_state),
        ("最近成功采集", format_elapsed(worker_age), collector_state),
        (
            "正式结论",
            f"核验 {formally_verified:,} · 排除 {formally_excluded:,}",
            "ok" if not review_needed else "watch",
        ),
        ("待补证 / 复核", f"{review_needed:,} 条", "watch" if review_needed else "ok"),
    ]
status_strip(status_items)
if UI_ROLE != "admin":
    st.markdown(
        '<p class="public-health-explainer">'
        f'采集与核验是两条独立时间线（最近发现新事件：{escape(format_elapsed(event_age))} 前）。'
        '正式结论只统计已核验和已排除；证据不足、待核验和已粗审仍需要补证或人工复核。'
        '</p>',
        unsafe_allow_html=True,
    )
if UI_ROLE == "admin":
    render_evidence_route(event_status, review_queue)
render_flow_shortcuts(
    event_status,
    public=UI_ROLE != "admin",
    public_funnel=public_funnel if UI_ROLE != "admin" else None,
)
if UI_ROLE != "admin":
    st.markdown(
        '<p class="mobile-scroll-cue" aria-label="筛选提示">左右滑动可查看全部状态筛选 · 向下浏览事件</p>',
        unsafe_allow_html=True,
    )

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
        query_path("/api/v1/events/facets", reader_ready=True),
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
        feed_result = api_request(
            query_path(
                "/api/v1/events",
                public_state=public_state,
                family=public_family,
                source=public_source,
                q=preview_query,
                date_from=date_from,
                reader_ready=True,
                sort=public_sort,
                limit=public_page_size,
                offset=(public_page - 1) * public_page_size,
            )
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
            current_feed_state = {
                "last_updated_at": str(item.get("last_updated_at") or ""),
                "status": public_event_state(item),
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
        preview_detail = api_request(f"/api/v1/events/{preview_event_id}")
        preview_evidence = sorted(
            api_request(f"/api/v1/events/{preview_event_id}/evidence")["items"],
            key=public_evidence_sort_key,
        )
        if UI_ROLE != "admin":
            preview_evidence = [
                item for item in preview_evidence if int(item.get("reader_eligible") or 0) == 1
            ]
        preview_sources: list[dict[str, object]] = []
        preview_interpretations: list[dict[str, object]] = []
        preview_knowledge: dict[str, object] = {}
        preview_sources_error: Exception | None = None
        preview_interpretations_error: Exception | None = None
        try:
            knowledge_response = api_request(
                f"/api/v1/events/{preview_event_id}/knowledge"
            )
            if isinstance(knowledge_response, dict):
                preview_knowledge = knowledge_response
        except Exception:
            # Knowledge is explanatory and must not hide the underlying event.
            preview_knowledge = {}
        try:
            source_response = api_request(
                f"/api/v1/events/{preview_event_id}/sources"
            )
            source_items = source_response.get("items", [])
            if isinstance(source_items, list):
                preview_sources = [
                    item for item in source_items if isinstance(item, dict)
                ]
        except Exception as exc:
            # Discovery history is explanatory and must never turn an
            # otherwise readable event detail into a total page failure.
            preview_sources_error = exc
        if preview_sources:
            try:
                interpretation_response = api_request(
                    f"/api/v1/events/{preview_event_id}/source-interpretations"
                )
                interpretation_items = interpretation_response.get("items", [])
                if isinstance(interpretation_items, list):
                    preview_interpretations = [
                        item for item in interpretation_items if isinstance(item, dict)
                    ]
            except Exception as exc:
                preview_interpretations_error = exc
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
        public_verification = preview_detail.get("verification_method")
        if not isinstance(public_verification, dict):
            public_verification = None
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
                reviewed_at = str(
                    (public_verification or {}).get("reviewed_at")
                    or preview_event.get("reviewed_at")
                    or ""
                )
                review_copy = {
                    "verified": (
                        "正式核验已完成"
                        if has_recorded_public_verification(public_verification)
                        else "历史已核验记录 · 核验时间与核验留痕未存档"
                    ),
                    "excluded": "线索已排除",
                    "insufficient": "证据不足，不形成结论",
                    "rough_reviewed": "粗审已完成，尚未正式核验",
                    "pending_verification": "尚未完成正式核验",
                }[public_copy["state"]]
                if reviewed_at and public_copy["state"] == "rough_reviewed":
                    review_copy += f" · {escape(reviewed_at[:10])}"
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
                    '<article><span>当前状态</span>'
                    f'<p><strong>{escape(public_copy["state_label"])}</strong> · {review_copy}</p></article>'
                    '<article><span>证据情况</span>'
                    f'<p>{len(preview_evidence):,} 条可引用支持证据 · {escape(evidence_authority)}'
                    + (
                        f' · {len(preview_sources):,} 条采集来源记录'
                        if preview_sources
                        else ''
                    )
                    + '</p></article>'
                    '</div>'
                    '</section>',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    public_time_markup(preview_event, preview_detail, public_verification),
                    unsafe_allow_html=True,
                )
                if previous_snapshot is None:
                    st.caption("本次浏览会话首次查看；系统已记住当前版本，后续变化会在这里说明。")
                elif changes_since_view:
                    st.markdown("**自上次查看后的变化**")
                    for change in changes_since_view:
                        st.markdown(f"- {escape(change)}")
                else:
                    st.caption("与本次浏览会话中的上次查看相比，状态、版本和证据集合没有变化。")
            if UI_ROLE != "admin" and public_verification:
                st.markdown(
                    public_verification_markup(
                        public_copy["state"], public_verification, preview_evidence
                    ),
                    unsafe_allow_html=True,
                )
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
            if preview_sources:
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
                receipt = str(source.get("capture_receipt_sha256") or "")
                interpretation = next(
                    (
                        item
                        for item in preview_interpretations
                        if str(item.get("capture_receipt_sha256") or "") == receipt
                    ),
                    preview_interpretations[0] if preview_interpretations else None,
                )
                with st.expander(
                    "查看采集到的原始线索与内容解读（未核验、非证据）",
                    expanded=not bool(preview_evidence),
                ):
                    st.markdown(
                        '<div class="preview-evidence raw-evidence discovery-capture">'
                        '<span>API 发现载荷 · 不参与正式结论</span>'
                        f'<h3>{escape(source_title)}</h3>'
                        f'<p>{escape(source_excerpt)}</p>'
                        '<small>该记录仅说明系统当时收到了什么；它不是 P0/P1 权威证据，'
                        '也不会改变已排除状态或触发交易。'
                        + (
                            f' 当前仅展示前 {int(source.get("source_excerpt_original_length") or 0):,} 字中的限长节选。'
                            if source.get("source_excerpt_truncated")
                            else ''
                        )
                        + '</small>'
                        '</div>',
                        unsafe_allow_html=True,
                    )
                    if interpretation:
                        one_line = escape(
                            str(
                                interpretation.get("one_line_zh")
                                or "当前没有可展示的解读。"
                            )
                        )
                        mode = str(interpretation.get("mode") or "DETERMINISTIC")
                        status = str(interpretation.get("status") or "PARTIAL")
                        label = (
                            "AI 辅助解读 · 未核验 · 非证据"
                            if mode == "LLM_ASSISTED"
                            else "确定性预览 · 外部模型待接入"
                        )
                        claim_items = interpretation.get("what_source_says") or []
                        claim_markup = "".join(
                            '<li><span>'
                            + escape(str(item.get("text_zh") or ""))
                            + '</span><small>原文：'
                            + escape(str(item.get("quote") or ""))
                            + '</small></li>'
                            for item in claim_items[:4]
                            if isinstance(item, dict)
                        )
                        missing_items = interpretation.get("missing_to_change_state_zh") or []
                        missing_markup = "".join(
                            f'<li>{escape(str(item))}</li>' for item in missing_items[:3]
                        )
                        not_proven_items = (
                            interpretation.get("what_source_does_not_prove_zh") or []
                        )
                        not_proven_markup = "".join(
                            f'<li>{escape(str(item))}</li>' for item in not_proven_items[:3]
                        )
                        assets = ", ".join(
                            escape(str(item))
                            for item in (interpretation.get("affected_assets") or [])[:8]
                        )
                        st.markdown(
                            '<section class="capture-interpretation" aria-label="API发现内容解读">'
                            '<div class="capture-interpretation-head">'
                            f'<span>{escape(label)}</span>'
                            f'<small>{escape(status)} · {escape(str(interpretation.get("coverage") or ""))}</small>'
                            '</div>'
                            '<h4>一句话看懂</h4>'
                            f'<p class="capture-interpretation-summary">{one_line}</p>'
                            + (
                                '<h4>原文明确表达</h4><ul class="capture-claim-list">'
                                + claim_markup
                                + '</ul>'
                                if claim_markup
                                else ''
                            )
                            + (
                                f'<p class="capture-assets"><strong>受影响资产：</strong>{assets}</p>'
                                if assets
                                else ''
                            )
                            + '<div class="capture-interpretation-grid">'
                            '<article><h4>仅凭这段未核验来源仍不能确认</h4><ul>'
                            + not_proven_markup
                            + '</ul></article>'
                            '<article><h4>要改变当前结论，还需要</h4><ul>'
                            + missing_markup
                            + '</ul></article>'
                            '</div>'
                            f'<p class="capture-disposition">{escape(str(interpretation.get("why_current_state_zh") or ""))}</p>'
                            '<small class="capture-boundary">解释只绑定当前捕获版本；来源修订后自动失效。'
                            '它不进入正式事实、风险路由、价格判断或交易流程。</small>'
                            '</section>',
                            unsafe_allow_html=True,
                        )
                    elif preview_interpretations_error:
                        st.warning(
                            "内容解读暂时不可用；原始捕获仍可阅读，事件正式状态没有改变。"
                        )
            elif preview_sources_error:
                st.warning(
                    "采集来源记录暂时无法读取；这不表示原始输入为空，也不改变事件状态。"
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
    active_state_label = PUBLIC_STATE_LABELS.get(public_state, "全部状态")
    st.markdown(
        '<section class="queue-card queue-card-horizontal" aria-label="可核验事件队列">'
        '<div>'
        '<div class="queue-card-label">可核验事件队列</div>'
        f'<div class="queue-card-value">{review_queue:,}</div>'
        '</div>'
        '<div class="queue-card-copy">'
        '这里只统计已有明确主体、结构化事实摘要和可引用原文，但仍需补证或规则处理的记录。'
        f'<div>另有 {reader_hidden_inventory:,} 条历史或发现记录未达到公开可读标准。</div>'
        '<div class="queue-card-next">下一步 · 打开事件，先读中文结论，再核对原始证据</div>'
        '</div>'
        '</section>',
        unsafe_allow_html=True,
    )
    st.markdown('<div id="live-events"></div>', unsafe_allow_html=True)
    feed_context = active_state_label
    if preview_query:
        feed_context += f" · 搜索 {preview_query}"
    section_header(
        "事件浏览",
        f"本页 {len(live_feed):,} 条 · 共 {live_total:,} 条 · 第 {public_page}/{total_pages} 页 · UTC",
    )
    if live_feed:
        link_context = {
            "preview_state": public_state,
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
            "preview_state": public_state,
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
    else "事件状态互斥且总和等于全部事件 · 时间统一为 UTC · 原始证据可能保留发布语言"
)
