from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "app" / "web" / "pages" / "5_Method_and_Boundaries.py"


def test_public_method_page_explains_evidence_without_internal_details() -> None:
    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    rendered = "\n".join(
        str(item.value)
        for item in [*page.markdown, *page.info, *page.warning, *page.caption]
    )
    assert not page.exception
    assert "先看证据，再形成判断" in rendered
    assert "来源" in rendered
    assert "时间" in rendered
    assert "置信度" in rendered
    assert "不提供投资建议" in rendered
    assert "公开界面是只读的" in rendered
    assert "系统发现时间" in rendered
    assert "正式处置状态" in rendered
    assert "核验引用证据 ID" in rendered
    assert "内部运行细节" in rendered
    assert "SQLite" not in rendered
    assert "Worker" not in rendered
    assert "/opt/" not in rendered
