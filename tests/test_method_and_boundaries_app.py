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
    assert "核心流程" in rendered
    assert "自动发现" in rendered
    assert "风险排序" in rendered
    assert "价格审计" in rendered
    assert "材料层级" in rendered
    assert "时间" in rendered
    assert "AI 解读" in rendered
    assert "只读事件研究工具" in rendered
    assert "千问模型结合人工金标" not in rendered
    assert "风险信号仅在有效结果存在时显示" in rendered
    for label in ("原文支持", "一手来源", "来源已收录", "事件记录"):
        assert label in rendered
    assert "数据更新时间" in rendered
    assert "系统发现" not in rendered
    assert "待核验" not in rendered
    assert "已粗审" not in rendered
    assert "正式处置状态" not in rendered
    assert "核验引用证据 ID" not in rendered
    assert "SQLite" not in rendered
    assert "Worker" not in rendered
    assert "/opt/" not in rendered
