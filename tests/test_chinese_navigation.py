from __future__ import annotations

from pathlib import Path

from app.web.common import (
    ADMIN_NAVIGATION,
    OPERATOR_NAVIGATION,
    PRIMARY_NAVIGATION,
    PUBLIC_NAVIGATION,
    REVIEWER_NAVIGATION,
    navigation_for_role,
)


def test_public_navigation_is_small_chinese_and_route_stable() -> None:
    assert PRIMARY_NAVIGATION == PUBLIC_NAVIGATION
    assert [(item["key"], item["label"]) for item in PUBLIC_NAVIGATION] == [
        ("home", "事件雷达"),
        ("replay", "案例"),
        ("method", "方法"),
    ]
    assert [item["path"] for item in PUBLIC_NAVIGATION] == [
        "Home.py",
        "pages/2_Replay_Lab.py",
        "pages/5_Method_and_Boundaries.py",
    ]
    assert [item["url"] for item in PUBLIC_NAVIGATION] == [
        "./",
        "./Replay_Lab",
        "./Method_and_Boundaries",
    ]
    assert all(item["description"] for item in PUBLIC_NAVIGATION)
    assert len({item["key"] for item in PUBLIC_NAVIGATION}) == len(PUBLIC_NAVIGATION)


def test_admin_navigation_keeps_management_entrances_separate() -> None:
    assert [(item["key"], item["label"]) for item in ADMIN_NAVIGATION] == [
        ("admin_home", "管理概览"),
        ("events", "人工复核"),
        ("replay", "证据回放"),
        ("operations", "运行与模型"),
        ("adjudication", "双人盲审"),
        ("method", "方法与边界"),
    ]
    assert all(item["description"] for item in ADMIN_NAVIGATION)
    assert {"events", "operations", "adjudication"}.isdisjoint(
        {item["key"] for item in PUBLIC_NAVIGATION}
    )


def test_internal_role_navigation_separates_review_from_operations() -> None:
    reviewer_keys = {item["key"] for item in REVIEWER_NAVIGATION}
    operator_keys = {item["key"] for item in OPERATOR_NAVIGATION}
    assert {"events", "adjudication"} <= reviewer_keys
    assert "operations" not in reviewer_keys
    assert "operations" in operator_keys
    assert {"events", "adjudication"}.isdisjoint(operator_keys)
    assert navigation_for_role("reviewer") == REVIEWER_NAVIGATION
    assert navigation_for_role("operator") == OPERATOR_NAVIGATION


def test_primary_navigation_paths_exist() -> None:
    web_root = Path(__file__).resolve().parents[1] / "app" / "web"
    all_items = (*PUBLIC_NAVIGATION, *REVIEWER_NAVIGATION, *OPERATOR_NAVIGATION, *ADMIN_NAVIGATION)
    missing = [item["path"] for item in all_items if not (web_root / item["path"]).is_file()]
    assert missing == []


def test_internal_html_navigation_explicitly_stays_in_current_tab() -> None:
    web_root = Path(__file__).resolve().parents[1] / "app" / "web"
    common_source = (web_root / "common.py").read_text(encoding="utf-8")
    home_source = (web_root / "Home.py").read_text(encoding="utf-8")

    assert 'public-primary-link" href="{}" target="_self"' in common_source
    assert 'class="radar-primary-link{}" href="{}" target="_self"' in common_source
    assert 'href="./#live-events" target="_self"' in home_source
