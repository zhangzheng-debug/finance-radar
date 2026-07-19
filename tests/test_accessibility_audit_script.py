from __future__ import annotations

from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "audit_public_accessibility.js"


def test_public_accessibility_audit_covers_all_product_pages_and_core_gates() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for route in (
        "Situation Room",
        "Event Intelligence",
        "Replay Lab",
        "Operations and Model",
        "Adjudication Studio",
    ):
        assert route in source
    for gate in (
        "main_landmark_count",
        "navigation_landmark_count",
        "missing_h1",
        "unnamed_interactive_controls",
        "keyboard_focus_visibility",
        "heading_level_skip",
        "horizontal_overflow",
    ):
        assert gate in source
    assert "wcag_aa_normal_text_contrast" in source
    assert "not a substitute for assistive-technology user testing" in source


def test_public_accessibility_audit_ignores_collapsed_off_canvas_touch_targets() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "rect.right <= 0 || rect.left >= window.innerWidth" in source
    assert "!item.offCanvas" in source
