from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


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


def test_internal_pages_are_denied_and_public_api_is_get_only_allowlist() -> None:
    for relative in (
        "deployment/systemd/nginx-radar-direct.conf",
        "deployment/systemd/nginx-radar-locations.conf",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        internal_group = "Event_Intelligence|Operations_and_Model|Adjudication_Studio"
        assert f"^/radar/({internal_group})(?:/|$)" in source
        assert f"$arg__page ~ ^({internal_group})$" in source
        assert "location = /finance-radar-api" in source
        assert "if ($request_method !~ ^(GET|HEAD)$) { return 403; }" in source
        assert "location = /finance-radar-api/" in source
        assert "root /var/www" in source
        assert "try_files /finance-radar-public-api/index.html =404" in source
        assert "location ~ ^/finance-radar-api/api/v1/" in source
        for route in (
            "live",
            "overview",
            "events",
            "events/facets",
            "dossier",
            "knowledge",
            "evidence",
            "sources",
            "source-interpretations",
            "capture-explanation",
        ):
            assert route in source
        assert "limit_except GET { deny all; }" in source
        assert "rewrite ^/finance-radar-api(/.*)$ $1 break" in source
        assert "proxy_pass http://127.0.0.1:18000" in source
        assert "proxy_pass_request_headers off" in source
        for header in (
            "Authorization",
            "Proxy-Authorization",
            "Cookie",
            "X-Admin-Token",
            "X-Reviewer-Token",
            "X-Operator-Token",
            "X-API-Key",
        ):
            assert f'proxy_set_header {header} ""' in source
        assert "proxy_hide_header Set-Cookie" in source
        assert 'Content-Security-Policy "default-src \'none\'; frame-ancestors \'none\'"' in source
        assert "location /finance-radar-api/" in source
        assert "location ^~ /finance-radar-api/" not in source
        assert "location = /radar-admin" in source
        assert "location ^~ /radar-admin/" in source
        assert "location = /radar-review" in source
        assert "location ^~ /radar-review/" in source
        assert "location = /radar-ops" in source
        assert "location ^~ /radar-ops/" in source


def test_public_api_entry_is_static_and_never_transmits_or_persists_the_key() -> None:
    source = (ROOT / "deployment/public-api/index.html").read_text(encoding="utf-8")

    assert 'type="password"' in source
    assert 'id="model"' in source
    for model in (
        "deepseek-chat",
        "deepseek-reasoner",
        "qwen-plus",
        "qwen-turbo",
        "qwen-max",
        "custom",
    ):
        assert f'value="{model}"' in source
    assert "event.preventDefault()" in source
    assert 'keyInput.value = ""' in source
    assert "仅展示" not in source
    for primitive in (
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "sendBeacon",
        "localStorage",
        "sessionStorage",
        "document.cookie",
    ):
        assert primitive not in source


def test_installer_versions_rolls_back_and_probes_the_public_read_api() -> None:
    source = (ROOT / "deployment/systemd/install_remote.sh").read_text(encoding="utf-8")

    assert "PUBLIC_API_DIR=/var/www/finance-radar-public-api" in source
    assert 'PUBLIC_API_INDEX="$PUBLIC_API_DIR/index.html"' in source
    assert '"$PUBLIC_API_INDEX"' in source.split("ROLLBACK_PATHS=(", 1)[1].split(")", 1)[0]
    assert "install_public_api_entry()" in source
    assert 'PUBLIC_API_ASSET="$RELEASE/deployment/public-api/index.html"' in source
    assert 'install -m 0644 -o root -g root "$source" "$temporary"' in source
    assert 'mv -f -- "$temporary" "$PUBLIC_API_INDEX"' in source
    assert "public API entry asset contains a network or persistence primitive" in source
    assert "public read API check failed" in source
    assert "assert_edge_status /finance-radar-api/api/v1/events 403 POST" in source


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
