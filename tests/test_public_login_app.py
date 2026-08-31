from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

import app.web.common as web_common


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "app" / "web" / "Home.py"


def test_public_reader_fails_closed_before_api_when_credential_is_missing(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def forbidden_api(path: str, **_kwargs):
        calls.append(path)
        raise AssertionError("public API called before login")

    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "PUBLIC_USERNAME", "")
    monkeypatch.setattr(web_common, "PUBLIC_PASSWORD_HASH", "")
    monkeypatch.setattr(web_common, "api_request", forbidden_api)

    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    rendered = "\n".join(str(item.value) for item in [*page.error, *page.caption])

    assert not page.exception
    assert not calls
    assert "公开访问凭据尚未配置" in rendered
