from __future__ import annotations

from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "audit_public_accessibility.js"


def test_accessibility_audit_separates_public_and_tunnel_scoped_admin_pages() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for route in (
        "Situation Room",
        "Replay Lab",
        "Method and Boundaries",
        "Admin Overview",
        "Event Intelligence",
        "Operations and Model",
        "Adjudication Studio",
    ):
        assert route in source
    assert 'argument("scope", "public")' in source
    assert "targetsByScope" in source
    assert "--scope must be public or admin" in source
    assert "Nginx route" in source
    assert "authenticated loopback tunnel" in source
    assert "limitations: \"machine accessibility audit" in source
    assert 'scope: "machine accessibility audit' not in source


def test_accessibility_audit_keeps_core_gates_for_each_scoped_run() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for gate in (
        "main_landmark_count",
        "navigation_landmark_count",
        "missing_h1",
        "unnamed_interactive_controls",
        "keyboard_focus_visibility",
        "heading_level_skip",
        "horizontal_overflow",
        "public_shell_layout",
    ):
        assert gate in source
    assert "wcag_aa_normal_text_contrast" in source
    assert "not a substitute for assistive-technology user testing" in source
    for width in (1920, 1440, 1366, 1280, 1024, 901, 900, 621, 620, 421, 420, 390):
        assert f"width: {width}" in source
    assert "public_header_clearance" in source
    assert "following_content_gap" in source
    assert "title_font_size_px" in source


def test_public_accessibility_audit_ignores_collapsed_off_canvas_touch_targets() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "rect.right <= 0 || rect.left >= window.innerWidth" in source
    assert "!item.offCanvas" in source
