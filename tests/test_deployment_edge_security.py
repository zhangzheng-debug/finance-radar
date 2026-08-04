from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_compose_public_web_does_not_load_dotenv_or_admin_token() -> None:
    source = (ROOT / "deployment/compose.yml").read_text(encoding="utf-8")
    web = source.split("\n  web:\n", 1)[1].split("\n  admin:\n", 1)[0]
    assert "env_file: []" in web
    assert "environment: *radar-public-web-environment" in web
    assert "volumes: []" in web
    assert "FINANCE_RADAR_ADMIN_TOKEN" not in web
    public_environment = source.split(
        "x-radar-public-web-environment: &radar-public-web-environment", 1
    )[1].split("\nx-radar-service:", 1)[0]
    assert "FINANCE_RADAR_UI_ROLE: public" in public_environment
    assert "FINANCE_RADAR_SHOW_DEBUG" in public_environment
    assert "FINANCE_RADAR_ADMIN_TOKEN" not in public_environment


def test_compose_admin_is_opt_in_and_bound_to_host_loopback() -> None:
    source = (ROOT / "deployment/compose.yml").read_text(encoding="utf-8")
    admin = source.split("\n  admin:\n", 1)[1].split("\n  worker:\n", 1)[0]
    assert 'profiles: ["admin"]' in admin
    assert "FINANCE_RADAR_UI_ROLE: admin" in admin
    assert 'ports: ["127.0.0.1:18502:8502"]' in admin
    assert '"app/web/Admin.py"' in admin


def test_compose_worker_and_backup_keep_the_production_safety_defaults() -> None:
    source = (ROOT / "deployment/compose.yml").read_text(encoding="utf-8")
    worker = source.split("\n  worker:\n", 1)[1].split("\n  backup:\n", 1)[0]
    backup = source.split("\n  backup:\n", 1)[1].split("\n  notifier:\n", 1)[0]

    assert '"--no-light-verify"' in worker
    assert '"--retention", "1", "--weekly-retention", "0"' in backup


def test_portable_caddy_edge_returns_not_found_for_backend_routes() -> None:
    source = (ROOT / "deployment/Caddyfile").read_text(encoding="utf-8")
    assert "@private_backend path /api/* /docs* /openapi.json /finance-radar-api*" in source
    for internal_path in (
        "/radar-admin*",
        "/Event_Intelligence*",
        "/Operations_and_Model*",
        "/Adjudication_Studio*",
    ):
        assert internal_path in source
    private_handle = source.split("handle @private_backend", 1)[1].split("}", 1)[0]
    assert "respond 404" in private_handle
    assert "reverse_proxy api:8000" not in source


def test_admin_entry_refuses_public_role_before_rendering_links() -> None:
    source = (ROOT / "app/web/Admin.py").read_text(encoding="utf-8")
    assert "require_admin_ui()" in source
    assert source.index("require_admin_ui()") < source.index("st.button")
    assert 'render_primary_navigation("admin_home")' in source
    assert 'requested_page = st.query_params.get("_page")' in source
    assert "st.switch_page(page_targets[requested_page])" in source
