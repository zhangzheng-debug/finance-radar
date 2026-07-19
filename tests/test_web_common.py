from __future__ import annotations

from app.web.common import ACCESSIBILITY_CSS, ACCESSIBILITY_JS, api_error_descriptor


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


def test_accessibility_contract_sets_landmarks_language_focus_and_targets() -> None:
    assert 'setAttribute("lang", "zh-CN")' in ACCESSIBILITY_JS
    assert 'setAttribute("role", "main")' in ACCESSIBILITY_JS
    assert 'setAttribute("role", "navigation")' in ACCESSIBILITY_JS
    assert "MutationObserver" in ACCESSIBILITY_JS
    assert ":focus-visible" in ACCESSIBILITY_CSS
    assert "outline: 2px solid var(--fr-cyan)" in ACCESSIBILITY_CSS
    assert "min-height: 44px" in ACCESSIBILITY_CSS
    assert 'a[aria-label="Link to heading"]' in ACCESSIBILITY_CSS
