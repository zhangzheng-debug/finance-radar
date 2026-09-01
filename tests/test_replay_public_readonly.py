from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from typing import Any

from streamlit.testing.v1 import AppTest

import app.web.common as web_common


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "app" / "web" / "pages" / "2_Replay_Lab.py"
CASES = ROOT / "replay" / "cases" / "cases.json"


def _page_tree() -> ast.Module:
    return ast.parse(PAGE.read_text(encoding="utf-8"))


def _presentation_steps_function():
    tree = _page_tree()
    selected: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = {
                target.id
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if isinstance(target, ast.Name)
            }
            if "_RISK_TERMS" in names:
                selected.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_presentation_steps":
            selected.append(node)
    namespace: dict[str, Any] = {"Any": Any}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(PAGE), "exec"), namespace)
    return namespace["_presentation_steps"]


def test_public_replay_page_has_no_write_or_history_controls() -> None:
    source = PAGE.read_text(encoding="utf-8")
    tree = _page_tree()
    api_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "api_request"
    ]

    assert len(api_calls) == 1
    assert not api_calls[0].keywords
    assert isinstance(api_calls[0].args[0], ast.Constant)
    assert api_calls[0].args[0].value == "/api/v1/replays"
    for forbidden in (
        'method="POST"',
        "/run",
        "/reset",
        "清空历史",
        "recent_runs",
        "run_id",
        "model_version",
        "same_downstream_router",
        "fixture",
    ):
        assert forbidden not in source


def test_readonly_presentation_is_deterministic_for_all_frozen_cases() -> None:
    presentation_steps = _presentation_steps_function()
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    expected = {
        "sec_bankruptcy_verified": ["ABSTAIN", "RISK_REVIEW"],
        "positive_earnings_non_target": ["NON_TARGET"],
        "rumor_correction_abstain": ["ABSTAIN", "ABSTAIN"],
        "sec_filing_corrected_abstain": ["RISK_REVIEW", "ABSTAIN"],
    }

    for case in cases:
        original = copy.deepcopy(case)
        first = presentation_steps(case)
        second = presentation_steps(case)
        assert first == second
        assert case == original
        assert [step["decision"] for step in first] == expected[case["case_id"]]
        assert [step["seconds"] for step in first] == sorted(
            observation["at_seconds"] for observation in case["observations"]
        )


def test_public_replay_states_its_readonly_boundary_once() -> None:
    source = PAGE.read_text(encoding="utf-8")
    boundary = "冻结案例仅用于说明证据变化；页面不写入生产数据。"
    assert source.count(boundary) == 1
    assert "status_strip" not in source
    assert "no_trading_banner" not in source


def test_public_replay_has_a_chinese_first_screen_and_secondary_raw_evidence() -> None:
    source = PAGE.read_text(encoding="utf-8")
    for copy in (
        '<div><span>FINANCE RADAR</span><h1>案例</h1></div>',
        'case = st.selectbox("选择案例"',
        'restart_col.button("第一步"',
        'next_col.button("下一步"',
        'all_col.button("完整过程"',
        'with st.expander("原文", expanded=False)',
    ):
        assert copy in source

    assert "READ-ONLY DEMO" not in source
    assert 'st.write(observation.get("passage")' in source
    assert source.index('with st.expander("原文"') < source.index(
        'st.write(observation.get("passage")'
    )


def test_completed_demo_has_an_explicit_terminal_state_and_session_only_restart() -> None:
    source = PAGE.read_text(encoding="utf-8")
    for copy in (
        'restart_col.button("第一步"',
        'st.success(f"案例结论：',
        'st.session_state[visible_key] = 1',
    ):
        assert copy in source

    assert "演示完成：最终判断为" not in source
    assert "此刻系统应该怎样做" not in source
    assert "session_state.pop" not in source


def test_replay_progress_strip_uses_the_same_visible_step_as_the_conclusion(monkeypatch) -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    monkeypatch.setattr(web_common, "api_request", lambda path: {"items": cases})

    page = AppTest.from_file(str(PAGE), default_timeout=10).run()
    assert not page.exception
    next_button = next(button for button in page.button if button.label == "下一步")
    next_button.click().run()

    rendered = "\n".join(str(item.value) for item in [*page.markdown, *page.caption])
    assert not page.exception
    assert "证据节点 2/2" in rendered
    assert any("案例结论：高风险信号" in str(item.value) for item in page.success)
