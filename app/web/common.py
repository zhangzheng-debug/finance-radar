from __future__ import annotations

import json
import os
import hashlib
import hmac
import ipaddress
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

from app.web.public_auth import (
    public_credential_fingerprint,
    verify_public_password,
)


API_URL = os.getenv("FINANCE_RADAR_API_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_TOKEN = os.getenv("FINANCE_RADAR_ADMIN_TOKEN")
REVIEWER_TOKEN = os.getenv("FINANCE_RADAR_REVIEWER_TOKEN")
OPERATOR_TOKEN = os.getenv("FINANCE_RADAR_OPERATOR_TOKEN")
SHOW_DEBUG = os.getenv("FINANCE_RADAR_SHOW_DEBUG") == "1"
PUBLIC_USERNAME = os.getenv("FINANCE_RADAR_PUBLIC_USERNAME", "").strip()
PUBLIC_PASSWORD_HASH = os.getenv("FINANCE_RADAR_PUBLIC_PASSWORD_HASH", "").strip()
UI_ROLES = frozenset({"public", "reviewer", "operator", "admin"})
_configured_ui_role = os.getenv("FINANCE_RADAR_UI_ROLE", "public").strip().lower()
UI_ROLE = _configured_ui_role if _configured_ui_role in UI_ROLES else "public"
DEEP_LINK_STATE_KEY = "_finance_radar_deep_link"
PUBLIC_AUTH_SESSION_KEY = "_finance_radar_public_auth_v1"
PUBLIC_AUTH_FAILURES_KEY = "_finance_radar_public_auth_failures_v1"
PUBLIC_AUTH_COOLDOWN_KEY = "_finance_radar_public_auth_cooldown_v1"
DESIGN_TOKENS_V3 = Path(__file__).with_name("design_tokens_v3.css").read_text(encoding="utf-8")
STYLE_V3 = Path(__file__).with_name("style_v3.css").read_text(encoding="utf-8")
PUBLIC_READER_V4 = Path(__file__).with_name("public_reader_v4.css").read_text(encoding="utf-8")


PUBLIC_LOGIN_CSS = """
[data-testid="stSidebar"], [data-testid="collapsedControl"] { display: none !important; }
[data-testid="stHeader"] { background: transparent !important; }
.stApp {
    background:
        radial-gradient(circle at 15% 10%, rgba(0, 127, 121, .10), transparent 31%),
        linear-gradient(135deg, #fcfcf8 0 48%, #f6f7f3 48% 100%) !important;
    color: #172029 !important;
}
.block-container { max-width: 1120px !important; padding: 4.5rem 2rem !important; }
.public-login-brand {
    display: flex; align-items: center; gap: 11px; margin-bottom: 4.6rem; color: #172029;
}
.public-login-mark {
    display: grid; width: 32px; height: 32px; place-items: center;
    color: #007f79; border: 1px solid currentColor; border-radius: 50%;
}
.public-login-brand strong { display: block; font-size: 13px; letter-spacing: .06em; }
.public-login-brand small { display: block; margin-top: 2px; color: #64717b; font-size: 11px; }
.public-login-story {
    min-height: 430px; padding: 42px; border: 1px solid #d9ded6; border-radius: 8px 0 0 8px;
    background:
        linear-gradient(145deg, rgba(0,127,121,.08), transparent 50%),
        repeating-linear-gradient(90deg, transparent 0 56px, rgba(0,127,121,.035) 56px 57px),
        #f0f3ed;
}
.public-login-kicker { color: #007f79; font-size: 12px; font-weight: 600; letter-spacing: .08em; }
.public-login-title {
    max-width: 560px; margin: 14px 0 18px; color: #172029;
    font: 400 clamp(2.8rem, 6vw, 4.7rem)/1.04 Georgia, "Noto Serif SC", "Songti SC", serif;
    letter-spacing: -.045em;
}
.public-login-copy { max-width: 560px; color: #64717b; line-height: 1.85; }
.public-login-route { display:flex; gap:22px; margin-top:72px; color:#64717b; font-size:11px; }
.public-login-route b { display:block; margin-bottom:4px; color:#007f79; font:600 11px ui-monospace,Consolas,monospace; }
.public-login-form-head { margin: 2.5rem 0 1.4rem; }
.public-login-form-head h2 { color: #172029; font: 400 1.8rem Georgia, "Noto Serif SC", "Songti SC", serif; }
.public-login-form-head p, .public-login-security { color: #64717b; font-size: .78rem; line-height: 1.7; }
.public-login-security { margin-top: 1.2rem; padding-top: 1rem; border-top: 1px solid #d9ded6; }
[data-testid="stForm"] { padding: 0 !important; background: transparent !important; border: 0 !important; }
[data-testid="stForm"] label { color: #64717b !important; }
[data-testid="stForm"] input {
    color: #172029 !important; background: #fff !important; border-color: #d9ded6 !important;
}
[data-testid="stForm"] button {
    min-height: 46px !important; color: #fff !important;
    background: linear-gradient(135deg, #008f88, #006d68) !important;
    border: 0 !important; border-radius: 6px !important;
    box-shadow: 0 10px 24px rgba(0,127,121,.16) !important;
}
[data-testid="stForm"] button:hover { transform: translateY(-1px); box-shadow: 0 13px 28px rgba(0,127,121,.22) !important; }
@media (max-width: 760px) {
    .block-container { padding: 1.25rem !important; }
    .public-login-brand { margin-bottom: 1.5rem; }
    .public-login-story { min-height: 310px; padding: 28px 22px; border-radius: 8px; }
    .public-login-route { margin-top: 38px; gap: 14px; }
    .public-login-form-head { margin-top: 1.4rem; }
}
""".strip()


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
        "label": "事件雷达",
        "description": "浏览事件与来源材料",
    },
    {
        "key": "replay",
        "path": "pages/2_Replay_Lab.py",
        "url": "./Replay_Lab",
        "label": "案例",
        "description": "查看精选事件案例",
    },
    {
        "key": "method",
        "path": "pages/5_Method_and_Boundaries.py",
        "url": "./Method_and_Boundaries",
        "label": "方法",
        "description": "了解数据与模型口径",
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


def public_auth_configured() -> bool:
    """Return whether the public Web credential contract is present."""

    return (
        3 <= len(PUBLIC_USERNAME) <= 64
        and PUBLIC_PASSWORD_HASH.startswith("pbkdf2_sha256$")
        and len(PUBLIC_PASSWORD_HASH) <= 512
    )


def require_public_login() -> None:
    """Fail closed before a public page performs API or model work."""

    if UI_ROLE != "public":
        return
    fingerprint = (
        public_credential_fingerprint(PUBLIC_USERNAME, PUBLIC_PASSWORD_HASH)
        if public_auth_configured()
        else ""
    )
    if fingerprint and hmac.compare_digest(
        str(st.session_state.get(PUBLIC_AUTH_SESSION_KEY) or ""), fingerprint
    ):
        return

    st.markdown(f"<style>{PUBLIC_LOGIN_CSS}</style>", unsafe_allow_html=True)
    st.markdown(
        '<div class="public-login-brand"><span class="public-login-mark">◎</span>'
        '<span><strong>FINANCE RADAR</strong>'
        '<small>Evidence-first financial intelligence</small></span></div>',
        unsafe_allow_html=True,
    )
    story, login = st.columns([1.16, 0.84], gap="large")
    with story:
        st.markdown(
            '<section class="public-login-story">'
            '<div class="public-login-kicker">受控访问 · 只读研究</div>'
            '<div class="public-login-title">把噪声留在门外</div>'
            '<p class="public-login-copy">登录后查看事件、来源材料、千问风险语义和消息发布后的市场反应。</p>'
            '<div class="public-login-route" aria-label="访问流程">'
            '<span><b>01</b>服务器校验</span><span><b>02</b>会话内访问</span>'
            '<span><b>03</b>安全退出</span></div></section>',
            unsafe_allow_html=True,
        )
    with login:
        st.markdown(
            '<div class="public-login-form-head"><h2>进入研究工作台</h2>'
            '<p>请输入访问凭据。</p></div>',
            unsafe_allow_html=True,
        )
        if not public_auth_configured():
            st.error("公开访问凭据尚未配置。")
            st.stop()

        now = time.monotonic()
        blocked_until = float(st.session_state.get(PUBLIC_AUTH_COOLDOWN_KEY) or 0.0)
        blocked = blocked_until > now
        with st.form("public_access_login", clear_on_submit=False, border=False):
            username = st.text_input("用户名", max_chars=64, autocomplete="username")
            password = st.text_input(
                "密码", type="password", max_chars=256, autocomplete="current-password"
            )
            submitted = st.form_submit_button(
                "验证并进入", use_container_width=True, disabled=blocked
            )
        if blocked:
            st.warning(f"请在 {max(1, int(blocked_until - now))} 秒后重试。")
        elif submitted:
            username_ok = hmac.compare_digest(
                username.strip().encode("utf-8"), PUBLIC_USERNAME.encode("utf-8")
            )
            password_ok = verify_public_password(password, PUBLIC_PASSWORD_HASH)
            if username_ok and password_ok:
                st.session_state[PUBLIC_AUTH_SESSION_KEY] = fingerprint
                st.session_state.pop(PUBLIC_AUTH_FAILURES_KEY, None)
                st.session_state.pop(PUBLIC_AUTH_COOLDOWN_KEY, None)
                st.rerun()
            failures = int(st.session_state.get(PUBLIC_AUTH_FAILURES_KEY) or 0) + 1
            if failures >= 5:
                st.session_state[PUBLIC_AUTH_FAILURES_KEY] = 0
                st.session_state[PUBLIC_AUTH_COOLDOWN_KEY] = now + 60.0
                st.error("用户名或密码不正确。登录已短暂暂停。")
            else:
                st.session_state[PUBLIC_AUTH_FAILURES_KEY] = failures
                st.error("用户名或密码不正确。")
        st.markdown(
            '<div class="public-login-security">◇ 凭据由服务器校验；验证通过前不会读取事件数据。</div>',
            unsafe_allow_html=True,
        )
    st.stop()


def _normalized_public_ip(value: object) -> str | None:
    """Return one usable visitor address, never a proxy/local placeholder."""
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return None
    if address.is_loopback or address.is_unspecified:
        return None
    return str(address)


def _context_header_values(headers: object, name: str) -> list[str]:
    """Read a possibly repeated Streamlit context header without ambiguity."""
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        try:
            raw_values = get_all(key=name)
        except TypeError:  # pragma: no cover - compatibility with mapping adapters
            raw_values = get_all(name)
        return [str(value).strip() for value in raw_values if str(value).strip()]
    get = getattr(headers, "get", None)
    value = get(name) if callable(get) else None
    return [str(value).strip()] if value not in (None, "") else []


def public_visitor_ip_for_api() -> str | None:
    """Return the public visitor IP that the loopback API may trust.

    A direct Streamlit peer address is preferred.  Behind production Nginx the
    peer is loopback (and ``st.context.ip_address`` is therefore unavailable),
    so accept the proxy headers only when Nginx's three overwritten values are
    present, singular and mutually consistent.  Internal role processes never
    forward browser-supplied address metadata.
    """
    if UI_ROLE != "public":
        return None
    try:
        direct = _normalized_public_ip(st.context.ip_address)
    except (AttributeError, RuntimeError):
        direct = None
    if direct:
        return direct
    try:
        headers = st.context.headers
    except (AttributeError, RuntimeError):
        return None
    real_values = _context_header_values(headers, "X-Real-IP")
    forwarded_values = _context_header_values(headers, "X-Forwarded-For")
    proto_values = _context_header_values(headers, "X-Forwarded-Proto")
    if len(real_values) != 1 or len(forwarded_values) != 1 or proto_values != ["https"]:
        return None
    real_ip = _normalized_public_ip(real_values[0])
    forwarded_ip = _normalized_public_ip(forwarded_values[0])
    return real_ip if real_ip is not None and real_ip == forwarded_ip else None


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

    require_public_login()

    links = "".join(
        (
            '<a class="radar-primary-link{} public-primary-link" href="{}" target="_self"{}>'
            '<span>{}</span></a>'
            if UI_ROLE == "public"
            else '<a class="radar-primary-link{}" href="{}" target="_self"{}>'
            '<span>{}</span><small>{}</small></a>'
        ).format(
            " is-active" if item["key"] == active else "",
            escape(item["url"], quote=True),
            ' aria-current="page"' if item["key"] == active else "",
            escape(item["label"]),
            *(() if UI_ROLE == "public" else (escape(item["description"]),)),
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
        brand = (
            '<div class="radar-sidebar-brand">'
            '<span class="radar-sidebar-mark" aria-hidden="true">◎</span>'
            '<div><strong>FINANCE RADAR</strong><span>风险事件研究</span></div>'
            '</div>'
        )
        navigation_markup = (
            f'<div class="radar-sidebar-section">{role_section}</div>'
            '<nav class="radar-primary-nav" aria-label="主要页面">'
            f'{links}'
            '</nav>'
        )
        if UI_ROLE == "public":
            st.markdown(brand + navigation_markup, unsafe_allow_html=True)
            st.markdown(
                '<div class="radar-sidebar-boundary"><span aria-hidden="true">●</span> '
                '只读事件研究</div>',
                unsafe_allow_html=True,
            )
            if st.button("安全退出", key="public_access_logout", use_container_width=True):
                st.session_state.pop(PUBLIC_AUTH_SESSION_KEY, None)
                st.rerun()
        else:
            st.markdown(
                brand
                + navigation_markup
                + '<div class="radar-sidebar-current">'
                '<span>当前工作面</span>'
                f'<strong>{escape(current["label"])}</strong>'
                f'<p>{escape(current["description"])}</p>'
                '</div>'
                '<div class="radar-sidebar-boundary">'
                '<span aria-hidden="true">◈</span> '
                '内部最小权限 · 操作留痕 · 不触发交易'
                '</div>',
                unsafe_allow_html=True,
            )


def api_request(
    path: str,
    *,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    timeout_seconds: int = 20,
    reviewer_credential: str | None = None,
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
    public_visitor_ip = public_visitor_ip_for_api()
    if public_visitor_ip is not None:
        headers["X-Real-IP"] = public_visitor_ip
    if reviewer_credential is not None:
        if UI_ROLE not in {"reviewer", "admin"}:
            raise ApiError("当前界面角色不允许使用人工审核凭据")
        normalized_credential = reviewer_credential.strip()
        if len(normalized_credential) < 24:
            raise ApiError("人工审核凭据无效")
        headers["X-Reviewer-Token"] = normalized_credential
    elif UI_ROLE == "reviewer" and REVIEWER_TOKEN:
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
    if UI_ROLE == "public":
        st.markdown(f"<style>{PUBLIC_READER_V4}</style>", unsafe_allow_html=True)
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
