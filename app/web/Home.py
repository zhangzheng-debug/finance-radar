from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from math import ceil
from urllib.parse import quote, urlencode

import streamlit as st

from app.source_url_policy import (
    preferred_public_source_url,
    public_source_url as safe_public_source_url,
)
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
    PUBLIC_SOURCE_LABELS,
    facet_counts,
    facet_values,
    event_anchor_id,
    focus_event_preview,
    focus_public_event_feed,
    next_action_guidance,
    public_event_copy,
    public_event_risk_assessment,
    public_event_source_provenance,
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
CAPTURE_EXPLANATION_CACHE_STATE_KEY = "public_capture_explanation_v1"
CAPTURE_EXPLANATION_MAX_POLLS = 15


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


def public_source_url(evidence: list[dict[str, object]]) -> str | None:
    """Return an explicitly safe public evidence link, if one is available."""
    if not evidence:
        return None
    value = str(evidence[0].get("evidence_url") or "").strip()
    if len(value) > 2048:
        return None
    return safe_public_source_url(value)


def public_capture_url(sources: list[dict[str, object]]) -> str | None:
    """Return a validated discovery-source link without treating it as evidence."""
    return preferred_public_source_url(sources)


def render_capture_explanation_payload(payload: dict[str, object]) -> None:
    """Render completed AI assistance without duplicating the source text."""

    if not payload.get("display"):
        return

    state = str(payload.get("state") or "CHECKING")
    interpretation = payload.get("item")
    interpretation = interpretation if isinstance(interpretation, dict) else None
    if state == "READY" and interpretation:
        st.markdown(
            '<section class="capture-ai-result" aria-label="AI 解读">'
            '<div class="capture-ai-boundary-head">'
            '<span>AI 阅读辅助</span>'
            f'<strong>{escape(str(interpretation.get("boundary_zh") or "AI仅解释来源文本，不参与事件评级或价格判断。"))}</strong>'
            '</div>'
            f'<p>{escape(str(interpretation.get("one_line_zh") or ""))}</p>'
            '</section>',
            unsafe_allow_html=True,
        )
        claims = interpretation.get("what_source_says") or []
        if claims:
            st.markdown("**要点**")
            for claim in claims[:2]:
                if not isinstance(claim, dict):
                    continue
                st.markdown(f"- {escape(str(claim.get('text_zh') or ''))}")


def public_research_signal_markup(
    detail: dict[str, object], public_copy: dict[str, object]
) -> str:
    """Render the stable Qwen slot and available market observations compactly."""

    role_order = {
        "DIRECT_ASSET": 1,
        "DIRECT_SECURITY": 1,
        "US_LISTED_PROXY": 2,
        "MARKET_BENCHMARK": 3,
        "SECTOR_PROXY": 4,
        "THEMATIC_PROXY": 5,
    }

    reaction = detail.get("market_reaction")
    reaction = reaction if isinstance(reaction, dict) else {}
    items = reaction.get("items")
    items = items if isinstance(items, list) else []
    normalized: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            value = float(item.get("return_pct"))
        except (TypeError, ValueError):
            continue
        if value != value or value in {float("inf"), float("-inf")}:
            continue
        label = " ".join(str(item.get("label") or "").split())
        symbol = " ".join(str(item.get("symbol") or "").split())
        if not label or not symbol:
            continue
        window = " ".join(str(item.get("window") or "").split())
        if not window:
            continue
        normalized.append(
            {
                "window": window,
                "label": label,
                "symbol": symbol,
                "return_pct": value,
                "role_label": " ".join(str(item.get("role_label") or "").split()),
                "proxy_label": " ".join(str(item.get("proxy_label") or "").split()),
                "role": " ".join(str(item.get("role") or "").split()),
            }
        )
    context = detail.get("market_context")
    context = context if isinstance(context, dict) else {}
    context_items = context.get("items")
    context_items = context_items if isinstance(context_items, list) else []
    prices: dict[str, dict[str, object]] = {}
    for item in context_items:
        if not isinstance(item, dict):
            continue
        symbol = " ".join(str(item.get("symbol") or "").split())
        try:
            price = float(item.get("price"))
        except (TypeError, ValueError):
            continue
        if not symbol or price <= 0 or price != price or price in {float("inf"), float("-inf")}:
            continue
        prices[symbol] = {
            "price": price,
            "currency": " ".join(str(item.get("currency") or "").split()),
            "observed_at": " ".join(str(item.get("observed_at") or "").split()),
            "role_label": " ".join(str(item.get("role_label") or "").split()),
            "proxy_label": " ".join(str(item.get("proxy_label") or "").split()),
            "role": " ".join(str(item.get("role") or "").split()),
        }

    signal_rows: list[str] = []
    qwen_ready = bool(public_copy.get("risk_route") and public_copy.get("risk_label"))
    basis = " ".join(str(public_copy.get("risk_basis_label") or "").split())
    qwen_values = (
        (
            str(public_copy.get("risk_polarity_label") or "—"),
            str(public_copy.get("risk_materiality_label") or "—"),
            str(public_copy.get("risk_strength_label") or "—"),
        )
        if qwen_ready
        else ("—", "—", "—")
    )
    qwen_metrics = "".join(
        '<span class="qwen-signal-metric">'
        f'<small>{escape(label)}</small><strong>{escape(value)}</strong></span>'
        for label, value in zip(("方向", "做空重大性", "强度"), qwen_values, strict=True)
    )
    signal_rows.append(
        '<div class="research-signal-row qwen-signal-row"'
        + (f' title="{escape(basis)}"' if basis else "")
        + '><span>千问风险研判</span><div class="qwen-signal-metrics">'
        + qwen_metrics
        + '</div><small class="qwen-slot-state">'
        + ("自动研判" if qwen_ready else "模型接口已预留")
        + "</small></div>"
    )

    if normalized:
        # Select one shared window so values are comparable. Coverage wins;
        # among equally covered windows the public research order is 30m, 2h,
        # 1d, then the remaining available horizons.
        preferred = (
            "t_plus_30m",
            "t_plus_2h",
            "t_plus_1d",
            "t_plus_5m",
            "next_close",
            "t_plus_5d",
        )
        by_window: dict[str, list[dict[str, object]]] = {}
        for item in normalized:
            by_window.setdefault(str(item["window"]), []).append(item)
        selected_window = max(
            by_window,
            key=lambda window: (
                len(by_window[window]),
                -preferred.index(window) if window in preferred else -len(preferred),
            ),
        )
        selected = sorted(
            by_window[selected_window],
            key=lambda item: (
                role_order.get(str(item.get("role") or ""), 99),
                str(item["symbol"]),
            ),
        )[:3]
        window_label = str(selected[0]["label"])
        values: list[str] = []
        for item in selected:
            value = float(item["return_pct"])
            tone = "positive" if value > 0 else "negative" if value < 0 else "flat"
            symbol = str(item["symbol"])
            proxy_label = str(item.get("proxy_label") or "")
            role = proxy_label or str(item.get("role_label") or "")
            role_markup = f'<small>{escape(role)}</small>' if role else ""
            values.append(
                '<span class="market-reaction-inline-item">'
                f'{escape(symbol)} {role_markup}<strong class="{tone}">{value:+.2f}%</strong>'
                '</span>'
            )
        signal_rows.append(
            '<div class="research-signal-row market-signal-row">'
            f'<span>消息发布后（{escape(window_label)}）</span>'
            '<div class="market-reaction-inline">'
            + "".join(values)
            + '</div></div>'
        )
    if prices:
        values: list[str] = []
        for symbol, item in sorted(
            prices.items(),
            key=lambda pair: (
                role_order.get(str(pair[1].get("role") or ""), 99),
                pair[0],
            ),
        )[:3]:
            role = str(item.get("proxy_label") or item.get("role_label") or "")
            role_markup = f'<small>{escape(role)}</small>' if role else ""
            currency = str(item.get("currency") or "")
            price = float(item["price"])
            precision = 2 if price >= 1 else 4
            values.append(
                '<span class="market-reaction-inline-item">'
                f'{escape(symbol)} {role_markup}<strong class="flat">{price:,.{precision}f}'
                f'{(" " + escape(currency)) if currency else ""}</strong></span>'
            )
        observed = max(str(item.get("observed_at") or "") for item in prices.values())
        signal_rows.append(
            '<div class="research-signal-row market-signal-row"'
            + (f' title="价格时间 {escape(observed)}；非实时行情"' if observed else "")
            + '><span>价格截面</span><div class="market-reaction-inline">'
            + "".join(values)
            + "</div></div>"
        )

    if not signal_rows:
        return ""
    return (
        '<section class="market-reaction research-signals" aria-label="研究信号">'
        '<div class="market-reaction-head"><span>研究信号</span></div>'
        + "".join(signal_rows)
        + '</section>'
    )


@st.fragment(run_every="2s")
def render_capture_explanation_fragment(
    event_path_id: str,
    event_id: str,
    event_version: int,
    initial_payload: dict[str, object],
) -> None:
    """Poll the cache-only endpoint without inventing a local queue state."""

    cache = st.session_state.get(CAPTURE_EXPLANATION_CACHE_STATE_KEY, {})
    cache = cache if isinstance(cache, dict) else {}
    previous = cache.get(event_id)
    previous = previous if isinstance(previous, dict) else {}
    previous_payload = previous.get("payload")
    previous_payload = (
        previous_payload if isinstance(previous_payload, dict) else {}
    )
    if (
        int(previous.get("version") or -1) == event_version
        and str(previous_payload.get("state") or "")
        in {
            "READY",
            "FAILED_TERMINAL",
            "SUPERSEDED",
            "NOT_APPLICABLE",
            "NO_CAPTURE_TEXT",
            "REFETCH_PRIMARY_SOURCE",
            "CLIENT_POLL_PAUSED",
        }
    ):
        render_capture_explanation_payload(previous_payload)
        return
    try:
        payload = api_request(
            f"/api/v1/events/{event_path_id}/capture-explanation",
            timeout_seconds=3,
        )
    except Exception:
        payload = dict(initial_payload)
        payload["display"] = bool(initial_payload.get("display"))
        payload["state"] = "STATUS_UNAVAILABLE"
        payload.setdefault("item", None)
    polls = int(previous.get("polls") or 0) + 1
    state = str(payload.get("state") or "")
    terminal = state in {
        "READY",
        "FAILED_TERMINAL",
        "SUPERSEDED",
        "NOT_APPLICABLE",
        "NO_CAPTURE_TEXT",
        "REFETCH_PRIMARY_SOURCE",
    }
    if terminal or polls >= CAPTURE_EXPLANATION_MAX_POLLS:
        if not terminal:
            payload = dict(payload)
            payload["state"] = "CLIENT_POLL_PAUSED"
        cache[event_id] = {
            "version": event_version,
            "polls": polls,
            "payload": payload,
        }
        st.session_state[CAPTURE_EXPLANATION_CACHE_STATE_KEY] = cache
    else:
        cache[event_id] = {
            "version": event_version,
            "polls": polls,
            "payload": payload,
        }
        st.session_state[CAPTURE_EXPLANATION_CACHE_STATE_KEY] = cache
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
    source = public_event_source_provenance(semantic_input)
    citation_ready = source["key"] == "CLAIM_SOURCE_LINKED"
    risk = public_event_risk_assessment(semantic_input)
    legacy_state = public_event_state(event)
    return {
        "last_updated_at": str(event.get("last_updated_at") or ""),
        "source_access": source["key"],
        "source_label": source["label"],
        "citation_ready": citation_ready,
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
    if (
        previous.get("source_access") is not None
        and previous.get("source_access") != current.get("source_access")
    ):
        changes.append(
            "来源状态："
            f"{previous.get('source_label') or '未标注'} → "
            f"{current.get('source_label') or '未标注'}"
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
        changes.append("事件记录的最后更新时间发生变化；来源状态与关联材料未变。")
    return changes


def public_time_value(value: object, *, date_only: bool = False) -> str:
    """Format a declared timestamp while preserving the difference between date and time."""
    normalized = " ".join(str(value or "").split())
    if not normalized:
        return ""
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
    """Render only the source and data clocks that help a public reader."""
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
        ("来源发布", public_time_value(published_at)),
        ("数据更新", public_time_value(event.get("last_updated_at"))),
    )
    values = tuple((label, value) for label, value in values if value)
    if not values:
        return ""
    cells = "".join(
        '<div class="event-time-cell"><span>{}</span><strong>{}</strong></div>'.format(
            escape(label), escape(value)
        )
        for label, value in values
    )
    return (
        '<section class="public-source-timing" aria-label="来源与数据时间">'
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

if UI_ROLE == "admin":
    header("态势总览", "事件流、复核队列与运行态总览")
    no_trading_banner()
else:
    st.markdown(
        '<header class="public-reader-header">'
        '<div><span>FINANCE RADAR</span><h1>风险雷达</h1></div>'
        '<p>自动发现潜在下行事件，连接来源材料与市场反应。</p>'
        '</header>',
        unsafe_allow_html=True,
    )
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
if public_worker_stale:
    st.markdown(
        '<div class="public-data-alert" role="status">'
        "数据更新中断 · 最近一次完整处理为 "
        f"{escape(format_elapsed(public_worker_age))} 前。"
        "</div>",
        unsafe_allow_html=True,
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
    int(
        counts.get("public_visible_events")
        if counts.get("public_visible_events") is not None
        else canonical_public_funnel.get("total") or counts.get("canonical_events") or 0
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

review_queue = int(overview.get("review_queue") or 0)
if UI_ROLE == "admin" and review_queue:
    brief_copy = f"有 {review_queue:,} 条可读事件仍在等待证据或规则核验。"
    brief_copy += "先在当前页预览证据与下一步行动，需要完整工具时再进入人工复核。"
elif UI_ROLE == "admin":
    brief_copy = "当前没有等待复核的事件。可以浏览最新事件流，或按需展开运行健康与来源状态。"
if UI_ROLE == "admin":
    situation_brief(
        "先看需要判断的事件",
        brief_copy,
        focus_label="当前优先级",
        focus_value=f"{review_queue:,} 待复核" if review_queue else "队列已清",
        focus_state="watch" if review_queue else "ok",
    )
else:
    st.markdown(
        '<div class="public-reader-summary">'
        f'<strong>{canonical_inventory_count:,}</strong><span>个事件</span>'
        f'<span>最近更新 {escape(format_elapsed(event_age))} 前</span>'
        '</div>',
        unsafe_allow_html=True,
    )
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
if UI_ROLE == "admin":
    status_strip(status_items)
if UI_ROLE == "admin":
    render_evidence_route(event_status, review_queue)
    render_flow_shortcuts(event_status, public=False)

facets: dict[str, object] = {"families": [], "sources": []}
feed_loading = st.empty()
if UI_ROLE != "admin":
    feed_loading.markdown(
        '<section class="fr-loading-state" role="status" aria-live="polite">'
        '<strong class="fr-state-title">加载事件…</strong>'
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
            placeholder="搜索公司、Ticker 或事件",
            value=preview_query,
            label_visibility="collapsed",
        )
        search_submitted = submit_col.form_submit_button("搜索", width="stretch")
        advanced_filters_active = bool(
            public_family
            or public_source
            or public_period != "全部时间"
            or public_sort != "latest"
            or public_page_size != 24
        )
        with st.expander("筛选", expanded=advanced_filters_active):
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
            feed_source = public_event_source_provenance(item)
            feed_risk = public_event_risk_assessment(item)
            legacy_state = public_event_state(item)
            current_feed_state = {
                "last_updated_at": str(item.get("last_updated_at") or ""),
                "source_access": feed_source["key"],
                "source_label": feed_source["label"],
                "citation_ready": feed_source["key"] == "CLAIM_SOURCE_LINKED",
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
                explanation_state = dossier.get("capture_explanation") or {}
                if isinstance(explanation_state, dict):
                    preview_capture_explanation = explanation_state
                for source_field in ("preferred_source", "source_link"):
                    source_capture = preview_detail.get(source_field) or {}
                    if (
                        isinstance(source_capture, dict)
                        and (
                            source_capture.get("source_title")
                            or source_capture.get("source_excerpt")
                            or source_capture.get("source_url")
                        )
                        and source_capture not in preview_sources
                    ):
                        preview_sources.append(source_capture)
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
            if UI_ROLE == "admin":
                section_header(
                    "当前页事件预览",
                    "留在态势总览 · 需要时再进入人工复核",
                )
            else:
                section_header("事件详情", "")
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
                meta_values = []
                event_date = str(preview_event.get("event_date") or "").strip()
                ticker = str(preview_event.get("ticker_at_event") or "").strip()
                if event_date:
                    meta_values.append(event_date[:10])
                if preview_company:
                    meta_values.append(str(preview_company))
                if ticker and ticker != preview_company:
                    meta_values.append(ticker)
                meta_values.append(str(public_copy["family"]))
                if public_copy["source_label"]:
                    meta_values.append(str(public_copy["source_label"]))
                if public_copy["headline_mode"] == "ATTRIBUTED_SOURCE":
                    headline_source = str(public_copy["headline_source"] or public_copy["source"])
                    if headline_source:
                        meta_values.append(f"来源：{headline_source}")
                meta_markup = "".join(
                    f"<span>{escape(value)}</span>" for value in meta_values if value
                )
                top_public_passage = ""
                top_public_title = ""
                if preview_evidence:
                    top_public_passage = " ".join(
                        str(
                            preview_evidence[0].get("evidence_passage")
                            or preview_evidence[0].get("observation_summary")
                            or ""
                        ).split()
                    )
                    if len(top_public_passage) > 900:
                        top_public_passage = top_public_passage[:897].rstrip() + "…"
                elif preview_sources:
                    top_public_title = " ".join(
                        str(preview_sources[0].get("source_title") or "").split()
                    )
                    top_public_passage = " ".join(
                        str(
                            preview_sources[0].get("source_excerpt")
                            or top_public_title
                            or ""
                        ).split()
                    )
                    if len(top_public_passage) > 900:
                        top_public_passage = top_public_passage[:897].rstrip() + "…"
                summary_text = " ".join(str(public_copy["summary"] or "").split())
                comparison_summary = summary_text.casefold().strip(" .。…")
                comparison_passage = top_public_passage.casefold().strip(" .。…")
                summary_duplicates_source = bool(
                    comparison_summary
                    and comparison_passage
                    and (
                        comparison_summary == comparison_passage
                        or comparison_summary in comparison_passage
                        or comparison_passage in comparison_summary
                    )
                )
                summary_article = (
                    '<article class="event-answer-summary"><span>事件摘要</span>'
                    f'<p>{escape(summary_text)}</p></article>'
                    if summary_text and not summary_duplicates_source
                    else ""
                )
                source_heading = "关键原文" if preview_evidence else "来源文本"
                source_title_markup = (
                    f'<h3>{escape(top_public_title)}</h3>'
                    if (
                        top_public_title
                        and top_public_title != top_public_passage
                        and top_public_title != str(public_copy["headline"])
                    )
                    else ""
                )
                source_article = (
                    f'<article class="event-source-passage"><span>{source_heading}</span>'
                    f'{source_title_markup}'
                    f'<p>{escape(top_public_passage)}</p></article>'
                    if top_public_passage
                    else ""
                )
                st.markdown(
                    '<section class="event-answer" aria-label="事件阅读摘要">'
                    f'<div class="event-answer-meta">{meta_markup}</div>'
                    f'<h2>{escape(str(public_copy["headline"]))}</h2>'
                    '<div class="event-answer-grid">'
                    f'{summary_article}'
                    f'{source_article}'
                    '</div>'
                    '</section>',
                    unsafe_allow_html=True,
                )
                st.markdown(
                    public_time_markup(preview_event, preview_detail),
                    unsafe_allow_html=True,
                )
                research_signal_markup = public_research_signal_markup(
                    preview_detail, public_copy
                )
                if research_signal_markup:
                    st.markdown(research_signal_markup, unsafe_allow_html=True)
                if previous_snapshot is not None and changes_since_view:
                    st.markdown("**自上次查看后的变化**")
                    for change in changes_since_view:
                        st.markdown(f"- {escape(change)}")
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
                event_version = int(preview_event.get("current_version") or 0)
                explanation_cache = st.session_state.get(
                    CAPTURE_EXPLANATION_CACHE_STATE_KEY,
                    {},
                )
                explanation_cache = (
                    explanation_cache if isinstance(explanation_cache, dict) else {}
                )
                cached_explanation = explanation_cache.get(preview_event_id)
                cached_explanation = (
                    cached_explanation if isinstance(cached_explanation, dict) else {}
                )
                if int(cached_explanation.get("version") or -1) == event_version:
                    cached_payload = cached_explanation.get("payload")
                    cached_payload = (
                        cached_payload if isinstance(cached_payload, dict) else {}
                    )
                else:
                    cached_payload = {}
                if str(cached_payload.get("state") or "") in {
                    "READY",
                    "FAILED_TERMINAL",
                    "SUPERSEDED",
                    "NOT_APPLICABLE",
                    "NO_CAPTURE_TEXT",
                    "REFETCH_PRIMARY_SOURCE",
                    "CLIENT_POLL_PAUSED",
                }:
                    render_capture_explanation_payload(cached_payload)
                else:
                    render_capture_explanation_fragment(
                        preview_event_path_id,
                        preview_event_id,
                        event_version,
                        preview_capture_explanation,
                    )
            if UI_ROLE == "admin":
                render_next_action_prompt(
                    next_action_guidance(preview_event, preview_evidence, preview_model)
                )
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
                        "查看原始来源",
                        source_url,
                        width="stretch",
                    )
                elif capture_url:
                    method_col.link_button(
                        "查看原始来源",
                        capture_url,
                        width="stretch",
                    )
                else:
                    method_col.empty()
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
    section_header("事件", "读取失败")
    render_api_error(feed_error)
elif UI_ROLE != "admin":
    total_pages = max(1, ceil(live_total / public_page_size))
    if live_total and public_page > total_pages:
        st.query_params["preview_page"] = str(total_pages)
        st.query_params.pop("preview_event_id", None)
        st.rerun()

    def public_page_url(page_number: int) -> str:
        params = {
            "preview_query": preview_query,
            "preview_family": public_family,
            "preview_source": public_source,
            "preview_period": public_period if public_period != "全部时间" else "",
            "preview_sort": public_sort,
            "preview_page_size": public_page_size,
            "preview_page": page_number if page_number > 1 else "",
            "preview_focus": "feed",
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
    pagination_markup = (
        '<div class="fr-pagination {placement}" role="group" aria-label="事件分页">'
        f'{previous_link}'
        f'<span class="fr-pagination-status">第 {public_page} / {total_pages} 页</span>'
        f'{next_link}'
        '<a href="./#live-events" target="_self">重置筛选</a>'
        '</div>'
    )
    st.markdown('<div id="live-events"></div>', unsafe_allow_html=True)
    if str(st.query_params.get("preview_focus") or "") == "feed":
        focus_public_event_feed(
            f"{public_page}:{preview_query}:{public_family}:{public_source}:{public_sort}"
        )
    st.markdown(
        f'<div class="public-feed-heading"><strong>事件</strong>'
        f'<span>{live_total:,} 条 · UTC</span></div>',
        unsafe_allow_html=True,
    )
    st.markdown(pagination_markup.format(placement="fr-pagination-top"), unsafe_allow_html=True)
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
            '<strong>当前筛选无结果</strong>'
            '<a href="./#live-events" target="_self">重置筛选</a>'
            '</section>',
            unsafe_allow_html=True,
        )
    st.markdown(pagination_markup.format(placement="fr-pagination-bottom"), unsafe_allow_html=True)
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

st.caption(
    "J/K 在人工复核中切换事件 · / 聚焦检索 · 所有行情只读 · 所有模型输出均为 shadow"
    if UI_ROLE == "admin"
    else "只读事件研究工具 · 来源材料用于事实核对 · 风险信号仅在有效结果存在时显示"
)
