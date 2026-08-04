from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any
from urllib.parse import quote, urlencode

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

PUBLIC_STATUS_LABELS = {
    "verified": "已核验",
    "candidate": "候选事件",
    "weak": "证据不足",
    "rejected": "已排除",
}

PUBLIC_STATE_LABELS = {
    "verified": "已核验",
    "excluded": "已排除",
    "insufficient": "证据不足",
    "rough_reviewed": "已粗审",
    "pending_verification": "待核验",
}

PUBLIC_STATE_STATUS_CLASS = {
    "verified": "verified",
    "excluded": "rejected",
    "insufficient": "weak",
    "rough_reviewed": "reviewed",
    "pending_verification": "candidate",
}

STATUS_GLYPHS = {
    "verified": "◆",
    "candidate": "◇",
    "weak": "△",
    "rejected": "×",
}

EVENT_FAMILY_LABELS = {
    "earnings": "业绩披露",
    "regulatory_filing": "监管申报",
    "corporate_action": "公司行动",
    "governance": "公司治理",
    "capital_return": "分红 / 回购",
    "fund_reporting": "基金报告",
    "fundamental_distress": "经营困境",
    "capital_structure": "资本结构",
    "price_crash": "价格异常",
    "equity_dilution": "股权稀释",
    "delisting_or_suspension": "退市 / 停牌",
    "distress_equity_death": "严重股权风险",
    "fundamental_shock": "经营重大变化",
    "listing_compliance": "上市合规",
    "spac_capital_formation": "SPAC 融资",
    "transaction_accounting": "交易会计",
    "corporate_action_merger": "并购",
    "source_metadata_control": "来源校验",
    "fundamental_metric_control": "财务指标校验",
    "biopharma": "生物医药",
    "identity_control": "主体校验",
    "product_safety": "产品安全",
    "spac_lifecycle": "SPAC 生命周期",
    "dilution_recapitalization": "稀释 / 资本重组",
    "price_dislocation_control": "价格异常校验",
    "regulatory": "监管动态",
    "distress": "财务困境",
    "listing_remediation": "上市整改",
    "regulatory_enforcement": "监管执法",
    "bankruptcy_reorganization": "破产重组",
    "equity_structure": "股权结构",
    "bankruptcy_or_distress": "破产 / 困境",
    "compensation_dilution": "薪酬 / 稀释",
    "debt_financing": "债务融资",
    "enforcement": "监管执法",
    "geopolitical": "地缘事件",
    "listing_status": "上市状态",
    "macro_policy": "宏观政策",
    "security_incident": "安全事件",
}

PUBLIC_SOURCE_LABELS = {
    "sec_current_filings": "SEC 官方文件",
    "sec_companyfacts": "SEC 公司数据",
    "sec_litigation_releases": "SEC 执法公告",
    "sec_trading_suspensions": "SEC 停牌公告",
    "nasdaq_trader": "Nasdaq 官方公告",
    "nyse_notices": "NYSE 官方公告",
    "federal_register": "美国联邦公报",
    "issuer_release": "公司官方公告",
    "aggregated_news": "公开新闻线索",
    "sharadar_active_research": "历史研究资料",
    "opennews_free": "公开新闻线索",
    "fda_medwatch": "FDA 安全公告",
    "federal_reserve_press": "美联储公告",
    "ecb_press": "欧洲央行公告",
    "ftc_press": "FTC 官方公告",
    "cftc_enforcement": "CFTC 执法公告",
    "fdic_press_releases": "FDIC 官方公告",
    "bls_key_indicators": "美国劳工统计局数据",
    "ecb_statistical_press": "欧洲央行统计公告",
}

PUBLIC_AUTHORITY_LABELS = {
    "P0": "官方原始文件",
    "P1": "发布主体原文",
    "P2": "可靠转述",
}

EVENT_FAMILY_RELEVANCE = {
    "earnings": "这类信息通常用于理解经营表现与财务变化。",
    "regulatory_filing": "监管申报可能补充公司义务、风险或重大事项的正式记录。",
    "corporate_action": "公司行动可能改变组织结构、证券权利或后续安排。",
    "governance": "治理变化可能影响管理责任、监督机制或重要决策流程。",
    "capital_return": "分红或回购可能影响现金使用与股东回报安排。",
    "fund_reporting": "基金报告用于了解持仓、资产与定期披露变化。",
    "fundamental_distress": "经营困境可能影响持续经营能力与债务履行。",
    "capital_structure": "资本结构变化可能影响融资条件、股本或持有人权益。",
    "price_crash": "价格异常需要结合公告与市场上下文判断原因，不能单独作为事实结论。",
    "equity_dilution": "股权稀释可能影响现有持有人的相对权益。",
    "delisting_or_suspension": "退市或停牌信息可能影响证券的持续交易资格。",
    "distress_equity_death": "严重股权风险可能涉及持续上市或剩余权益价值。",
    "fundamental_shock": "重大经营变化可能影响业务连续性或财务预期。",
    "listing_compliance": "上市合规事项可能影响证券的挂牌状态与整改要求。",
    "spac_capital_formation": "SPAC 融资事项可能影响交易结构、期限与资本安排。",
    "transaction_accounting": "交易会计事项可能改变财务报表的确认与披露口径。",
    "corporate_action_merger": "并购事项可能改变控制权、交易条件与后续审批安排。",
    "source_metadata_control": "该记录用于确认来源、主体与发布时间是否可靠。",
    "fundamental_metric_control": "该记录用于核对财务指标的来源与计算口径。",
    "biopharma": "生物医药事项通常需要核对监管状态、试验阶段与适用范围。",
    "identity_control": "主体校验用于避免同名公司、证券或文件归属错误。",
    "product_safety": "产品安全信息可能涉及召回、警示或监管处置。",
    "spac_lifecycle": "SPAC 生命周期事项可能影响交易期限、赎回或合并进度。",
    "dilution_recapitalization": "资本重组可能改变股份数量、证券条款或相对权益。",
    "price_dislocation_control": "该记录用于核对价格异常是否有可验证的事件依据。",
    "regulatory": "监管动态可能改变适用规则、合规要求或后续程序。",
    "distress": "财务困境可能影响偿付能力、经营连续性或重组安排。",
    "listing_remediation": "上市整改可能影响持续挂牌资格与整改期限。",
    "regulatory_enforcement": "监管执法可能涉及调查、处罚、和解或后续义务。",
    "bankruptcy_reorganization": "破产重组可能改变债权、股权与持续经营安排。",
    "equity_structure": "股权结构变化可能影响控制权与持有人权益。",
    "bankruptcy_or_distress": "破产或困境事项可能影响偿付、重组与剩余权益。",
    "compensation_dilution": "薪酬或增发安排可能带来股份稀释。",
    "debt_financing": "债务融资可能改变杠杆、偿付要求与资金成本。",
    "enforcement": "执法事项可能涉及调查、处罚、和解或整改要求。",
    "geopolitical": "地缘事件需要核对影响对象、时间与官方口径。",
    "listing_status": "上市状态变化可能影响证券的持续挂牌与交易安排。",
    "macro_policy": "宏观政策可能影响利率、流动性或行业规则，但不直接代表价格方向。",
    "security_incident": "安全事件可能影响业务连续性、数据责任或监管处置。",
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


EVENT_PREVIEW_FOCUS_JS = """
export default function(component) {
  const eventId = String(component?.data?.event_id || "");
  if (!eventId) return () => {};

  let frame = 0;
  const timers = [];
  const focusPreview = () => {
    const target = document.getElementById("event-preview");
    if (!target) return;
    target.setAttribute("tabindex", "-1");
    try { target.focus({ preventScroll: true }); } catch (_) { target.focus(); }
    const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    const behavior = reducedMotion ? "auto" : "smooth";
    target.scrollIntoView({ block: "start", behavior });
    // A Streamlit page delta can restore the previous scroll position after
    // the first paint.  Correct against the document scroll position as well
    // so the selected preview does not land below the fold.
    const headerHeight = document.querySelector('[data-testid="stHeader"]')?.getBoundingClientRect().height || 72;
    const top = target.getBoundingClientRect().top - headerHeight - 8;
    if (Math.abs(top) > 16) window.scrollBy({ top, behavior });
  };

  frame = requestAnimationFrame(focusPreview);
  // Streamlit may apply the page delta after the initial animation frame.
  // Repeat a bounded number of times so a card opened from a long feed lands
  // on its inline preview rather than an unrelated restored scroll position.
  [120, 420, 900].forEach((delay) => timers.push(window.setTimeout(focusPreview, delay)));
  return () => {
    cancelAnimationFrame(frame);
    timers.forEach((timer) => window.clearTimeout(timer));
  };
}
""".strip()


_event_preview_focus_component = st.components.v2.component(
    "finance_radar_event_preview_focus",
    js=EVENT_PREVIEW_FOCUS_JS,
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


def focus_event_preview(event_id: str) -> None:
    """Move keyboard and visual focus to an inline public event preview.

    The event card retains all query filters, so this is deliberately a
    same-page focus action rather than a route change or a new browser tab.
    """
    global _event_preview_focus_component
    normalized = " ".join(str(event_id or "").split())[:160]
    if not normalized:
        return
    mount_args = {
        "key": f"event-preview-focus::{normalized}",
        "data": {"event_id": normalized},
        "height": 0,
        "width": "stretch",
    }
    try:
        _event_preview_focus_component(**mount_args)
    except ValueError as exc:
        if "is not registered" not in str(exc):
            raise
        _event_preview_focus_component = st.components.v2.component(
            "finance_radar_event_preview_focus",
            js=EVENT_PREVIEW_FOCUS_JS,
        )
        _event_preview_focus_component(**mount_args)


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


def public_event_state(item: dict[str, Any]) -> str:
    """Return the stable reader-facing lifecycle state for one event."""
    declared = str(item.get("public_state") or "").strip().lower()
    if declared in PUBLIC_STATE_LABELS:
        return declared
    canonical = str(item.get("status") or "candidate").strip().lower()
    return {
        "verified": "verified",
        "rejected": "excluded",
        "weak": "insufficient",
        "candidate": "pending_verification",
    }.get(canonical, "pending_verification")


def public_event_subject(item: dict[str, Any]) -> str:
    """Prefer a named entity while being honest about incomplete identity."""
    value = item.get("company_name") or item.get("ticker_at_event")
    return " ".join(str(value).split()) if value else "主体待确认"


def _bounded_public_text(value: object, *, limit: int = 360) -> str:
    """Normalize a source-provided snippet without turning it into a new claim."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text or text in {"—", "-", "暂无", "未知", "unknown", "None", "null"}:
        return ""
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def public_event_fact_summary(item: dict[str, Any]) -> tuple[str, str]:
    """Return the most specific available public fact text and its provenance.

    This intentionally accepts only structured event facts.  Raw source
    summaries and legal excerpts remain in the evidence panel, where readers
    can see their original context; they are not promoted into a card claim.
    """
    facts = item.get("facts")
    if not isinstance(facts, dict):
        facts = {}
    candidates = (
        ("结构化事实摘要", item.get("public_fact_summary")),
        ("结构化事实摘要", item.get("fact_summary")),
        ("结构化事实摘要", item.get("evidence_summary")),
        ("结构化事实摘要", facts.get("public_fact_summary")),
        ("结构化事实摘要", facts.get("fact_summary")),
        ("结构化事实摘要", facts.get("evidence_summary")),
    )
    for provenance, value in candidates:
        summary = _bounded_public_text(value)
        if summary:
            if not any("\u4e00" <= char <= "\u9fff" for char in summary):
                provenance = f"{provenance}（原文）"
            return summary, provenance
    return "", ""


def public_event_timing(item: dict[str, Any]) -> list[tuple[str, str]]:
    """Expose time semantics instead of presenting every timestamp as "latest"."""
    event_date = _bounded_public_text(item.get("event_date"), limit=32)
    published_at = _bounded_public_text(
        item.get("source_published_at") or item.get("published_at"), limit=64
    )
    discovered_at = _bounded_public_text(item.get("first_seen_at"), limit=64)
    updated_at = _bounded_public_text(item.get("last_updated_at"), limit=64)
    reviewed_at = _bounded_public_text(item.get("reviewed_at"), limit=64)

    timing: list[tuple[str, str]] = []
    if event_date:
        timing.append(("事件日", event_date[:10]))
    if published_at:
        timing.append(("来源发布", compact_timestamp(published_at) + " UTC"))
    if discovered_at:
        timing.append(("系统发现", compact_timestamp(discovered_at) + " UTC"))
    if updated_at:
        timing.append(("最后更新", compact_timestamp(updated_at) + " UTC"))
    if reviewed_at:
        timing.append(("核验记录", compact_timestamp(reviewed_at) + " UTC"))
    return timing


def public_event_copy(item: dict[str, Any]) -> dict[str, str]:
    """Translate event metadata into calm Chinese product copy.

    Raw filing prose remains available as evidence.  It is deliberately not
    reused as the public summary because boilerplate and untranslated legal
    text are poor substitutes for a bounded statement of what is known.
    """
    state = public_event_state(item)
    subject = public_event_subject(item)
    family_key = str(item.get("event_family") or "")
    family = EVENT_FAMILY_LABELS.get(family_key, "其他公司事件")
    source_key = str(item.get("discovery_source") or "")
    source = PUBLIC_SOURCE_LABELS.get(source_key, "公开来源")
    authority = str(item.get("credibility_tier") or "P?")
    authority_label = PUBLIC_AUTHORITY_LABELS.get(authority, "来源待核实")
    fact_summary, fact_provenance = public_event_fact_summary(item)
    summary_templates = {
        "verified": f"{subject}出现一项与{family}相关的公开记录，当前已有证据支持。",
        "excluded": f"{subject}相关的{family}线索已被排除，不作为有效事件结论。",
        "insufficient": f"{subject}相关的{family}线索目前证据不足，系统不形成确定结论。",
        "rough_reviewed": f"{subject}相关的{family}线索已完成粗审，仍需正式证据核验。",
        "pending_verification": f"{subject}出现一项与{family}相关的公开线索，仍需核对原始文件。",
    }
    if fact_summary:
        state_suffix = {
            "verified": "当前状态为已核验；仍建议回到完整原文核对上下文。",
            "excluded": "当前线索已排除，不作为有效事实结论。",
            "insufficient": "当前证据不足，不将这段材料作为确定事实。",
            "rough_reviewed": "当前仅完成粗审，仍需正式证据核验。",
            "pending_verification": "当前仍需核对原始文件与上下文。",
        }[state]
        separator = " " if fact_summary[-1:] in {"。", "！", "？", ".", "!", "?"} else "。"
        summary = f"{fact_provenance}：{fact_summary}{separator}{state_suffix}"
    else:
        summary = summary_templates[state]
    return {
        "subject": subject,
        "family": family,
        "source": source,
        "authority": authority_label,
        "state": state,
        "state_label": PUBLIC_STATE_LABELS[state],
        "summary": summary,
        "summary_provenance": fact_provenance or "状态说明",
        "relevance": EVENT_FAMILY_RELEVANCE.get(
            family_key,
            "请结合原始来源、主体、日期与上下文判断其实际意义。",
        ),
    }


def event_feed_row(
    item: dict[str, Any],
    *,
    flow: str = "全部事件",
    public: bool = False,
    link_context: dict[str, Any] | None = None,
) -> str:
    """Return one safe, compact Situation Room event row."""
    canonical_status = str(item.get("status") or "candidate").lower()
    copy = public_event_copy(item) if public else None
    public_state = copy["state"] if copy else ""
    status_key = PUBLIC_STATE_STATUS_CLASS.get(public_state, canonical_status)
    status = (
        copy["state_label"]
        if public
        else STATUS_LABELS.get(canonical_status, "EVENT")
    )
    subject = copy["subject"] if copy else (
        item.get("company_name") or item.get("ticker_at_event") or "Unknown"
    )
    event_type = str(item.get("event_type") or "event").replace("_", " ")
    family_key = str(item.get("event_family") or "")
    family = copy["family"] if copy else EVENT_FAMILY_LABELS.get(
        family_key, family_key.replace("_", " ") or "未分类"
    )
    source_key = str(item.get("discovery_source") or "unknown")
    source = copy["source"] if copy else source_key
    authority = str(item.get("credibility_tier") or "P?")
    summary = copy["summary"] if copy else " ".join(
        str(item.get("evidence_excerpt") or "尚无结构化证据摘要，等待人工复核。").split()
    )
    preview_flow = flow if flow in FLOW_PRESETS else "全部事件"
    preview_params = {
        key: str(value)
        for key, value in (link_context or {}).items()
        if value not in (None, "")
    }
    if not public:
        preview_params.setdefault("preview_flow", preview_flow)
    preview_params["preview_event_id"] = str(item.get("event_id") or "")
    preview_url = f'./?{urlencode(preview_params)}#event-preview'
    timing = public_event_timing(item) if public else []
    timing_by_label = dict(timing)
    timestamp = timing_by_label.get("最后更新") or compact_timestamp(item.get("last_updated_at"))
    timing_markup = "".join(
        '<span><b>{}</b> {}</span>'.format(escape(label), escape(value))
        for label, value in timing
        if label != "最后更新"
    )
    status_glyph = STATUS_GLYPHS.get(status_key, "◇" if status_key == "reviewed" else "○")
    authority_class = f"authority-{authority.lower()}" if authority.lower() in {"p0", "p1", "p2"} else ""
    authority_label = PUBLIC_AUTHORITY_LABELS.get(authority) if public else authority
    authority_chip = (
        f'<span class="feed-chip {escape(authority_class)}">{escape(authority_label)}</span>'
        if authority_label
        else ""
    )
    event_type_markup = "" if public else f'<span class="feed-type">{escape(event_type)}</span>'
    row_class = "feed-row public-feed-row" if public else "feed-row"
    open_label = "查看证据 ›" if public else "当前页预览 ›"
    aria_label = (
        f"在当前页面查看 {subject} 的证据" if public else f"在当前页面预览事件 {subject}"
    )
    impact_markup = (
        f'<div class="feed-impact">为什么关注：{escape(copy["relevance"])}</div>'
        if copy
        else ""
    )
    return (
        f'<a class="{row_class}" href="{preview_url}" target="_self" '
        f'aria-label="{escape(str(aria_label), quote=True)}">'
        '<div class="feed-time"><span class="feed-time-label">最后更新</span>'
        f'<time>{escape(timestamp)}</time></div>'
        f'<div class="feed-signal status-{escape(status_key)}" aria-hidden="true">{escape(status_glyph)}</div>'
        '<div class="feed-body">'
        '<div class="feed-meta">'
        f'<span class="feed-chip status-{escape(status_key)}">{escape(status)}</span>'
        f'{authority_chip}'
        f'<span class="feed-chip">{escape(family)}</span>'
        f'<span class="feed-chip">{escape(source)}</span>'
        '</div>'
        f'<div class="feed-headline">{escape(str(subject))}'
        f'{event_type_markup}</div>'
        f'<div class="feed-summary">{escape(str(summary))}</div>'
        f'<div class="feed-timing" aria-label="事件时间">{timing_markup}</div>'
        f'{impact_markup}'
        '</div>'
        f'<span class="feed-open" aria-hidden="true">{open_label}</span>'
        '</a>'
    )


def render_event_feed(
    items: list[dict[str, Any]],
    *,
    flow: str = "全部事件",
    public: bool = False,
    link_context: dict[str, Any] | None = None,
) -> None:
    st.markdown(
        f'<div class="feed-list">'
        f'{"".join(event_feed_row(item, flow=flow, public=public, link_context=link_context) for item in items)}'
        '</div>',
        unsafe_allow_html=True,
    )


def evidence_route_markup(event_status: dict[str, Any], review_queue: int) -> str:
    """Explain the product's evidence path with current, non-inferred counts."""
    verified = max(0, int(event_status.get("verified") or 0))
    candidate = max(0, int(event_status.get("candidate") or 0))
    weak = max(0, int(event_status.get("weak") or 0))
    rejected = max(0, int(event_status.get("rejected") or 0))
    discovered = verified + candidate + weak + rejected
    review_count = max(0, int(review_queue or 0))
    stages = [
        ("01 · 发现", f"{discovered:,}", "规范化事件", "is-evidence"),
        ("02 · 证据门", f"{verified:,}", "已核验", "is-verified"),
        ("03 · 补证", f"{candidate + weak:,}", "候选与弱证据", "is-review"),
        ("04 · Shadow", "只分流", "不替代人工结论", "is-shadow"),
        ("05 · 人工复核", f"{review_count:,}", "自动执行始终禁用", "is-locked"),
    ]
    cards = "".join(
        '<div class="route-stage {}">'
        '<div class="route-kicker">{}</div>'
        '<div class="route-value">{}</div>'
        '<div class="route-copy">{}</div>'
        '</div>'.format(
            escape(state, quote=True),
            escape(kicker),
            escape(value),
            escape(copy),
        )
        for kicker, value, copy, state in stages
    )
    return (
        '<div class="evidence-route" role="group" '
        'aria-label="证据路径：发现、证据门、补证、Shadow 分流、人工复核">'
        f'{cards}</div>'
    )


def render_evidence_route(event_status: dict[str, Any], review_queue: int) -> None:
    st.markdown(evidence_route_markup(event_status, review_queue), unsafe_allow_html=True)


def flow_shortcuts_markup(
    event_status: dict[str, Any],
    *,
    public: bool = False,
    public_funnel: dict[str, Any] | None = None,
) -> str:
    """Build a compact terminal-style entry bar for the canonical event flows."""
    verified = int(event_status.get("verified") or 0)
    candidate = int(event_status.get("candidate") or 0)
    weak = int(event_status.get("weak") or 0)
    rejected = int(event_status.get("rejected") or 0)
    if public:
        funnel = public_funnel or {}
        def funnel_count(key: str, fallback: int) -> int:
            value = funnel.get(key)
            return fallback if value is None else max(0, int(value))

        total = funnel_count("total", verified + candidate + weak + rejected)
        flows = [
            ("", "全部事件", total, ""),
            (
                "pending_verification",
                "待核验",
                funnel_count("pending_verification", candidate),
                "is-review",
            ),
            (
                "rough_reviewed",
                "已粗审",
                funnel_count("rough_reviewed", 0),
                "is-reviewed",
            ),
            (
                "insufficient",
                "证据不足",
                funnel_count("insufficient", weak),
                "is-review",
            ),
            ("verified", "已核验", funnel_count("verified", verified), "is-verified"),
            ("excluded", "已排除", funnel_count("excluded", rejected), ""),
        ]
    else:
        flows = [
            ("全部事件", "全部事件", verified + candidate + weak + rejected, ""),
            ("待复核", "待复核", candidate, "is-review"),
            ("已核验", "证据核验", verified, "is-verified"),
            ("弱证据", "证据不足", weak, "is-review"),
            ("已拒绝", "已拒绝", rejected, ""),
        ]
    links = []
    for flow, label, count, state in flows:
        if public:
            url = f"./?preview_state={quote(flow, safe='')}#live-events" if flow else "./#live-events"
        else:
            url = f"./?preview_flow={quote(flow, safe='')}#live-events"
        links.append(
            f'<a class="flow-link {state}" href="{url}" target="_self" '
            f'aria-label="在当前页面筛选{escape(label)}信息流，{count}条">'
            f'<span>{escape(label)}</span><span class="flow-count">{count:,}</span></a>'
        )
    return (
        '<div class="flow-bar" role="group" aria-label="快速信息流">'
        '<span class="flow-bar-label">快速信息流</span>'
        f'{"".join(links)}</div>'
    )


def render_flow_shortcuts(
    event_status: dict[str, Any],
    *,
    public: bool = False,
    public_funnel: dict[str, Any] | None = None,
) -> None:
    st.markdown(
        flow_shortcuts_markup(
            event_status,
            public=public,
            public_funnel=public_funnel,
        ),
        unsafe_allow_html=True,
    )


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
        '<a class="command-link" href="{}" target="_self" aria-label="打开命令 {}">'
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
    confirmed = sum(
        1
        for status in statuses
        if "confirm" in status or "support" in status or "light_primary" in status
    )
    return {
        "count": len(evidence),
        "highest_authority": highest,
        "conflict": conflict,
        "confirmed_statuses": confirmed,
    }


def next_action_guidance(
    event: dict[str, Any],
    evidence: list[dict[str, Any]],
    model: dict[str, Any],
    *,
    trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Choose the next review step from auditable state, never model confidence."""
    summary = evidence_summary(evidence)
    workflow = str(event.get("status") or "candidate").lower()
    workflow_label = {
        "candidate": "待复核",
        "weak": "弱证据",
        "verified": "已核验",
        "rejected": "已拒绝",
    }.get(workflow, workflow or "待复核")
    model_label = str(model.get("label") or "ABSTAIN").upper()
    has_agent_trace = bool((trace or {}).get("agent_decisions"))
    has_human_review = bool((trace or {}).get("human_overrides"))

    if summary["conflict"]:
        guidance = {
            "code": "EVIDENCE_CONFLICT",
            "tone": "risk",
            "priority": "立即复核",
            "title": "先处理证据冲突",
            "reason": f"当前 {summary['count']} 条证据中存在相互冲突的状态，结论不能自动升级。",
            "steps": (
                "并排核对冲突来源的精确引文与发布时间",
                "确认是否属于修订、撤回或同名主体混淆",
                "在人工复核记录中写明保留、降级或拒绝理由",
            ),
        }
    elif not evidence:
        guidance = {
            "code": "MISSING_EVIDENCE",
            "tone": "risk",
            "priority": "先补证据",
            "title": "补齐可引用的原始证据",
            "reason": "当前事件没有证据边，系统必须保持待复核或弃权。",
            "steps": (
                "优先找到 P0/P1 原始来源并确认主体与日期",
                "截取能够直接支持事件事实的精确段落",
                "运行只读证据代理生成可追溯记录后再人工判断",
            ),
        }
    elif workflow == "rejected":
        guidance = {
            "code": "REJECTION_WATCH",
            "tone": "ok",
            "priority": "保留拒绝",
            "title": "保留拒绝结论，等待新证据",
            "reason": "当前工作流已拒绝该事件；只有来源修订或新增高权威证据才应重开。",
            "steps": (
                "确认拒绝理由仍与当前原始来源一致",
                "关注撤回、修订或新增监管文件",
                "没有新证据时不重复运行判断流程",
            ),
        }
    elif workflow == "verified":
        guidance = {
            "code": "VERIFIED_MONITOR",
            "tone": "ok",
            "priority": "持续观察",
            "title": "核验已完成，转入版本观察",
            "reason": f"事件已有 {summary['count']} 条证据边，当前工作流状态为已核验。",
            "steps": (
                "确认最高权威来源与当前事件版本一致",
                "只在来源修订或新增事实时创建新版本",
                "保留精确引文与人工记录以便回放审计",
            ),
        }
    elif workflow == "weak":
        guidance = {
            "code": "WEAK_EVIDENCE",
            "tone": "watch",
            "priority": "补强证据",
            "title": "先补强证据，再决定是否保留事件",
            "reason": f"当前 {summary['count']} 条证据尚不足以闭合事实，工作流保持弱证据状态。",
            "steps": (
                f"复核现有最高权威 {summary['highest_authority']} 来源能直接支持哪些事实",
                "补充更高权威来源或缩小事件陈述的事实范围",
                "仍无法闭合时由人工标记证据不足或拒绝",
            ),
        }
    elif model_label == "RISK_REVIEW":
        guidance = {
            "code": "HUMAN_RISK_REVIEW",
            "tone": "risk",
            "priority": "人工风险复核",
            "title": "核对不利事件的事实边界",
            "reason": "Shadow 路由将其列入风险复核队列；这只是排序信号，不是交易方向。",
            "steps": (
                f"先核对最高权威 {summary['highest_authority']} 来源的精确引文",
                "区分已发生事实、条件性表述与前瞻性陈述",
                "由人工记录保留、降级或证据不足结论",
            ),
        }
    else:
        first_step = (
            f"逐字核对最高权威 {summary['highest_authority']} 来源与事件摘要"
            if summary["highest_authority"] == "P0"
            else "优先补充 P0/P1 来源，再核对现有引文"
        )
        final_step = (
            "核对代理引用后，在人工复核记录中写明结论"
            if has_agent_trace
            else "需要结构化比对时，先运行只读证据代理"
        )
        guidance = {
            "code": "EVIDENCE_REVIEW",
            "tone": "watch",
            "priority": "等待判断",
            "title": "核对最高权威证据后再判断",
            "reason": f"当前为{workflow_label}状态，共有 {summary['count']} 条证据边，尚未完成事实闭环。",
            "steps": (
                first_step,
                "检查主体、发布日期、事件版本和引文上下文",
                final_step,
            ),
        }

    guidance["review_recorded"] = has_human_review
    return guidance


def next_action_markup(guidance: dict[str, Any]) -> str:
    """Return escaped markup for one calm, explicit next-action card."""
    tone = str(guidance.get("tone") or "watch")
    if tone not in {"ok", "watch", "risk"}:
        tone = "watch"
    steps = "".join(
        f"<li>{escape(str(step))}</li>" for step in (guidance.get("steps") or ())[:3]
    )
    review_note = (
        '<span class="next-action-reviewed">已存在人工复核记录</span>'
        if guidance.get("review_recorded")
        else '<span>尚未记录人工复核</span>'
    )
    return (
        f'<section class="next-action next-action-{tone}" aria-labelledby="next-action-title">'
        '<div class="next-action-topline">'
        '<span>下一步行动</span>'
        f'<strong>{escape(str(guidance.get("priority") or "等待判断"))}</strong>'
        '</div>'
        f'<h2 id="next-action-title">{escape(str(guidance.get("title") or "人工复核"))}</h2>'
        f'<p>{escape(str(guidance.get("reason") or "请依据原始证据进行人工判断。"))}</p>'
        f'<ol>{steps}</ol>'
        '<div class="next-action-boundary">'
        f'{review_note}<span>只读提示 · 不构成交易建议 · 不触发下单</span>'
        '</div>'
        '</section>'
    )


def render_next_action_prompt(guidance: dict[str, Any]) -> None:
    st.markdown(next_action_markup(guidance), unsafe_allow_html=True)


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
