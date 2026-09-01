from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]
ARCHIVED_README = ROOT / "claudeUI" / "deployment" / "README.md"
ARCHIVED_NGINX = ROOT / "claudeUI" / "deployment" / "nginx-finance-radar-static.conf"
ARCHIVED_PROTOTYPE = ROOT / "claudeUI" / "prototype" / "index.html"
ARCHIVED_DEV_SERVER = ROOT / "claudeUI" / "prototype" / "dev_server.py"
PRODUCTION_NGINX = ROOT / "deployment" / "systemd" / "nginx-radar-direct.conf"


def test_archived_static_terminal_cannot_be_mistaken_for_a_second_production_path() -> None:
    readme = ARCHIVED_README.read_text(encoding="utf-8")
    nginx = ARCHIVED_NGINX.read_text(encoding="utf-8")

    assert "not a deployment path" in readme
    assert "one authoritative production UI" in readme
    assert "`server` block" in readme
    assert "server {" not in nginx
    assert "proxy_pass" not in nginx


def test_archived_prototype_is_frozen_and_cannot_transition_to_production_data() -> None:
    prototype = ARCHIVED_PROTOTYPE.read_text(encoding="utf-8")
    dev_server = ARCHIVED_DEV_SERVER.read_text(encoding="utf-8")

    assert "ARCHIVED · FROZEN SNAPSHOT" in prototype
    assert "fetch(" not in prototype
    assert "tryLive" not in prototype
    assert "apiBase:" not in prototype
    assert "/finance-radar-api" not in prototype
    assert "S.live = true" not in prototype
    assert "requests" not in dev_server
    assert "--upstream" not in dev_server
    assert "_proxy_read_only" not in dev_server
    assert "ArchivedPrototypeHandler" in dev_server


def test_production_edge_adds_only_a_bounded_read_api_to_streamlit() -> None:
    nginx = PRODUCTION_NGINX.read_text(encoding="utf-8")

    assert "proxy_pass http://127.0.0.1:18501" in nginx
    assert "location = /finance-radar-api/" in nginx
    assert "location ~ ^/finance-radar-api/api/v1/" in nginx
    assert "proxy_pass_request_headers off" in nginx
    assert "limit_except GET { deny all; }" in nginx
    assert "location /finance-radar-api/" in nginx
    assert "location ^~ /radar-admin/" in nginx
    assert nginx.count("return 404;") >= 4
