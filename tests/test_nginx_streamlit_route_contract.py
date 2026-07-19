from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_nested_streamlit_core_routes_are_canonicalized() -> None:
    for relative in (
        "deployment/systemd/nginx-radar-direct.conf",
        "deployment/systemd/nginx-radar-locations.conf",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for page in (
            "Event_Intelligence",
            "Replay_Lab",
            "Operations_and_Model",
            "Adjudication_Studio",
        ):
            assert page in source
        assert "_stcore/(.*)" in source
        assert "/radar/_stcore/$1 break" in source
        assert "proxy_pass http://127.0.0.1:18501" in source
        assert 'proxy_set_header Upgrade $http_upgrade' in source
        assert 'proxy_set_header Connection "upgrade"' in source
        assert "return 302 /radar/?_page=$1&$args" in source
