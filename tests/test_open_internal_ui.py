from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import scripts.open_internal_ui as launcher


def test_role_contract_is_loopback_only_and_mutually_scoped() -> None:
    assert {
        key: (role.unit, role.remote_port, role.base_path)
        for key, role in launcher.ROLE_SPECS.items()
    } == {
        "admin": ("finance-radar-admin.service", 18502, "/radar-admin/"),
        "reviewer": ("finance-radar-reviewer.service", 18503, "/radar-review/"),
        "operator": ("finance-radar-operator.service", 18504, "/radar-ops/"),
    }
    for role in launcher.ROLE_SPECS.values():
        assert launcher.local_url(role, role.remote_port).startswith("http://127.0.0.1:")


def test_command_generation_uses_argv_and_explicit_destination() -> None:
    base = launcher.ssh_base_command(
        ssh_command="ssh",
        ssh_port=2222,
        identity_file=Path(r"D:\keys\finance radar.pem"),
    )
    start = launcher.service_command(
        base,
        "ubuntu@server.example",
        "start",
        launcher.ROLE_SPECS["admin"].unit,
    )
    tunnel = launcher.tunnel_command(
        base,
        "ubuntu@server.example",
        local_port=19502,
        remote_port=18502,
    )

    assert start[-6:] == [
        "ubuntu@server.example",
        "sudo",
        "-n",
        "systemctl",
        "start",
        "finance-radar-admin.service",
    ]
    assert "D:\\keys\\finance radar.pem" in base
    assert "19502:127.0.0.1:18502" in tunnel
    assert "0.0.0.0" not in " ".join(tunnel)
    assert all("token" not in item.lower() for item in start + tunnel)


@pytest.mark.parametrize(
    "value",
    ["", "-oProxyCommand=bad", "ubuntu@host;whoami", "ubuntu@host name", "$(bad)"],
)
def test_host_validation_rejects_option_and_shell_injection(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        launcher.validated_host(value)


def test_dry_run_never_connects_or_opens_browser(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("dry-run executed SSH"),
    )
    monkeypatch.setattr(
        launcher.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("dry-run opened a tunnel"),
    )
    monkeypatch.setattr(
        launcher.webbrowser,
        "open",
        lambda *_args, **_kwargs: pytest.fail("dry-run opened a browser"),
    )

    assert launcher.run(
        [
            "--host",
            "ubuntu@server.example",
            "--role",
            "reviewer",
            "--identity-file",
            r"D:\keys\review.pem",
            "--dry-run",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "finance-radar-reviewer.service" in output
    assert "18503:127.0.0.1:18503" in output
    assert "http://127.0.0.1:18503/radar-review/" in output
    assert "stop" in output


def test_default_entry_is_owner_admin_not_a_role_prompt(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        launcher,
        "choose_role",
        lambda: pytest.fail("default owner entry prompted for a role"),
    )
    assert launcher.run(["--host", "ubuntu@server.example", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert "Admin / 管理总览" in output
    assert "finance-radar-admin.service" in output
    assert "http://127.0.0.1:18502/radar-admin/" in output


def test_source_contains_no_environment_specific_host_or_secret() -> None:
    source = Path(launcher.__file__).read_text(encoding="utf-8")
    assert "18.208.34.152" not in source
    assert "sslip.io" not in source
    assert "FINANCE_RADAR_ADMIN_TOKEN" not in source
    assert "0.0.0.0" not in source


def test_cleanup_is_idempotent_and_attempts_remote_stop(monkeypatch) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    def fake_run(command: list[str], **_kwargs: object) -> Result:
        calls.append(command)
        return Result()

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    cleanup = launcher.SessionCleanup(["ssh", "explicit-host", "stop-unit"])
    cleanup.service_started = True
    cleanup.run()
    cleanup.run()

    assert calls == [["ssh", "explicit-host", "stop-unit"]]
