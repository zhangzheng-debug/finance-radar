from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import app.web.common as web_common


ROOT = Path(__file__).resolve().parents[1]
INTERNAL_PAGES = (
    ROOT / "app" / "web" / "pages" / "1_Event_Intelligence.py",
    ROOT / "app" / "web" / "pages" / "3_Operations_and_Model.py",
    ROOT / "app" / "web" / "pages" / "4_Adjudication_Studio.py",
)


@pytest.mark.parametrize("page_path", INTERNAL_PAGES, ids=lambda path: path.stem)
def test_internal_page_stops_before_any_api_read(monkeypatch, page_path: Path) -> None:
    api_calls: list[str] = []

    def forbidden_api(path: str, **kwargs):
        api_calls.append(path)
        raise AssertionError("public role reached an internal API call")

    monkeypatch.setattr(web_common, "UI_ROLE", "public")
    monkeypatch.setattr(web_common, "api_request", forbidden_api)

    page = AppTest.from_file(str(page_path), default_timeout=10).run()
    rendered = "\n".join(str(item.value) for item in [*page.error, *page.caption])

    assert not page.exception
    assert not api_calls
    assert "仅限内部管理环境" in rendered
    assert "不会开放复核写入、运行控制、模型治理或盲审工具" in rendered
