from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".streamlit" / "config.toml"
STYLE = ROOT / "app" / "web" / "style_v3.css"
TOKENS = ROOT / "app" / "web" / "design_tokens_v3.css"


def test_streamlit_native_page_navigation_is_disabled_before_first_paint() -> None:
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    style = STYLE.read_text(encoding="utf-8")

    assert config["client"]["showSidebarNavigation"] is False
    assert '[data-testid="stSidebarNav"] { display: none !important; }' in style


def test_runtime_typography_uses_comfortable_body_and_touch_tokens() -> None:
    tokens = TOKENS.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")

    for token in (
        "--fr-type-body: 1rem;",
        "--fr-type-copy: .9375rem;",
        "--fr-type-small: .8125rem;",
        "--fr-type-label: .75rem;",
        "--fr-touch-target: 44px;",
        "--fr-focus-ring: #a9ecff;",
        "--fr-content-gutter: clamp(.85rem, 2.5vw, 2.5rem);",
    ):
        assert token in tokens

    assert re.search(r"html, body, \[class\*=" + '"css"' + r"\]\s*\{[^}]*font-size:\s*16px", style, re.S)
    assert "min-height: var(--fr-touch-target);" in style
    assert "text-transform: uppercase" not in style


def test_shell_has_explicit_layout_contracts_for_all_target_widths() -> None:
    tokens = TOKENS.read_text(encoding="utf-8")
    style = STYLE.read_text(encoding="utf-8")

    for width in (1100, 900, 620, 420):
        assert f"@media (max-width: {width}px)" in style
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in style
    assert ".mobile-scroll-cue" in style
    assert ".event-time-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }" in style
    assert "grid-template-areas:" in style
    assert '"time signal"' in style
    assert '"body body"' in style
    assert "width: min(20rem, 86vw) !important;" in style
    assert "overflow-wrap: anywhere;" in style
    assert "word-break: break-word;" in style
    assert "--fr-streamlit-header-height: 3.75rem;" in tokens
    assert "--fr-public-shell-gap: .75rem;" in tokens
    assert (
        "padding-top: calc(var(--fr-streamlit-header-height) + "
        "var(--fr-public-shell-gap));"
    ) in style
    assert ".public-reader-header span {" not in style
    assert ".public-reader-header > div > span {" in style
    assert '.public-reader-header [data-testid="stHeaderActionElements"]' in style
    assert ":has(> .public-reader-header)" in style
    assert "margin-bottom: 0 !important;" in style
    assert ".public-reader-header { display: none; }" not in style
    assert "scroll-margin-top: calc(var(--fr-streamlit-header-height) + 1rem);" in style


def test_accessibility_contract_covers_focus_contrast_and_reduced_motion() -> None:
    style = STYLE.read_text(encoding="utf-8")

    assert "outline: 3px solid var(--fr-focus-ring) !important;" in style
    assert "@media (prefers-reduced-motion: reduce)" in style
    assert "animation-duration: .01ms !important;" in style
    assert "animation-iteration-count: 1 !important;" in style
    assert ".fr-loading-state::after { display: none; }" in style
    assert "@media (prefers-contrast: more)" in style
    assert "outline-width: 4px !important;" in style
    assert "@media (forced-colors: active)" in style
    assert "outline: 3px solid Highlight !important;" in style


def test_future_public_states_have_namespaced_responsive_style_slots() -> None:
    style = STYLE.read_text(encoding="utf-8")

    for selector in (
        ".fr-loading-state",
        ".fr-empty-state",
        ".fr-state-title",
        ".fr-state-copy",
        ".fr-filter-panel",
        ".fr-pagination",
        ".fr-event-detail",
        ".fr-event-detail-header",
        ".fr-event-detail-main",
        ".fr-event-detail-aside",
    ):
        assert selector in style
    assert "@keyframes fr-loading-sheen" in style
    assert ".fr-filter-panel { grid-template-columns: 1fr;" in style
    assert ".fr-event-detail { grid-template-columns: 1fr; }" in style
