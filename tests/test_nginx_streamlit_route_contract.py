from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_public_streamlit_declares_its_chinese_content_language() -> None:
    source = (ROOT / "deployment/systemd/nginx-radar-direct.conf").read_text(
        encoding="utf-8"
    )

    assert 'add_header Content-Language "zh-CN" always;' in source


def test_only_public_nested_streamlit_routes_are_canonicalized() -> None:
    for relative in (
        "deployment/systemd/nginx-radar-direct.conf",
        "deployment/systemd/nginx-radar-locations.conf",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for page in ("Replay_Lab", "Method_and_Boundaries"):
            assert page in source
        assert "_stcore/(.*)" in source
        assert "/radar/_stcore/$1 break" in source
        assert "proxy_pass http://127.0.0.1:18501" in source
        assert 'proxy_set_header Upgrade $http_upgrade' in source
        assert 'proxy_set_header Connection "upgrade"' in source
        assert "proxy_set_header X-Forwarded-For $remote_addr" in source
        assert "$proxy_add_x_forwarded_for" not in source
        assert "return 302 /radar/?_page=$1&$args" in source


def test_internal_pages_and_backend_are_denied_at_public_edge() -> None:
    for relative in (
        "deployment/systemd/nginx-radar-direct.conf",
        "deployment/systemd/nginx-radar-locations.conf",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        internal_group = "Event_Intelligence|Operations_and_Model|Adjudication_Studio"
        assert f"^/radar/({internal_group})(?:/|$)" in source
        assert f"$arg__page ~ ^({internal_group})$" in source
        assert "location = /finance-radar-api" in source
        assert "location ^~ /finance-radar-api/" in source
        assert "location = /radar-admin" in source
        assert "location ^~ /radar-admin/" in source
        assert "location = /radar-review" in source
        assert "location ^~ /radar-review/" in source
        assert "location = /radar-ops" in source
        assert "location ^~ /radar-ops/" in source
        assert "proxy_pass http://127.0.0.1:18000" not in source


def test_offhost_status_is_denied_at_the_public_edge() -> None:
    for relative in (
        "deployment/systemd/nginx-radar-direct.conf",
        "deployment/systemd/nginx-radar-locations.conf",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "location = /radar/offhost-status.json" in source
        block = source.split("location = /radar/offhost-status.json", 1)[1].split("}", 1)[0]
        assert "return 404;" in block
        assert "alias " not in block
