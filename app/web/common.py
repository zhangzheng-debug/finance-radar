from __future__ import annotations

import json
import os
import hashlib
import re
import time
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from html import escape
from threading import RLock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import streamlit as st


API_URL = os.getenv("FINANCE_RADAR_API_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_TOKEN = os.getenv("FINANCE_RADAR_ADMIN_TOKEN")
REVIEWER_TOKEN = os.getenv("FINANCE_RADAR_REVIEWER_TOKEN")
OPERATOR_TOKEN = os.getenv("FINANCE_RADAR_OPERATOR_TOKEN")
SHOW_DEBUG = os.getenv("FINANCE_RADAR_SHOW_DEBUG") == "1"
UI_ROLES = frozenset({"public", "reviewer", "operator", "admin"})
_configured_ui_role = os.getenv("FINANCE_RADAR_UI_ROLE", "public").strip().lower()
UI_ROLE = _configured_ui_role if _configured_ui_role in UI_ROLES else "public"
DEEP_LINK_STATE_KEY = "_finance_radar_deep_link"
DESIGN_TOKENS_V3 = Path(__file__).with_name("design_tokens_v3.css").read_text(encoding="utf-8")
STYLE_V3 = Path(__file__).with_name("style_v3.css").read_text(encoding="utf-8")


@dataclass(frozen=True)
class ApiCacheMetadata:
    age_seconds: float
    cache_hit: bool
    stale: bool


_API_GET_CACHE_MAX_ENTRIES = 64
_api_get_cache: OrderedDict[
    tuple[str, str, int], tuple[float, dict[str, Any]]
] = OrderedDict()
_api_get_cache_lock = RLock()


PUBLIC_NAVIGATION: tuple[dict[str, str], ...] = (
    {
        "key": "home",
        "path": "Home.py",
        "url": "./",
        "label": "态势总览",
        "description": "浏览事件、证据摘要与更新状态",
    },
    {
        "key": "replay",
        "path": "pages/2_Replay_Lab.py",
        "url": "./Replay_Lab",
        "label": "证据演示",
        "description": "用精选案例理解证据判断过程",
    },
    {
        "key": "method",
        "path": "pages/5_Method_and_Boundaries.py",
        "url": "./Method_and_Boundaries",
        "label": "方法与边界",
        "description": "了解来源、时间、置信度与使用边界",
    },
)


ADMIN_NAVIGATION: tuple[dict[str, str], ...] = (
    {
        "key": "admin_home",
        "path": "Admin.py",
        "url": "./",
        "label": "管理概览",
        "description": "内部运行、复核队列与服务态势",
    },
    {
        "key": "events",
        "path": "pages/1_Event_Intelligence.py",
        "url": "./?_page=Event_Intelligence",
        "label": "人工复核",
        "description": "逐条核验证据并记录人工判断",
    },
    {
        "key": "replay",
        "path": "pages/2_Replay_Lab.py",
        "url": "./?_page=Replay_Lab",
        "label": "证据回放",
        "description": "用冻结时钟检查完整判断链路",
    },
    {
        "key": "operations",
        "path": "pages/3_Operations_and_Model.py",
        "url": "./?_page=Operations_and_Model",
        "label": "运行与模型",
        "description": "服务健康、数据来源与模型状态",
    },
    {
        "key": "adjudication",
        "path": "pages/4_Adjudication_Studio.py",
        "url": "./?_page=Adjudication_Studio",
        "label": "双人盲审",
        "description": "独立人工标注与分歧处理",
    },
    {
        "key": "method",
        "path": "pages/5_Method_and_Boundaries.py",
        "url": "./?_page=Method_and_Boundaries",
        "label": "方法与边界",
        "description": "核对对外口径与系统硬边界",
    },
)


REVIEWER_NAVIGATION: tuple[dict[str, str], ...] = (
    {
        "key": "reviewer_home",
        "path": "Reviewer.py",
        "url": "./",
        "label": "复核概览",
        "description": "聚焦待核验证据与人工判断",
    },
    ADMIN_NAVIGATION[1],
    ADMIN_NAVIGATION[2],
    ADMIN_NAVIGATION[4],
    ADMIN_NAVIGATION[5],
)


OPERATOR_NAVIGATION: tuple[dict[str, str], ...] = (
    {
        "key": "operator_home",
        "path": "Operator.py",
        "url": "./",
        "label": "运维概览",
        "description": "查看服务、来源、备份与模型运行",
    },
    ADMIN_NAVIGATION[3],
    ADMIN_NAVIGATION[5],
)


def navigation_for_role(role: str) -> tuple[dict[str, str], ...]:
    return {
        "public": PUBLIC_NAVIGATION,
        "reviewer": REVIEWER_NAVIGATION,
        "operator": OPERATOR_NAVIGATION,
        "admin": ADMIN_NAVIGATION,
    }.get(role, PUBLIC_NAVIGATION)


PRIMARY_NAVIGATION = navigation_for_role(UI_ROLE)


ACCESSIBILITY_CSS = """
/* Shared keyboard and touch contract. Keep this separate so it can be tested. */
[data-testid="stMain"]:focus-visible,
[data-testid="stSidebarNav"] a:focus-visible,
[data-testid="stPageLink"] a:focus-visible,
[data-baseweb="tab"]:focus-visible,
[data-testid="stDataFrame"] button:focus-visible,
button:focus-visible,
a[href]:focus-visible,
input:focus-visible,
select:focus-visible,
textarea:focus-visible,
[tabindex]:not([tabindex="-1"]):focus-visible {
    outline: 2px solid var(--fr-cyan) !important;
    outline-offset: 2px !important;
    box-shadow: 0 0 0 3px rgba(41, 189, 227, .22) !important;
}
[data-testid="stSidebarNav"] a,
.stButton > button,
.stLinkButton > a,
[data-testid="stPageLink"] a,
[data-baseweb="tab"] {
    min-height: 40px;
}
[data-testid="stDataFrame"] button,
button[aria-label^="Help for"],
a[aria-label="Link to heading"] {
    min-width: 28px !important;
    min-height: 28px !important;
}
a[aria-label="Link to heading"] {
    display: inline-flex !important;
    align-items: center;
    justify-content: center;
}
@media (max-width: 900px) {
    [data-testid="stSidebarNav"] a,
    .stButton > button,
    .stLinkButton > a,
    [data-testid="stPageLink"] a,
    [data-baseweb="tab"] {
        min-height: 44px;
    }
    [data-testid="stSidebarNav"] a {
        min-width: 44px;
    }
    [data-testid="stDataFrame"] button,
    button[aria-label^="Help for"],
    a[aria-label="Link to heading"] {
        min-width: 32px !important;
        min-height: 32px !important;
    }
}
""".strip()


ACCESSIBILITY_JS = """
export default function(component) {
  const applyContract = () => {
    document.documentElement.setAttribute("lang", "zh-CN");
    const main = document.querySelector('[data-testid="stMain"]');
    if (main) {
      main.setAttribute("role", "main");
      main.setAttribute("aria-label", "Finance Radar 主工作区");
    }
    const sidebar = document.querySelector('[data-testid="stSidebarContent"]');
    if (sidebar) {
      // The rendered primary <nav> owns the only navigation landmark.  Giving
      // its generic Streamlit parent the same role creates duplicate landmarks
      // for assistive technology.
      sidebar.removeAttribute("role");
      sidebar.removeAttribute("aria-label");
    }
  };
  applyContract();
  const observer = new MutationObserver(applyContract);
  observer.observe(document.body, { childList: true, subtree: true });
  return () => observer.disconnect();
}
""".strip()


_accessibility_component = st.components.v2.component(
    "finance_radar_accessibility_contract",
    js=ACCESSIBILITY_JS,
)


class ApiError(RuntimeError):
    pass


def require_ui_role(*allowed_roles: str) -> None:
    """Stop a page before it renders outside its declared internal role."""
    if UI_ROLE in allowed_roles:
        return
    st.error("当前内部角色无权访问此页面。" if UI_ROLE != "public" else "此页面仅限内部管理环境。")
    st.caption(
        "公开界面不会开放复核写入、运行控制、模型治理或盲审工具；"
        "复核、运维和管理员工作面也彼此隔离。"
    )
    st.stop()


def require_admin_ui() -> None:
    """Backward-compatible strict guard for the administrator landing page."""
    require_ui_role("admin")


def restore_deep_link(page_slug: str) -> None:
    """Restore query parameters captured by Home's fresh deep-link bootstrap."""
    transfer = st.session_state.get(DEEP_LINK_STATE_KEY)
    if not isinstance(transfer, dict) or transfer.get("page") != page_slug:
        return
    st.session_state.pop(DEEP_LINK_STATE_KEY, None)
    st.query_params.clear()
    for key, value in (transfer.get("params") or {}).items():
        if key != "_page" and value not in (None, ""):
            st.query_params[key] = str(value)


def render_primary_navigation(active: str) -> None:
    """Render a stable Chinese navigation layer without changing page routes."""
    navigation = navigation_for_role(UI_ROLE)
    current = next((item for item in navigation if item["key"] == active), None)
    if current is None:
        internal_keys = {
            item["key"]
            for role_navigation in (REVIEWER_NAVIGATION, OPERATOR_NAVIGATION, ADMIN_NAVIGATION)
            for item in role_navigation
        } - {
            item["key"] for item in PUBLIC_NAVIGATION
        }
        if active in internal_keys:
            require_ui_role("reviewer", "operator", "admin")
        raise ValueError(f"Unknown primary navigation key: {active}")

    links = "".join(
        '<a class="radar-primary-link{}" href="{}" target="_self"{}>'
        '<span>{}</span><small>{}</small></a>'.format(
            " is-active" if item["key"] == active else "",
            escape(item["url"], quote=True),
            ' aria-current="page"' if item["key"] == active else "",
            escape(item["label"]),
            escape(item["description"]),
        )
        for item in navigation
    )
    role_section = {
        "public": "公开浏览",
        "reviewer": "人工复核",
        "operator": "运行维护",
        "admin": "系统管理",
    }.get(UI_ROLE, "公开浏览")
    with st.sidebar:
        st.markdown(
            '<div class="radar-sidebar-brand">'
            '<span class="radar-sidebar-mark" aria-hidden="true">◎</span>'
            '<div><strong>FINANCE RADAR</strong><span>证据情报终端</span></div>'
            '</div>'
            f'<div class="radar-sidebar-section">{role_section}</div>'
            '<nav class="radar-primary-nav" aria-label="主要页面">'
            f'{links}'
            '</nav>'
            '<div class="radar-sidebar-current">'
            '<span>当前工作面</span>'
            f'<strong>{escape(current["label"])}</strong>'
            f'<p>{escape(current["description"])}</p>'
            '</div>'
            '<div class="radar-sidebar-boundary">'
            '<span aria-hidden="true">◈</span> '
            f'{"只读情报 · 证据驱动核验 · 不触发交易" if UI_ROLE == "public" else "内部最小权限 · 操作留痕 · 不触发交易"}'
            '</div>',
            unsafe_allow_html=True,
        )


def api_request(
    path: str,
    *,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    timeout_seconds: int = 20,
) -> dict[str, Any]:
    if not 1 <= int(timeout_seconds) <= 20:
        raise ValueError("timeout_seconds must be between 1 and 20")
    normalized_method = method.upper()
    if normalized_method not in {"GET", "HEAD"}:
        reviewer_write = bool(
            re.fullmatch(r"/api/v1/events/[^/]+/human-override", path)
            or re.fullmatch(r"/api/v1/adjudication/samples/[^/]+/reviews", path)
        )
        operator_write = bool(
            re.fullmatch(r"/api/v1/events/[^/]+/agent/run", path)
            or re.fullmatch(r"/api/v1/replays/[^/]+/(?:run|reset)", path)
            or re.fullmatch(r"/api/v1/demo/mode/[^/]+", path)
        )
        allowed = (
            UI_ROLE == "admin"
            or (UI_ROLE == "reviewer" and reviewer_write)
            or (UI_ROLE == "operator" and operator_write)
        )
        if not allowed:
            raise ApiError(
                "公开界面只允许只读请求"
                if UI_ROLE == "public"
                else "当前界面角色不允许此写入请求"
            )
    headers = {"Accept": "application/json"}
    if UI_ROLE == "reviewer" and REVIEWER_TOKEN:
        headers["X-Reviewer-Token"] = REVIEWER_TOKEN
    elif UI_ROLE == "operator" and OPERATOR_TOKEN:
        headers["X-Operator-Token"] = OPERATOR_TOKEN
    elif UI_ROLE == "admin" and ADMIN_TOKEN:
        headers["X-Admin-Token"] = ADMIN_TOKEN
    body = None
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{API_URL}{path}", method=normalized_method, headers=headers, data=body
    )
    try:
        with urllib.request.urlopen(request, timeout=int(timeout_seconds)) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ApiError(f"API {exc.code}") from exc
    except json.JSONDecodeError as exc:
        raise ApiError("API returned invalid data") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ApiError(f"API unavailable ({type(exc).__name__})") from exc
    if not isinstance(payload, dict):
        raise ApiError("API returned invalid data")
    if "error" in payload:
        error = payload.get("error")
        raw_code = str(
            (error.get("code") or "REQUEST_FAILED")
            if isinstance(error, dict)
            else "REQUEST_FAILED"
        ).upper()
        safe_code = raw_code if re.fullmatch(r"[A-Z0-9_]{1,64}", raw_code) else "REQUEST_FAILED"
        raise ApiError(f"API error {safe_code}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ApiError("API returned invalid data")
    return data


def cached_api_get(
    path: str,
    *,
    ttl_seconds: float = 15.0,
    stale_if_error_seconds: float = 120.0,
    timeout_seconds: int = 5,
) -> tuple[dict[str, Any], ApiCacheMetadata]:
    """Return a bounded, role-scoped GET snapshot with explicit freshness metadata.

    Stale data is used only when a refresh fails and its age is within the
    caller's stated bound. Callers must surface ``metadata.stale`` to users.
    The loader identity is part of the key so Streamlit tests and role-specific
    processes cannot reuse another caller's snapshot.
    """

    if not path.startswith("/"):
        raise ValueError("cached API path must be absolute")
    if ttl_seconds < 0 or stale_if_error_seconds < ttl_seconds:
        raise ValueError("cache freshness bounds are invalid")
    key = (UI_ROLE, path, id(api_request))
    now = time.monotonic()
    with _api_get_cache_lock:
        cached = _api_get_cache.get(key)
        if cached is not None:
            cached_at, cached_data = cached
            age = max(0.0, now - cached_at)
            if age <= ttl_seconds:
                _api_get_cache.move_to_end(key)
                return deepcopy(cached_data), ApiCacheMetadata(age, True, False)

    try:
        fresh = api_request(path, timeout_seconds=timeout_seconds)
    except Exception:
        if cached is not None:
            cached_at, cached_data = cached
            age = max(0.0, time.monotonic() - cached_at)
            if age <= stale_if_error_seconds:
                return deepcopy(cached_data), ApiCacheMetadata(age, True, True)
        raise

    stored_at = time.monotonic()
    with _api_get_cache_lock:
        _api_get_cache[key] = (stored_at, deepcopy(fresh))
        _api_get_cache.move_to_end(key)
        while len(_api_get_cache) > _API_GET_CACHE_MAX_ENTRIES:
            _api_get_cache.popitem(last=False)
    return deepcopy(fresh), ApiCacheMetadata(0.0, False, False)


def clear_api_get_cache() -> None:
    """Clear process-local UI snapshots (primarily for deterministic tests)."""

    with _api_get_cache_lock:
        _api_get_cache.clear()


def query_path(path: str, **params: Any) -> str:
    clean = {key: value for key, value in params.items() if value not in (None, "")}
    return path + ("?" + urllib.parse.urlencode(clean) if clean else "")


def format_elapsed(seconds: float | int | None) -> str:
    """Render an operational age in a human-scale unit without hiding staleness."""
    if seconds is None:
        return "—"
    value = max(0.0, float(seconds))
    if value >= 86400:
        return f"{value / 86400:.1f} 天"
    if value >= 3600:
        return f"{value / 3600:.1f} 小时"
    if value >= 60:
        return f"{value / 60:.1f} 分钟"
    return f"{int(value)} 秒"


def install_style() -> None:
    st.markdown(
        """
        <style>
        :root {
            --fr-canvas: #060c13;
            --fr-panel: #0a1420;
            --fr-raised: #0e1b29;
            --fr-border: #1b2d3d;
            --fr-border-strong: #294257;
            --fr-text: #e6eef5;
            --fr-muted: #879caf;
            --fr-cyan: #29bde3;
            --fr-green: #3ed59f;
            --fr-amber: #f0b35a;
            --fr-red: #ff6b7c;
            --fr-purple: #9b8afb;
        }
        html, body, [class*="css"] {
            font-family: "Segoe UI", "Noto Sans SC", "Microsoft YaHei", sans-serif;
            font-variant-numeric: tabular-nums;
        }
        .stApp { background: var(--fr-canvas); color: var(--fr-text); }
        [data-testid="stHeader"] { background: rgba(6, 12, 19, .94); }
        [data-testid="stSidebar"] { background: #07111b; border-right: 1px solid var(--fr-border); }
        [data-testid="stSidebarNav"] { padding-top: 1.2rem; }
        [data-testid="stSidebarNav"] span { font-size: .79rem; letter-spacing: -.01em; }
        [data-testid="stSidebarNav"] a { border-radius: 4px; min-height: 36px; }
        [data-testid="stSidebarNav"] a[aria-current="page"] { background: var(--fr-raised); }
        .block-container { max-width: 1600px; padding-top: 2.65rem; padding-bottom: 2rem; }
        h1, h2, h3 { letter-spacing: -.015em; }
        h2 { font-size: 1.08rem !important; }
        h3 { font-size: .95rem !important; }
        [data-testid="stMetric"] {
            background: var(--fr-panel);
            border: 1px solid var(--fr-border);
            border-radius: 4px;
            padding: 8px 10px;
        }
        [data-testid="stMetricLabel"] { color: var(--fr-muted); font-size: .69rem; text-transform: uppercase; letter-spacing: .06em; }
        [data-testid="stMetricValue"] { font-size: 1.05rem; font-family: "IBM Plex Mono", "JetBrains Mono", Consolas, monospace; }
        [data-baseweb="tab-list"] { gap: 18px; border-bottom: 1px solid var(--fr-border); }
        [data-baseweb="tab"] { height: 38px; padding: 0; font-size: .78rem; }
        [data-testid="stDataFrame"] { border: 1px solid var(--fr-border); border-radius: 4px; overflow: hidden; }
        .stButton > button, .stLinkButton > a, [data-testid="stPageLink"] a {
            border-radius: 4px;
            min-height: 34px;
            font-size: .76rem;
        }
        .stButton > button:hover, .stLinkButton > a:hover, [data-testid="stPageLink"] a:hover {
            border-color: var(--fr-cyan);
            color: #c8f3ff;
        }
        .stButton > button:focus-visible, .stLinkButton > a:focus-visible,
        input:focus-visible, select:focus-visible, textarea:focus-visible {
            outline: 2px solid var(--fr-cyan) !important;
            outline-offset: 2px !important;
        }
        .stSelectbox label, .stTextInput label { color: var(--fr-muted); font-size: .74rem; }
        .radar-commandbar {
            display: flex;
            align-items: center;
            gap: 10px;
            min-height: 38px;
            padding: 0 10px;
            margin: -4px 0 4px;
            background: var(--fr-panel);
            border: 1px solid var(--fr-border);
            border-radius: 4px;
        }
        .radar-brand { color: var(--fr-text); font-size: .73rem; font-weight: 800; letter-spacing: .14em; }
        .radar-divider { color: #496075; }
        .radar-title { color: var(--fr-text); font-size: .86rem !important; font-weight: 650; margin: 0; line-height: 1.2; }
        .radar-spacer { flex: 1; }
        .radar-subtitle { color: var(--fr-muted); font-size: .78rem; margin: 0 0 10px 2px; }
        .mode-badge {
            display: inline-block;
            border: 1px solid #225d76;
            color: #8bddf8;
            background: #0b2735;
            padding: 2px 7px;
            border-radius: 3px;
            font: 700 .65rem "IBM Plex Mono", Consolas, monospace;
            letter-spacing: .06em;
        }
        .safe-banner {
            border: 1px solid #1d4458;
            border-left: 3px solid var(--fr-cyan);
            background: #0a1d2a;
            padding: 6px 9px;
            margin-bottom: 8px;
            border-radius: 4px;
            color: #a8c8d7;
            font-size: .72rem;
            letter-spacing: .015em;
        }
        .status-strip {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
            border: 1px solid var(--fr-border);
            border-radius: 4px;
            background: var(--fr-panel);
            margin: 8px 0;
            overflow: hidden;
        }
        .status-item { padding: 8px 10px; border-right: 1px solid var(--fr-border); }
        .status-item:last-child { border-right: 0; }
        .status-label { color: var(--fr-muted); font-size: .65rem; text-transform: uppercase; letter-spacing: .06em; }
        .status-value { color: var(--fr-text); margin-top: 2px; font: 650 .86rem "IBM Plex Mono", "JetBrains Mono", Consolas, monospace; }
        .status-value.ok { color: var(--fr-green); }
        .status-value.watch { color: var(--fr-amber); }
        .status-value.risk { color: var(--fr-red); }
        .flow-bar {
            display: flex;
            align-items: center;
            gap: 5px;
            padding: 6px 0 8px;
            overflow-x: auto;
            scrollbar-width: thin;
        }
        .flow-bar-label {
            flex: 0 0 auto;
            color: var(--fr-muted);
            font: 700 .62rem ui-monospace, Consolas, monospace;
            letter-spacing: .08em;
            padding-right: 5px;
        }
        .flow-link {
            display: inline-flex;
            align-items: center;
            gap: 7px;
            flex: 0 0 auto;
            min-height: 30px;
            padding: 0 9px;
            border: 1px solid var(--fr-border);
            border-radius: 4px;
            background: var(--fr-panel);
            color: #c5d4df !important;
            font-size: .7rem;
            text-decoration: none !important;
        }
        .flow-link:hover, .flow-link:focus-visible {
            background: var(--fr-raised);
            border-color: var(--fr-border-strong);
            color: var(--fr-text) !important;
        }
        .flow-link.is-review { border-color: #5b4725; }
        .flow-link.is-verified { border-color: #245544; }
        .flow-count {
            color: var(--fr-text);
            font: 700 .67rem ui-monospace, Consolas, monospace;
        }
        .command-palette {
            display: flex;
            align-items: stretch;
            gap: 5px;
            padding: 0 0 8px;
            overflow-x: auto;
            scrollbar-width: thin;
        }
        .command-palette-label {
            display: inline-flex;
            align-items: center;
            flex: 0 0 auto;
            padding: 0 6px 0 1px;
            color: var(--fr-cyan);
            font: 750 .6rem ui-monospace, Consolas, monospace;
            letter-spacing: .09em;
        }
        .command-link {
            display: grid;
            grid-template-rows: auto auto;
            align-content: center;
            flex: 0 0 auto;
            min-width: 112px;
            min-height: 42px;
            padding: 4px 9px;
            border: 1px solid var(--fr-border);
            border-radius: 4px;
            background: linear-gradient(180deg, #0b1723 0%, var(--fr-panel) 100%);
            color: var(--fr-text) !important;
            text-decoration: none !important;
        }
        .command-link:hover, .command-link:focus-visible {
            background: var(--fr-raised);
            border-color: var(--fr-cyan);
        }
        .command-name { font-size: .7rem; font-weight: 680; line-height: 1.25; }
        .command-meta {
            margin-top: 2px;
            color: var(--fr-muted);
            font: 650 .54rem ui-monospace, Consolas, monospace;
            letter-spacing: .04em;
        }
        .score-rail { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; margin: 8px 0; }
        .score-cell { min-height: 52px; padding: 7px 8px; background: var(--fr-panel); border: 1px solid var(--fr-border); border-radius: 5px; }
        .score-label { color: var(--fr-muted); font-size: .62rem; text-transform: uppercase; letter-spacing: .06em; }
        .score-value { color: var(--fr-text); margin-top: 5px; font: 650 .75rem "IBM Plex Mono", Consolas, monospace; overflow-wrap: anywhere; }
        .score-value.ok { color: var(--fr-green); }
        .score-value.watch { color: var(--fr-amber); }
        .score-value.risk { color: var(--fr-red); }
        .score-value.evidence { color: var(--fr-purple); }
        .market-context { margin: 7px 0 9px; }
        .market-context-card {
            background: var(--fr-panel);
            border: 1px solid var(--fr-border);
            border-radius: 4px;
            padding: 8px 9px;
            margin-bottom: 6px;
        }
        .market-context-top { display: flex; align-items: baseline; gap: 7px; }
        .market-symbol { color: var(--fr-text); font: 750 .74rem ui-monospace, Consolas, monospace; }
        .market-provider { color: var(--fr-muted); font: .59rem ui-monospace, Consolas, monospace; text-transform: uppercase; }
        .market-price { color: var(--fr-text); font: 720 1rem ui-monospace, Consolas, monospace; margin: 5px 0 3px; }
        .market-meta { color: var(--fr-muted); font-size: .64rem; line-height: 1.42; }
        .market-horizons { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; margin-top: 7px; }
        .market-horizon { padding: 5px 6px; background: #08111b; border: 1px solid var(--fr-border); border-radius: 3px; min-width: 0; }
        .market-horizon-label { color: var(--fr-muted); font: 650 .55rem ui-monospace, Consolas, monospace; letter-spacing: .04em; }
        .market-horizon-value { color: #b7a6f7; margin-top: 3px; font: 720 .65rem ui-monospace, Consolas, monospace; overflow-wrap: anywhere; }
        .market-horizon-value.watch { color: var(--fr-amber); }
        .market-horizon-value.risk { color: var(--fr-red); }
        .market-freshness { color: var(--fr-muted); font: 700 .6rem ui-monospace, Consolas, monospace; margin-left: auto; }
        .market-freshness.ok { color: var(--fr-green); }
        .market-freshness.watch { color: var(--fr-amber); }
        .market-freshness.risk { color: var(--fr-red); }
        .market-unavailable { border-style: dashed; }
        .event-kicker { color: var(--fr-muted); font: .67rem "IBM Plex Mono", Consolas, monospace; text-transform: uppercase; letter-spacing: .05em; }
        .event-headline { color: var(--fr-text); font-size: 1.05rem; font-weight: 680; line-height: 1.32; margin: 4px 0 8px; }
        .event-summary { color: #c4d3df; font-size: .82rem; line-height: 1.55; }
        .evidence-card { background: var(--fr-panel); border: 1px solid var(--fr-border); border-left: 3px solid var(--fr-purple); border-radius: 5px; padding: 9px 10px; margin-bottom: 7px; }
        .evidence-meta { color: #b7a6f7; font: .65rem "IBM Plex Mono", Consolas, monospace; letter-spacing: .03em; }
        .evidence-passage { color: #d2dde6; font-size: .78rem; line-height: 1.5; margin-top: 6px; }
        .error-state { border: 1px solid #68323c; border-left: 3px solid var(--fr-red); background: #24131a; border-radius: 5px; padding: 10px 12px; margin: 8px 0; }
        .error-title { color: #ff9aaa; font-size: .82rem; font-weight: 750; }
        .error-copy { color: #d7bdc3; font-size: .76rem; line-height: 1.5; margin-top: 4px; }
        .error-ref { color: var(--fr-muted); font: .64rem "IBM Plex Mono", Consolas, monospace; margin-top: 6px; }
        div[data-testid="stVerticalBlockBorderWrapper"] { border-color: var(--fr-border); border-radius: 4px; }
        .terminal-section {
            display: flex;
            align-items: baseline;
            gap: 9px;
            padding: 2px 0 8px;
            border-bottom: 1px solid var(--fr-border);
            margin-bottom: 2px;
        }
        .terminal-section-title { color: var(--fr-text); font-size: .84rem !important; font-weight: 720; margin: 0; line-height: 1.25; }
        .terminal-section-meta { color: var(--fr-muted); font: .64rem ui-monospace, Consolas, monospace; }
        .feed-list { border: 1px solid var(--fr-border); border-radius: 4px; overflow: hidden; }
        .feed-row {
            display: grid;
            grid-template-columns: 58px minmax(0, 1fr);
            gap: 12px;
            padding: 10px 12px;
            color: inherit !important;
            text-decoration: none !important;
            background: var(--fr-panel);
            border-bottom: 1px solid var(--fr-border);
            transition: background .12s ease, border-color .12s ease;
        }
        .feed-row:last-child { border-bottom: 0; }
        .feed-row:hover, .feed-row:focus-visible { background: var(--fr-raised); }
        .feed-row:focus-visible { outline: 2px solid var(--fr-cyan); outline-offset: -2px; }
        .feed-time { color: #9eb2c3; font: 700 .68rem ui-monospace, Consolas, monospace; padding-top: 2px; }
        .feed-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-bottom: 4px; }
        .feed-tag {
            color: var(--fr-muted);
            font: 650 .58rem ui-monospace, Consolas, monospace;
            text-transform: uppercase;
            letter-spacing: .045em;
        }
        .feed-tag.status-verified { color: var(--fr-green); }
        .feed-tag.status-candidate, .feed-tag.status-weak { color: var(--fr-amber); }
        .feed-tag.status-rejected { color: var(--fr-red); }
        .feed-dot { color: #42596c; font-size: .6rem; }
        .feed-headline { color: var(--fr-text); font-size: .84rem; font-weight: 680; line-height: 1.35; }
        .feed-type { color: #a9bac8; font: .66rem ui-monospace, Consolas, monospace; margin-left: 5px; }
        .feed-summary {
            color: var(--fr-muted);
            font-size: .71rem;
            line-height: 1.45;
            margin-top: 4px;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }
        .queue-card {
            background: var(--fr-panel);
            border: 1px solid var(--fr-border);
            border-radius: 4px;
            padding: 12px;
            margin-bottom: 8px;
        }
        .queue-card-label { color: var(--fr-muted); font-size: .66rem; text-transform: uppercase; letter-spacing: .07em; }
        .queue-card-value { color: var(--fr-amber); font: 720 1.55rem ui-monospace, Consolas, monospace; margin: 3px 0; }
        .queue-card-copy { color: #b9c7d2; font-size: .72rem; line-height: 1.45; }
        .pulse-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; margin: 8px 0; }
        .pulse-cell { background: var(--fr-panel); border: 1px solid var(--fr-border); border-radius: 4px; padding: 8px 9px; }
        .pulse-label { color: var(--fr-muted); font-size: .61rem; text-transform: uppercase; letter-spacing: .06em; }
        .pulse-value { color: var(--fr-text); font: 700 .88rem ui-monospace, Consolas, monospace; margin-top: 4px; }
        .pulse-value.ok { color: var(--fr-green); }
        .pulse-value.watch { color: var(--fr-amber); }
        .pulse-value.risk { color: var(--fr-red); }
        .boundary-ok { color: var(--fr-green); border-left: 3px solid var(--fr-green); background: #0b251d; padding: 8px 10px; border-radius: 3px; font-size: .72rem; }
        @media (max-width: 900px) {
            .block-container { padding-left: .75rem; padding-right: .75rem; }
            .status-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .score-rail { grid-template-columns: 1fr; }
            .radar-subtitle { display: none; }
            .feed-row { grid-template-columns: 48px minmax(0, 1fr); gap: 8px; padding: 9px; }
            .flow-bar { margin-right: -.75rem; padding-right: .75rem; }
        }
        .warn-banner { border-left:3px solid var(--fr-amber); background:#2b210f; padding:6px 9px; border-radius:4px; }
        .small-muted { color: var(--fr-muted); font-size:.76rem; }
        @media (prefers-reduced-motion: reduce) {
            *, *::before, *::after { animation-duration: .01ms !important; transition-duration: .01ms !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(f"<style>{ACCESSIBILITY_CSS}</style>", unsafe_allow_html=True)
    st.markdown(f"<style>{DESIGN_TOKENS_V3}</style>", unsafe_allow_html=True)
    st.markdown(f"<style>{STYLE_V3}</style>", unsafe_allow_html=True)
    _install_accessibility_contract()


def _install_accessibility_contract() -> None:
    """Apply document-level semantics that Streamlit 1.59 does not emit."""
    global _accessibility_component
    mount_args = {
        "key": "finance-radar-accessibility-contract",
        "data": {"language": "zh-CN"},
        "height": 0,
        "width": "stretch",
    }
    try:
        _accessibility_component(**mount_args)
    except ValueError as exc:
        # Independent AppTest instances can reset the component registry while
        # imported modules remain cached. Re-register only for that test path.
        if "is not registered" not in str(exc):
            raise
        _accessibility_component = st.components.v2.component(
            "finance_radar_accessibility_contract",
            js=ACCESSIBILITY_JS,
        )
        _accessibility_component(**mount_args)


def header(title: str, subtitle: str, mode: str | None = None) -> None:
    mode_markup = f'<span class="mode-badge">{escape(mode)}</span>' if mode else ""
    st.markdown(
        '<div class="radar-commandbar">'
        '<span class="radar-brand-lockup">'
        '<span class="radar-brand-mark" aria-hidden="true">◎</span>'
        '<span class="radar-brand">FINANCE RADAR</span>'
        '</span>'
        '<span class="radar-divider">/</span>'
        f'<h1 class="radar-page-context">{escape(title)}</h1>'
        '<span class="radar-spacer"></span>'
        f'{mode_markup}'
        '</div>'
        f'<div class="radar-subtitle">{escape(subtitle)}</div>',
        unsafe_allow_html=True,
    )


def situation_brief(
    title: str,
    copy: str,
    *,
    focus_label: str,
    focus_value: Any,
    focus_state: str = "",
) -> None:
    """Render a single calm orientation block with one explicit priority."""
    safe_state = focus_state if focus_state in {"ok", "watch", "risk"} else ""
    st.markdown(
        '<section class="situation-brief" aria-labelledby="situation-brief-title">'
        '<div>'
        '<div class="situation-eyebrow">EVIDENCE DESK · 先证据，后判断</div>'
        f'<h2 class="situation-title" id="situation-brief-title">{escape(title)}</h2>'
        f'<p class="situation-copy">{escape(copy)}</p>'
        '</div>'
        '<div class="situation-focus" role="status">'
        f'<div class="situation-focus-label">{escape(focus_label)}</div>'
        f'<div class="situation-focus-value {safe_state}">{escape(str(focus_value))}</div>'
        '</div>'
        '</section>',
        unsafe_allow_html=True,
    )


def section_header(title: str, meta: str = "") -> None:
    st.markdown(
        '<div class="terminal-section">'
        f'<h2 class="terminal-section-title">{escape(title)}</h2>'
        f'<span class="terminal-section-meta">{escape(meta)}</span>'
        '</div>',
        unsafe_allow_html=True,
    )


def pulse_grid(items: list[tuple[str, Any, str]]) -> None:
    cells: list[str] = []
    for label, value, state in items:
        safe_state = state if state in {"ok", "watch", "risk"} else ""
        cells.append(
            '<div class="pulse-cell">'
            f'<div class="pulse-label">{escape(str(label))}</div>'
            f'<div class="pulse-value {safe_state}">{escape(str(value))}</div>'
            '</div>'
        )
    st.markdown(f'<div class="pulse-grid">{"".join(cells)}</div>', unsafe_allow_html=True)


def status_strip(items: list[tuple[str, Any, str]]) -> None:
    """Render compact operational facts; state is one of default/ok/watch/risk."""
    cells = []
    for label, value, state in items:
        safe_state = state if state in {"ok", "watch", "risk"} else ""
        cells.append(
            '<div class="status-item">'
            f'<div class="status-label">{escape(str(label))}</div>'
            f'<div class="status-value {safe_state}">{escape(str(value))}</div>'
            '</div>'
        )
    labels = ", ".join(f"{label}: {value}" for label, value, _ in items)
    st.markdown(
        f'<div class="status-strip" role="status" aria-label="{escape(labels)}">{"".join(cells)}</div>',
        unsafe_allow_html=True,
    )


def no_trading_banner() -> None:
    st.markdown(
        '<div class="safe-banner" role="note">情报与人工复核系统 · 只读行情 · 不含下单、仓位、余额或交易执行能力</div>',
        unsafe_allow_html=True,
    )


def api_error_descriptor(exc: Exception) -> tuple[str, str, str]:
    """Return user-facing outage copy and a non-secret diagnostic fingerprint."""
    message = str(exc)
    if "API 429" in message:
        title = "请求频率已受控"
        copy = "终端暂缓继续读取，避免放大上游压力。请稍后刷新；不会使用陈旧数据替代当前结果。"
    elif "API 401" in message or "API 403" in message:
        title = "只读接口拒绝访问"
        copy = "当前部署授权不完整。页面停止读取，且不会尝试其他账户、交易接口或降级数据。"
    else:
        title = "数据服务暂时不可用"
        copy = "页面已停止读取，不会把旧快照伪装成实时结果，也不会触发任何外部动作。请稍后刷新。"
    fingerprint = hashlib.sha256(message.encode("utf-8", errors="replace")).hexdigest()[:12]
    return title, copy, fingerprint


def render_api_error(exc: Exception) -> None:
    title, copy, fingerprint = api_error_descriptor(exc)
    st.markdown(
        '<div class="error-state" role="alert">'
        f'<div class="error-title">{escape(title)}</div>'
        f'<div class="error-copy">{escape(copy)}</div>'
        f'<div class="error-ref">error ref {fingerprint}</div>'
        '</div>',
        unsafe_allow_html=True,
    )
    if UI_ROLE == "admin" and SHOW_DEBUG:
        with st.expander("Developer diagnostics"):
            st.code(f"{type(exc).__name__}: {exc}", language=None)
            st.code(f"python -m uvicorn app.api.main:app --port 8000\nAPI target: {API_URL}", language=None)
