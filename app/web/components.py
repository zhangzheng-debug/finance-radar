from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any
from urllib.parse import quote

import streamlit as st


FLOW_PRESETS: dict[str, dict[str, str]] = {
    "待复核": {"status": "candidate"},
    "已核验": {"status": "verified"},
    "弱证据": {"status": "weak"},
    "全部事件": {"status": ""},
    "已拒绝": {"status": "rejected"},
}

STATUS_LABELS = {
    "verified": "VERIFIED",
    "candidate": "REVIEW",
    "weak": "WEAK",
    "rejected": "REJECTED",
}

EVENT_FAMILY_LABELS = {
    "bankruptcy_or_distress": "破产 / 困境",
    "compensation_dilution": "薪酬 / 稀释",
    "debt_financing": "债务融资",
    "enforcement": "监管执法",
    "geopolitical": "地缘事件",
    "listing_status": "上市状态",
    "macro_policy": "宏观政策",
    "security_incident": "安全事件",
}


EVENT_KEYBOARD_JS = """
export default function(component) {
  const { data } = component;
  const ids = Array.isArray(data?.event_ids) ? data.event_ids : [];
  const selected = String(data?.selected_id || "");
  const searchLabel = String(data?.search_label || "全局检索");
  const handler = (event) => {
    const target = event.target;
    const tag = String(target?.tagName || "").toUpperCase();
    const editing = Boolean(target?.isContentEditable) || ["INPUT", "TEXTAREA", "SELECT"].includes(tag);
    if (editing) return;
    const key = String(event.key || "").toLowerCase();
    if (key === "/") {
      const input = document.querySelector(`input[aria-label="${searchLabel}"]`);
      if (input) { event.preventDefault(); input.focus(); }
      return;
    }
    const delta = (key === "j" || key === "arrowdown") ? 1 :
                  (key === "k" || key === "arrowup") ? -1 : 0;
    if (!delta || !ids.length) return;
    const currentIndex = Math.max(0, ids.indexOf(selected));
    const nextIndex = Math.min(Math.max(currentIndex + delta, 0), ids.length - 1);
    if (nextIndex === currentIndex) return;
    event.preventDefault();
    const nextUrl = new URL(window.location.href);
    nextUrl.searchParams.set("event_id", ids[nextIndex]);
    window.location.assign(nextUrl.toString());
  };
  document.addEventListener("keydown", handler, true);
  return () => document.removeEventListener("keydown", handler, true);
}
""".strip()


_event_keyboard_component = st.components.v2.component(
    "finance_radar_event_keyboard",
    js=EVENT_KEYBOARD_JS,
)


SAVED_FLOW_HTML = """
<div class="saved-flow-manager">
  <div class="saved-flow-entry">
    <span class="saved-flow-kicker">我的信息流</span>
    <label class="sr-only" for="saved-flow-name">自定义信息流名称</label>
    <input id="saved-flow-name" maxlength="24" placeholder="例如：SEC 待复核" />
    <button type="button" data-action="save">保存当前筛选</button>
    <span class="saved-flow-status" aria-live="polite"></span>
  </div>
  <div class="saved-flow-list" role="list" aria-label="本机保存的信息流"></div>
</div>
""".strip()


SAVED_FLOW_CSS = """
.saved-flow-manager { color: #e6eef5; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }
.saved-flow-entry { display: flex; align-items: center; gap: 6px; min-height: 32px; }
.saved-flow-kicker { color: #879caf; font: 700 10px ui-monospace, Consolas, monospace; letter-spacing: .08em; white-space: nowrap; }
.saved-flow-entry input { min-width: 130px; max-width: 220px; height: 30px; padding: 0 8px; color: #e6eef5; background: #0a1420; border: 1px solid #294257; border-radius: 4px; }
.saved-flow-entry button, .saved-flow-link, .saved-flow-delete { min-height: 30px; color: #c9d8e4; background: #0a1420; border: 1px solid #1b2d3d; border-radius: 4px; cursor: pointer; }
.saved-flow-entry button { padding: 0 10px; }
.saved-flow-entry button:hover, .saved-flow-entry button:focus-visible, .saved-flow-link:hover, .saved-flow-link:focus-visible, .saved-flow-delete:hover, .saved-flow-delete:focus-visible { color: #e6eef5; background: #0e1b29; border-color: #29bde3; outline: 2px solid rgba(41,189,227,.25); outline-offset: 1px; }
.saved-flow-status { color: #879caf; font-size: 11px; }
.saved-flow-list { display: flex; align-items: center; gap: 5px; margin-top: 6px; overflow-x: auto; scrollbar-width: thin; }
.saved-flow-item { display: inline-flex; flex: 0 0 auto; }
.saved-flow-link { display: inline-flex; align-items: center; padding: 0 9px; text-decoration: none; border-radius: 4px 0 0 4px; }
.saved-flow-delete { width: 30px; padding: 0; color: #879caf; border-left: 0; border-radius: 0 4px 4px 0; }
.saved-flow-empty { color: #879caf; font-size: 11px; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; border: 0; }
@media (max-width: 620px) {
  .saved-flow-entry { flex-wrap: wrap; }
  .saved-flow-entry input { flex: 1 1 130px; max-width: none; }
  .saved-flow-status { flex-basis: 100%; }
}
""".strip()


SAVED_FLOW_JS = """
export default function(component) {
  const { data, parentElement } = component;
  const storageKey = "finance-radar.saved-flows.v1";
  const maxFlows = 8;
  const manager = parentElement.querySelector(".saved-flow-manager");
  const nameInput = manager.querySelector("#saved-flow-name");
  const saveButton = manager.querySelector('[data-action="save"]');
  const status = manager.querySelector(".saved-flow-status");
  const list = manager.querySelector(".saved-flow-list");

  const cleanText = (value, maxLength) => String(value ?? "").replace(/\\s+/g, " ").trim().slice(0, maxLength);
  const cleanConfig = (value) => {
    const flow = ["待复核", "已核验", "弱证据", "全部事件", "已拒绝"].includes(value?.flow) ? value.flow : "待复核";
    const limit = ["15", "25", "50", "100"].includes(String(value?.limit)) ? String(value.limit) : "25";
    return {
      flow,
      family: cleanText(value?.family, 80),
      source: cleanText(value?.source, 80),
      q: cleanText(value?.q, 120),
      limit,
    };
  };
  const readFlows = () => {
    try {
      const parsed = JSON.parse(localStorage.getItem(storageKey) || "[]");
      if (!Array.isArray(parsed)) return [];
      return parsed.slice(0, maxFlows).map((item) => ({ name: cleanText(item?.name, 24), config: cleanConfig(item?.config) })).filter((item) => item.name);
    } catch (_) { return []; }
  };
  const writeFlows = (flows) => localStorage.setItem(storageKey, JSON.stringify(flows.slice(0, maxFlows)));
  const flowUrl = (config) => {
    const url = new URL(window.location.href);
    url.searchParams.delete("reset");
    url.searchParams.delete("event_id");
    url.searchParams.set("flow", config.flow);
    url.searchParams.set("limit", config.limit);
    config.family ? url.searchParams.set("family", config.family) : url.searchParams.delete("family");
    config.source ? url.searchParams.set("source", config.source) : url.searchParams.delete("source");
    config.q ? url.searchParams.set("q", config.q) : url.searchParams.delete("q");
    return url.toString();
  };
  const render = () => {
    const flows = readFlows();
    list.replaceChildren();
    if (!flows.length) {
      const empty = document.createElement("span");
      empty.className = "saved-flow-empty";
      empty.textContent = "尚未保存 · 筛选仅保存在本机浏览器，不上传服务器";
      list.appendChild(empty);
      return;
    }
    flows.forEach((item, index) => {
      const wrapper = document.createElement("span");
      wrapper.className = "saved-flow-item";
      wrapper.setAttribute("role", "listitem");
      const link = document.createElement("a");
      link.className = "saved-flow-link";
      link.href = flowUrl(item.config);
      link.textContent = item.name;
      link.setAttribute("aria-label", `打开本机信息流 ${item.name}`);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "saved-flow-delete";
      remove.textContent = "×";
      remove.setAttribute("aria-label", `删除本机信息流 ${item.name}`);
      remove.onclick = () => {
        const next = readFlows();
        next.splice(index, 1);
        writeFlows(next);
        status.textContent = `已删除 ${item.name}`;
        render();
      };
      wrapper.append(link, remove);
      list.appendChild(wrapper);
    });
  };
  saveButton.onclick = () => {
    const name = cleanText(nameInput.value, 24);
    if (!name) {
      status.textContent = "请先输入名称";
      nameInput.focus();
      return;
    }
    const flows = readFlows().filter((item) => item.name !== name);
    flows.unshift({ name, config: cleanConfig(data?.current) });
    writeFlows(flows);
    nameInput.value = "";
    status.textContent = `已保存在本机：${name}`;
    render();
  };
  render();
}
""".strip()


_saved_flow_component = st.components.v2.component(
    "finance_radar_saved_flows",
    html=SAVED_FLOW_HTML,
    css=SAVED_FLOW_CSS,
    js=SAVED_FLOW_JS,
)


def adjacent_event_id(event_ids: list[str], current_id: str, offset: int) -> str:
    """Return a bounded adjacent event id for keyboard/button navigation."""
    if not event_ids:
        return current_id
    try:
        current_index = event_ids.index(current_id)
    except ValueError:
        current_index = 0
    next_index = min(max(current_index + offset, 0), len(event_ids) - 1)
    return event_ids[next_index]


def event_keyboard_payload(
    event_ids: list[str],
    selected_id: str,
    *,
    search_label: str = "全局检索",
) -> dict[str, Any]:
    """Build structured data for the Streamlit v2 keyboard component."""
    return {
        "event_ids": list(event_ids),
        "selected_id": selected_id,
        "search_label": search_label,
    }


def install_event_keyboard_navigation(event_ids: list[str], selected_id: str) -> None:
    """Install J/K, arrow and slash navigation using the current v2 component API."""
    global _event_keyboard_component
    mount_args = {
        "key": "event-workbench-keyboard",
        "data": event_keyboard_payload(event_ids, selected_id),
        "height": 0,
        "width": "stretch",
    }
    try:
        _event_keyboard_component(**mount_args)
    except ValueError as exc:
        # AppTest can reset the component registry while retaining imported
        # Python modules between independent app instances. Production does not
        # normally take this path, but re-registering makes the page testable.
        if "is not registered" not in str(exc):
            raise
        _event_keyboard_component = st.components.v2.component(
            "finance_radar_event_keyboard",
            js=EVENT_KEYBOARD_JS,
        )
        _event_keyboard_component(**mount_args)


def saved_flow_payload(
    flow: str,
    family: str,
    query: str,
    limit: int,
    *,
    source: str = "",
) -> dict[str, str]:
    """Normalize the only non-sensitive filter state allowed in browser storage."""
    normalized_flow = flow if flow in FLOW_PRESETS else "待复核"
    normalized_limit = limit if limit in (15, 25, 50, 100) else 25
    return {
        "flow": normalized_flow,
        "family": " ".join(str(family or "").split())[:80],
        "source": " ".join(str(source or "").split())[:80],
        "q": " ".join(str(query or "").split())[:120],
        "limit": str(normalized_limit),
    }


def render_saved_flow_manager(
    flow: str,
    family: str,
    query: str,
    limit: int,
    *,
    source: str = "",
) -> None:
    """Render an eight-slot, device-local saved-flow manager without server writes."""
    global _saved_flow_component
    mount_args = {
        "key": "event-workbench-saved-flows",
        "data": {"current": saved_flow_payload(flow, family, query, limit, source=source)},
        "height": 76,
        "width": "stretch",
    }
    try:
        _saved_flow_component(**mount_args)
    except ValueError as exc:
        if "is not registered" not in str(exc):
            raise
        _saved_flow_component = st.components.v2.component(
            "finance_radar_saved_flows",
            html=SAVED_FLOW_HTML,
            css=SAVED_FLOW_CSS,
            js=SAVED_FLOW_JS,
        )
        _saved_flow_component(**mount_args)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def compact_timestamp(value: str | None) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return "--:--"
    return parsed.astimezone(timezone.utc).strftime("%m-%d %H:%M")


def age_label(value: str | None, *, now: datetime | None = None) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return "unknown"
    now = now or datetime.now(timezone.utc)
    seconds = max(0, int((now.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def event_button_label(item: dict[str, Any]) -> str:
    status = STATUS_LABELS.get(str(item.get("status") or "").lower(), "EVENT")
    ticker = item.get("ticker_at_event") or "—"
    company = item.get("company_name") or item.get("event_type") or item.get("event_id") or "Unknown"
    company = " ".join(str(company).split())
    if len(company) > 34:
        company = company[:31] + "..."
    return f"{status:<8}  {compact_timestamp(item.get('last_updated_at'))}  {ticker} · {company}"


def event_feed_row(item: dict[str, Any]) -> str:
    """Return one safe, compact Situation Room event row."""
    status_key = str(item.get("status") or "candidate").lower()
    status = STATUS_LABELS.get(status_key, "EVENT")
    subject = item.get("company_name") or item.get("ticker_at_event") or "未识别主体"
    event_type = str(item.get("event_type") or "event").replace("_", " ")
    family_key = str(item.get("event_family") or "")
    family = EVENT_FAMILY_LABELS.get(family_key, family_key.replace("_", " ") or "未分类")
    source = str(item.get("discovery_source") or "unknown")
    authority = str(item.get("credibility_tier") or "P?")
    summary = item.get("evidence_excerpt") or "尚无结构化证据摘要，等待人工复核。"
    event_id = quote(str(item.get("event_id") or ""), safe="")
    timestamp = compact_timestamp(item.get("last_updated_at"))
    return (
        f'<a class="feed-row" href="./Event_Intelligence?flow={quote("全部事件", safe="")}&event_id={event_id}" '
        f'aria-label="打开事件 {escape(str(subject))}">'
        f'<div class="feed-time">{escape(timestamp)}</div>'
        '<div>'
        '<div class="feed-meta">'
        f'<span class="feed-tag status-{escape(status_key)}">{escape(status)}</span>'
        '<span class="feed-dot">●</span>'
        f'<span class="feed-tag">{escape(authority)}</span>'
        '<span class="feed-dot">●</span>'
        f'<span class="feed-tag">{escape(family)}</span>'
        '<span class="feed-dot">●</span>'
        f'<span class="feed-tag">{escape(source)}</span>'
        '</div>'
        f'<div class="feed-headline">{escape(str(subject))}'
        f'<span class="feed-type">{escape(event_type)}</span></div>'
        f'<div class="feed-summary">{escape(str(summary))}</div>'
        '</div></a>'
    )


def render_event_feed(items: list[dict[str, Any]]) -> None:
    st.markdown(
        f'<div class="feed-list">{"".join(event_feed_row(item) for item in items)}</div>',
        unsafe_allow_html=True,
    )


def flow_shortcuts_markup(event_status: dict[str, Any]) -> str:
    """Build a compact terminal-style entry bar for the canonical event flows."""
    verified = int(event_status.get("verified") or 0)
    candidate = int(event_status.get("candidate") or 0)
    weak = int(event_status.get("weak") or 0)
    rejected = int(event_status.get("rejected") or 0)
    flows = [
        ("全部事件", "全部事件", verified + candidate + weak + rejected, ""),
        ("待复核", "待复核", candidate, "is-review"),
        ("已核验", "证据核验", verified, "is-verified"),
        ("弱证据", "证据不足", weak, "is-review"),
        ("已拒绝", "已拒绝", rejected, ""),
    ]
    links = []
    for flow, label, count, state in flows:
        url = f"./Event_Intelligence?flow={quote(flow, safe='')}"
        links.append(
            f'<a class="flow-link {state}" href="{url}" aria-label="打开{escape(label)}信息流，{count}条">'
            f'<span>{escape(label)}</span><span class="flow-count">{count:,}</span></a>'
        )
    return (
        '<div class="flow-bar" role="group" aria-label="快速信息流">'
        '<span class="flow-bar-label">快速信息流</span>'
        f'{"".join(links)}</div>'
    )


def render_flow_shortcuts(event_status: dict[str, Any]) -> None:
    st.markdown(flow_shortcuts_markup(event_status), unsafe_allow_html=True)


def facet_values(facets: dict[str, Any], key: str, requested: str = "") -> list[str]:
    """Return safe, de-duplicated select options while preserving deep links."""
    values: list[str] = []
    if requested:
        values.append(str(requested))
    for item in (facets.get(key) or [])[:100]:
        value = " ".join(str(item.get("value") or "").split())[:80]
        if value and value not in values:
            values.append(value)
    return ["", *values]


def facet_counts(facets: dict[str, Any], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in (facets.get(key) or [])[:100]:
        value = " ".join(str(item.get("value") or "").split())[:80]
        if not value:
            continue
        try:
            counts[value] = max(0, int(item.get("count") or 0))
        except (TypeError, ValueError):
            counts[value] = 0
    return counts


def family_option_label(value: str, counts: dict[str, int]) -> str:
    if not value:
        return "全部事件族"
    label = EVENT_FAMILY_LABELS.get(value, value.replace("_", " "))
    count = counts.get(value)
    return f"{label} · {value} · {count:,}" if count is not None else f"{label} · {value}"


def source_option_label(value: str, counts: dict[str, int]) -> str:
    if not value:
        return "全部来源"
    count = counts.get(value)
    return f"{value} · {count:,}" if count is not None else value


def command_palette_markup(facets: dict[str, Any]) -> str:
    """Build data-backed terminal commands with encoded, read-only deep links."""
    commands = [
        ("回放演示", "REPLAY", "./?_page=Replay_Lab"),
        ("运行状态", "OPS", "./?_page=Operations_and_Model"),
    ]
    for item in (facets.get("families") or [])[:3]:
        value = " ".join(str(item.get("value") or "").split())[:80]
        if not value:
            continue
        label = EVENT_FAMILY_LABELS.get(value, value.replace("_", " "))
        commands.append(
            (
                label,
                f"FAMILY · {int(item.get('count') or 0):,}",
                f'./Event_Intelligence?flow={quote("全部事件", safe="")}&family={quote(value, safe="")}',
            )
        )
    for item in (facets.get("sources") or [])[:2]:
        value = " ".join(str(item.get("value") or "").split())[:80]
        if not value:
            continue
        commands.append(
            (
                value,
                f"SOURCE · {int(item.get('count') or 0):,}",
                f'./Event_Intelligence?flow={quote("全部事件", safe="")}&source={quote(value, safe="")}',
            )
        )
    links = "".join(
        '<a class="command-link" href="{}" aria-label="打开命令 {}">'
        '<span class="command-name">{}</span><span class="command-meta">{}</span></a>'.format(
            escape(url, quote=True),
            escape(name, quote=True),
            escape(name),
            escape(meta),
        )
        for name, meta, url in commands
    )
    return (
        '<div class="command-palette" role="navigation" aria-label="终端快捷命令">'
        '<span class="command-palette-label">快捷命令</span>'
        f"{links}</div>"
    )


def render_command_palette(facets: dict[str, Any]) -> None:
    st.markdown(command_palette_markup(facets), unsafe_allow_html=True)


def terminal_search_state(query: str, *, limit: int = 50) -> dict[str, str]:
    """Build canonical cross-page search state without retaining stale filters."""
    normalized = " ".join(str(query or "").split())
    return {"flow": "全部事件", "q": normalized, "limit": str(limit)}


MARKET_HORIZONS = (
    ("t_plus_5m", "T+5M"),
    ("t_plus_30m", "T+30M"),
    ("t_plus_1d", "T+1D"),
)


def market_horizon_items(detail: dict[str, Any], asset_id: str) -> list[dict[str, str]]:
    """Return honest observer-relative window states for one reviewed asset."""
    jobs = {
        str(item.get("observation_window")): item
        for item in detail.get("market_jobs") or []
        if str(item.get("asset_id") or "") == asset_id
    }
    metrics: dict[str, dict[str, Any]] = {}
    for metric in detail.get("market_metrics") or []:
        if str(metric.get("stable_id") or "") != asset_id:
            continue
        name = str(metric.get("metric_name") or "")
        for window, _label in MARKET_HORIZONS:
            if name.startswith(f"observer_return_{window}_pct__"):
                metrics[window] = metric
    items = []
    for window, label in MARKET_HORIZONS:
        metric = metrics.get(window)
        job = jobs.get(window)
        if metric is not None:
            try:
                value = Decimal(str(metric.get("metric_value")))
                rendered = f"{value:+.2f}%"
            except (InvalidOperation, ValueError):
                rendered = "INVALID"
            state = "evidence" if rendered != "INVALID" else "risk"
        elif job is None:
            rendered, state = "NOT SCHEDULED", "watch"
        else:
            status = str(job.get("status") or "UNKNOWN").upper()
            if status == "MISSED_WINDOW":
                rendered, state = "MISSED", "risk"
            elif status in {"PENDING", "RETRY"}:
                rendered, state = status, "watch"
            elif status == "COMPLETED":
                rendered, state = "CALCULATING", "watch"
            else:
                rendered, state = status, "watch"
        items.append({"window": window, "label": label, "value": rendered, "state": state})
    return items


def market_context_items(
    detail: dict[str, Any], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Return one honest latest state per reviewed event-asset relation."""
    now = now or datetime.now(timezone.utc)
    assets = {str(item.get("asset_id")): item for item in detail.get("assets") or []}
    latest: dict[str, dict[str, Any]] = {}
    for snapshot in detail.get("market_snapshots") or []:
        asset_id = str(snapshot.get("asset_id") or "")
        if asset_id not in latest:
            latest[asset_id] = snapshot

    items: list[dict[str, Any]] = []
    for asset_id, asset in assets.items():
        if not asset.get("market_observation_allowed"):
            continue
        snapshot = latest.get(asset_id)
        if snapshot is None:
            items.append(
                {
                    "symbol": str(asset.get("symbol") or asset.get("provider_symbol") or "UNMAPPED"),
                    "provider": str(asset.get("venue") or "provider unavailable"),
                    "price": "UNAVAILABLE",
                    "freshness": "NO SNAPSHOT",
                    "state": "watch",
                    "detail": "已允许只读观察，但尚无成功报价；不会用零值或旧行情替代。",
                    "available": "false",
                    "horizons": market_horizon_items(detail, asset_id),
                }
            )
            continue
        captured_at = str(snapshot.get("captured_at") or "")
        parsed = parse_datetime(captured_at)
        seconds = max(0, int((now - parsed).total_seconds())) if parsed else None
        state = "ok" if seconds is not None and seconds <= 900 else "watch"
        if seconds is None or seconds > 86400:
            state = "risk"
        safe_boundary = bool(snapshot.get("read_only")) and bool(snapshot.get("no_trading"))
        if not safe_boundary:
            state = "risk"
        currency = str(snapshot.get("currency") or "").strip()
        items.append(
            {
                "symbol": str(snapshot.get("provider_symbol") or asset.get("provider_symbol") or "—"),
                "provider": str(snapshot.get("provider") or asset.get("venue") or "unknown"),
                "price": f"{snapshot.get('price', '—')} {currency}".strip(),
                "freshness": f"CAPTURED {age_label(captured_at, now=now).upper()}",
                "state": state,
                "detail": (
                    "只读事件后观察 · 提供商未返回源时间戳"
                    if safe_boundary
                    else "BOUNDARY VIOLATION · 快照未声明只读/禁止交易"
                ),
                "available": "true",
                "horizons": market_horizon_items(detail, asset_id),
            }
        )
    if not items:
        items.append(
            {
                "symbol": "NO REVIEWED ASSET",
                "provider": "mapping required",
                "price": "UNAVAILABLE",
                "freshness": "NOT MAPPED",
                "state": "watch",
                "detail": "当前事件尚无经人工确认且允许观察的资产映射。",
                "available": "false",
                "horizons": [],
            }
        )
    return items


def market_context_markup(detail: dict[str, Any], *, now: datetime | None = None) -> str:
    cards = []
    for item in market_context_items(detail, now=now):
        state = item["state"] if item["state"] in {"ok", "watch", "risk"} else ""
        unavailable = " market-unavailable" if item["available"] == "false" else ""
        horizons = "".join(
            '<div class="market-horizon">'
            f'<div class="market-horizon-label">{escape(horizon["label"])}</div>'
            f'<div class="market-horizon-value {escape(horizon["state"])}">{escape(horizon["value"])}</div>'
            '</div>'
            for horizon in item.get("horizons") or []
        )
        horizon_markup = (
            f'<div class="market-horizons" aria-label="观察基线后的行情窗口">{horizons}</div>'
            if horizons
            else ""
        )
        cards.append(
            f'<div class="market-context-card{unavailable}">'
            '<div class="market-context-top">'
            f'<span class="market-symbol">{escape(item["symbol"])}</span>'
            f'<span class="market-provider">{escape(item["provider"])}</span>'
            f'<span class="market-freshness {state}">{escape(item["freshness"])}</span>'
            '</div>'
            f'<div class="market-price">{escape(item["price"])}</div>'
            f'<div class="market-meta">{escape(item["detail"])}</div>'
            f'{horizon_markup}'
            '</div>'
        )
    return (
        '<div class="market-context" role="group" aria-label="只读行情上下文">'
        f'{"".join(cards)}</div>'
    )


def render_market_context(detail: dict[str, Any]) -> None:
    st.markdown(market_context_markup(detail), unsafe_allow_html=True)


def evidence_summary(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    tiers = [str(item.get("authority_tier") or "P?") for item in evidence]
    highest = min(tiers, key=lambda tier: int(tier[1:]) if tier[1:].isdigit() else 99, default="—")
    statuses = {str(item.get("evidence_status") or "").lower() for item in evidence}
    conflict = any("contradict" in status or "conflict" in status for status in statuses)
    confirmed = sum(1 for status in statuses if "confirm" in status or "support" in status)
    return {
        "count": len(evidence),
        "highest_authority": highest,
        "conflict": conflict,
        "confirmed_statuses": confirmed,
    }


def score_dimensions(
    event: dict[str, Any],
    evidence: list[dict[str, Any]],
    model: dict[str, Any],
    *,
    current_version: dict[str, Any] | None = None,
) -> list[tuple[str, str, str]]:
    summary = evidence_summary(evidence)
    model_label = str(model.get("label") or "ABSTAIN")
    risk_state = "risk" if model_label == "RISK_REVIEW" else "watch" if model_label == "ABSTAIN" else "ok"
    workflow = str(event.get("status") or "unknown").upper()
    evidence_state = "ok" if evidence else "watch"
    conflict_state = "risk" if summary["conflict"] else "ok"
    version = int(event.get("current_version") or 1)
    novelty = "REVISION" if version > 1 else "NEW"
    confidence = model.get("confidence")
    confidence_text = f"{float(confidence):.0%}" if confidence is not None else "—"
    return [
        ("风险路由", model_label, risk_state),
        ("证据边", f"{summary['count']} linked", evidence_state),
        ("最高权威", summary["highest_authority"], "evidence" if evidence else "watch"),
        ("新颖性", novelty, "default"),
        ("证据冲突", "DETECTED" if summary["conflict"] else "CLEAR", conflict_state),
        ("模型", f"{confidence_text} · SHADOW", "default"),
        ("工作流", workflow, "ok" if workflow == "VERIFIED" else "watch"),
    ]


def render_score_rail(dimensions: list[tuple[str, str, str]]) -> None:
    cells: list[str] = []
    for label, value, state in dimensions:
        safe_state = state if state in {"ok", "watch", "risk", "evidence"} else ""
        cells.append(
            '<div class="score-cell">'
            f'<div class="score-label">{escape(label)}</div>'
            f'<div class="score-value {safe_state}">{escape(value)}</div>'
            '</div>'
        )
    st.markdown(
        f'<div class="score-rail" role="group" aria-label="Decision dimensions">{"".join(cells)}</div>',
        unsafe_allow_html=True,
    )


def source_health_state(source: dict[str, Any]) -> tuple[int, str]:
    status = str(source.get("cursor_status") or "UNOBSERVED").upper()
    if source.get("last_error") or status in {"FAILED", "ERROR", "DEGRADED"}:
        return 0, "ERROR"
    if status in {"UNOBSERVED", "STALE", "PENDING"} or not source.get("last_success_at"):
        return 1, "WATCH"
    return 2, "OK"
