from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.web.common as web_common
from app.web.common import (
    ACCESSIBILITY_CSS,
    ACCESSIBILITY_JS,
    DESIGN_TOKENS_V3,
    STYLE_V3,
    ApiError,
    api_request,
    api_error_descriptor,
    format_elapsed,
)


class _JsonResponse:
    def __enter__(self) -> "_JsonResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps({"data": {"ok": True}}).encode("utf-8")


class _RawResponse(_JsonResponse):
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class _ContextHeaders(dict[str, object]):
    def get_all(self, *, key: str) -> list[object]:
        value = self.get(key)
        if value is None:
            return []
        return list(value) if isinstance(value, list) else [value]


def test_api_error_descriptor_is_safe_and_deterministic() -> None:
    exc = RuntimeError("API unavailable at http://internal:8000: secret-ish diagnostic")
    first = api_error_descriptor(exc)
    second = api_error_descriptor(exc)
    assert first == second
    title, copy, fingerprint = first
    assert title == "数据服务暂时不可用"
    assert "旧快照" in copy
    assert "internal" not in title + copy
    assert len(fingerprint) == 12


def test_api_error_descriptor_distinguishes_rate_limit_and_auth() -> None:
    rate_title, _, _ = api_error_descriptor(RuntimeError("API 429: bounded"))
    auth_title, _, _ = api_error_descriptor(RuntimeError("API 403: denied"))
    assert rate_title == "请求频率已受控"
    assert auth_title == "只读接口拒绝访问"


def test_format_elapsed_uses_a_human_scale_without_hiding_age() -> None:
    assert format_elapsed(None) == "—"
    assert format_elapsed(30) == "30 秒"
    assert format_elapsed(90) == "1.5 分钟"
    assert format_elapsed(7200) == "2.0 小时"
    assert format_elapsed(172800) == "2.0 天"


def test_accessibility_contract_sets_landmarks_language_focus_and_targets() -> None:
    assert 'setAttribute("lang", "zh-CN")' in ACCESSIBILITY_JS
    assert 'setAttribute("role", "main")' in ACCESSIBILITY_JS
    assert 'setAttribute("role", "navigation")' not in ACCESSIBILITY_JS
    assert 'removeAttribute("role")' in ACCESSIBILITY_JS
    assert "MutationObserver" in ACCESSIBILITY_JS
    assert ":focus-visible" in ACCESSIBILITY_CSS
    assert "outline: 2px solid var(--fr-cyan)" in ACCESSIBILITY_CSS
    assert "min-height: 44px" in ACCESSIBILITY_CSS
    assert 'a[aria-label="Link to heading"]' in ACCESSIBILITY_CSS


def test_shared_shell_has_one_primary_navigation_landmark_and_a_page_h1() -> None:
    common_source = (Path(__file__).parents[1] / "app" / "web" / "common.py").read_text(
        encoding="utf-8"
    )
    components_source = (
        Path(__file__).parents[1] / "app" / "web" / "components.py"
    ).read_text(encoding="utf-8")
    home_source = (Path(__file__).parents[1] / "app" / "web" / "Home.py").read_text(
        encoding="utf-8"
    )
    assert '<nav class="radar-primary-nav"' in common_source
    assert "f'<h1 class=\"radar-page-context\">{escape(title)}</h1>'" in common_source
    assert "f'<h2 class=\"situation-title\"" in common_source
    assert 'role="navigation"' not in components_source
    assert '<div class="fr-pagination {placement}" role="group"' in home_source


def test_public_navigation_does_not_call_stale_data_realtime() -> None:
    home = next(item for item in web_common.PUBLIC_NAVIGATION if item["key"] == "home")
    assert home["label"] == "事件雷达"
    assert home["description"] == "浏览事件与来源材料"
    assert "实时" not in home["description"]


def test_public_auth_configuration_fails_closed_without_both_values(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "PUBLIC_USERNAME", "")
    monkeypatch.setattr(web_common, "PUBLIC_PASSWORD_HASH", "")
    assert web_common.public_auth_configured() is False
    monkeypatch.setattr(web_common, "PUBLIC_USERNAME", "radar-admin")
    assert web_common.public_auth_configured() is False
    monkeypatch.setattr(
        web_common,
        "PUBLIC_PASSWORD_HASH",
        "pbkdf2_sha256$600000$c2FsdHNhbHRzYWx0c2FsdA$ZGlnZXN0",
    )
    assert web_common.public_auth_configured() is True


def test_v3_runtime_tokens_are_single_source_and_styles_consume_them() -> None:
    assert "--fr-text-2: #b5c3d1" in DESIGN_TOKENS_V3
    assert "--fr-muted: #8ea1b4" in DESIGN_TOKENS_V3
    assert ":root" not in STYLE_V3
    assert "var(--fr-text-2)" in STYLE_V3


def test_public_api_requests_never_attach_admin_token(monkeypatch) -> None:
    captured: list[object] = []

    def fake_urlopen(request: object, *, timeout: int) -> _JsonResponse:
        assert timeout == 20
        captured.append(request)
        return _JsonResponse()

    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "ADMIN_TOKEN", "must-not-leave-process")
    monkeypatch.setattr(web_common.urllib.request, "urlopen", fake_urlopen)
    assert api_request("/api/v1/overview") == {"ok": True}
    assert captured
    assert captured[0].get_header("X-admin-token") is None


def test_public_api_forwards_valid_streamlit_peer_ip_to_loopback_api(monkeypatch) -> None:
    captured: list[object] = []

    def fake_urlopen(request: object, *, timeout: int) -> _JsonResponse:
        captured.append(request)
        return _JsonResponse()

    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(
        web_common.st,
        "context",
        SimpleNamespace(ip_address="2001:db8::10", headers=_ContextHeaders()),
    )
    monkeypatch.setattr(web_common.urllib.request, "urlopen", fake_urlopen)

    assert api_request("/api/v1/overview") == {"ok": True}
    assert captured[0].get_header("X-real-ip") == "2001:db8::10"


def test_public_api_accepts_only_consistent_overwritten_proxy_ip_headers(monkeypatch) -> None:
    captured: list[object] = []

    def fake_urlopen(request: object, *, timeout: int) -> _JsonResponse:
        captured.append(request)
        return _JsonResponse()

    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(
        web_common.st,
        "context",
        SimpleNamespace(
            ip_address=None,
            headers=_ContextHeaders(
                {
                    "X-Real-IP": "203.0.113.17",
                    "X-Forwarded-For": "203.0.113.17",
                    "X-Forwarded-Proto": "https",
                }
            ),
        ),
    )
    monkeypatch.setattr(web_common.urllib.request, "urlopen", fake_urlopen)

    assert api_request("/api/v1/overview") == {"ok": True}
    assert captured[0].get_header("X-real-ip") == "203.0.113.17"

    captured.clear()
    web_common.st.context.headers["X-Forwarded-For"] = "198.51.100.8"
    assert api_request("/api/v1/overview") == {"ok": True}
    assert captured[0].get_header("X-real-ip") is None


def test_internal_ui_never_forwards_browser_address_metadata(monkeypatch) -> None:
    captured: list[object] = []

    def fake_urlopen(request: object, *, timeout: int) -> _JsonResponse:
        captured.append(request)
        return _JsonResponse()

    monkeypatch.setattr(web_common, "UI_ROLE", "reviewer")
    monkeypatch.setattr(
        web_common.st,
        "context",
        SimpleNamespace(
            ip_address="203.0.113.20",
            headers=_ContextHeaders(
                {
                    "X-Real-IP": "198.51.100.9",
                    "X-Forwarded-For": "198.51.100.9",
                    "X-Forwarded-Proto": "https",
                }
            ),
        ),
    )
    monkeypatch.setattr(web_common.urllib.request, "urlopen", fake_urlopen)

    assert api_request("/api/v1/overview") == {"ok": True}
    assert captured[0].get_header("X-real-ip") is None


def test_admin_api_request_can_attach_configured_token(monkeypatch) -> None:
    captured: list[object] = []

    def fake_urlopen(request: object, *, timeout: int) -> _JsonResponse:
        captured.append(request)
        return _JsonResponse()

    monkeypatch.setattr(web_common, "UI_ROLE", "admin")
    monkeypatch.setattr(web_common, "ADMIN_TOKEN", "internal-only")
    monkeypatch.setattr(web_common.urllib.request, "urlopen", fake_urlopen)
    assert api_request("/api/v1/overview") == {"ok": True}
    assert captured[0].get_header("X-admin-token") == "internal-only"


def test_personal_reviewer_credential_overrides_static_and_admin_tokens(monkeypatch) -> None:
    captured: list[object] = []

    def fake_urlopen(request: object, *, timeout: int) -> _JsonResponse:
        captured.append(request)
        return _JsonResponse()

    monkeypatch.setattr(web_common, "UI_ROLE", "admin")
    monkeypatch.setattr(web_common, "REVIEWER_TOKEN", "shared-static-token")
    monkeypatch.setattr(web_common, "ADMIN_TOKEN", "admin-token")
    monkeypatch.setattr(web_common.urllib.request, "urlopen", fake_urlopen)
    assert api_request(
        "/api/v1/adjudication/status",
        reviewer_credential="personal-reviewer-credential",
    ) == {"ok": True}
    assert captured[0].get_header("X-reviewer-token") == "personal-reviewer-credential"
    assert captured[0].get_header("X-admin-token") is None


def test_public_ui_rejects_personal_reviewer_credential_before_network(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(
        web_common.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("reviewer credential reached the network"),
    )
    with pytest.raises(ApiError, match="角色不允许"):
        api_request(
            "/api/v1/adjudication/status",
            reviewer_credential="personal-reviewer-credential",
        )


def test_cached_api_get_is_bounded_by_ttl_and_returns_defensive_copies(monkeypatch) -> None:
    calls = 0

    def fake_api(path: str, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"path": path, "items": ["original"]}

    monkeypatch.setattr(web_common, "api_request", fake_api)
    web_common.clear_api_get_cache()
    first, first_meta = web_common.cached_api_get("/api/v1/overview", ttl_seconds=30)
    first["items"].append("caller mutation")
    second, second_meta = web_common.cached_api_get("/api/v1/overview", ttl_seconds=30)

    assert calls == 1
    assert first_meta.cache_hit is False
    assert second_meta.cache_hit is True
    assert second_meta.stale is False
    assert second["items"] == ["original"]


def test_cached_api_get_uses_only_explicitly_aged_stale_snapshot_on_error(monkeypatch) -> None:
    should_fail = False

    def fake_api(_path: str, **_kwargs: object) -> dict[str, object]:
        if should_fail:
            raise web_common.ApiError("refresh unavailable")
        return {"value": "known snapshot"}

    clock = iter([100.0, 100.0, 102.0, 102.0])
    monkeypatch.setattr(web_common, "api_request", fake_api)
    monkeypatch.setattr(web_common.time, "monotonic", lambda: next(clock))
    web_common.clear_api_get_cache()
    web_common.cached_api_get("/api/v1/overview", ttl_seconds=1, stale_if_error_seconds=5)
    should_fail = True
    data, metadata = web_common.cached_api_get(
        "/api/v1/overview", ttl_seconds=1, stale_if_error_seconds=5
    )

    assert data == {"value": "known snapshot"}
    assert metadata.cache_hit is True
    assert metadata.stale is True
    assert metadata.age_seconds == 2.0


def test_home_renders_public_shell_before_overview_without_internal_kpis() -> None:
    home_source = (Path(__file__).parents[1] / "app" / "web" / "Home.py").read_text(
        encoding="utf-8"
    )
    assert home_source.index('class="public-reader-header"') < home_source.index(
        'cached_api_get(\n        "/api/v1/overview"'
    )
    assert '"/api/v1/product/metrics"' not in home_source


@pytest.mark.parametrize(
    ("role", "token_name", "header_name", "path", "method"),
    [
        ("reviewer", "REVIEWER_TOKEN", "X-reviewer-token", "/api/v1/events/e1/human-override", "POST"),
        ("operator", "OPERATOR_TOKEN", "X-operator-token", "/api/v1/events/e1/agent/run", "POST"),
    ],
)
def test_scoped_ui_attaches_only_its_own_token(
    monkeypatch, role: str, token_name: str, header_name: str, path: str, method: str
) -> None:
    captured: list[object] = []

    def fake_urlopen(request: object, *, timeout: int) -> _JsonResponse:
        captured.append(request)
        return _JsonResponse()

    monkeypatch.setattr(web_common, "UI_ROLE", role)
    monkeypatch.setattr(web_common, token_name, "scoped-secret")
    monkeypatch.setattr(web_common, "ADMIN_TOKEN", "must-stay-private")
    monkeypatch.setattr(web_common.urllib.request, "urlopen", fake_urlopen)
    assert api_request(path, method=method) == {"ok": True}
    assert captured[0].get_header(header_name) == "scoped-secret"
    assert captured[0].get_header("X-admin-token") is None


def test_reviewer_cannot_run_operator_action_before_network(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "UI_ROLE", "reviewer")
    monkeypatch.setattr(
        web_common.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("cross-role write reached the network"),
    )
    with pytest.raises(ApiError, match="角色不允许"):
        api_request("/api/v1/events/e1/agent/run", method="POST")


def test_public_api_rejects_writes_before_network_access(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(
        web_common.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("public write reached the network"),
    )
    with pytest.raises(ApiError, match="只允许只读请求"):
        api_request("/api/v1/demo/mode/LIVE", method="POST")


def test_public_transport_errors_do_not_expose_internal_target(monkeypatch) -> None:
    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "API_URL", "http://private-api:18000")
    monkeypatch.setattr(
        web_common.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.URLError("secret internal path /srv/radar")
        ),
    )
    with pytest.raises(ApiError) as caught:
        api_request("/api/v1/overview")
    message = str(caught.value)
    assert "private-api" not in message
    assert "/srv/radar" not in message
    assert "secret" not in message


@pytest.mark.parametrize("payload", [["unexpected"], {"error": "internal /srv/path"}, {}])
def test_malformed_api_envelopes_fail_closed_without_echoing_payload(
    monkeypatch, payload: object
) -> None:
    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(
        web_common.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _RawResponse(payload),
    )
    with pytest.raises(ApiError) as caught:
        api_request("/api/v1/overview")
    assert "/srv/path" not in str(caught.value)
    assert "internal" not in str(caught.value)


def test_require_admin_ui_stops_public_render(monkeypatch) -> None:
    rendered: list[str] = []

    class Stopped(RuntimeError):
        pass

    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common.st, "error", lambda value: rendered.append(str(value)))
    monkeypatch.setattr(web_common.st, "caption", lambda value: rendered.append(str(value)))
    monkeypatch.setattr(web_common.st, "stop", lambda: (_ for _ in ()).throw(Stopped()))
    with pytest.raises(Stopped):
        web_common.require_admin_ui()
    assert any("仅限内部管理环境" in value for value in rendered)
