from __future__ import annotations

import os
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
WRAPPER = ROOT / "deployment" / "systemd" / "run_backup_quiesced.sh"


def _bash() -> str:
    # Windows exposes a Microsoft Store launcher at WindowsApps/bash.exe even
    # when WSL is not installed.  It is a real file but not a usable Bash
    # runtime, so prefer the conventional Git for Windows executable first.
    for candidate in ("C:/Program Files/Git/bin/bash.exe", shutil.which("bash")):
        if candidate and Path(candidate).is_file():
            return candidate
    pytest.skip("bash is required to exercise the backup quiesce wrapper")


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _path_for_bash(path: PurePath) -> str:
    """Return a PATH-safe spelling for native Bash and Git Bash.

    A Windows drive prefix contains ``:`` and would be split as a PATH list by
    Git Bash, so convert ``C:/...`` to ``/c/...``.  Native POSIX paths have no
    drive and must pass through unchanged.
    """

    value = path.as_posix()
    drive = path.drive
    if drive:
        return f"/{drive[0].lower()}{value[len(drive):]}"
    return value


def _run_wrapper(
    tmp_path: Path,
    *,
    worker_state: str,
    backup_rc: int = 0,
    start_rc: int = 0,
    inhibit: bool = False,
    backup_start_inhibit: bool = False,
    candidate_source: bool = False,
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    state_file = tmp_path / "worker-state.txt"
    log_file = tmp_path / "calls.log"
    state_file.write_text(worker_state, encoding="utf-8")
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
set -euo pipefail
state="$(cat "$FAKE_STATE")"
printf 'systemctl %s\\n' "$*" >> "$FAKE_LOG"
case "$1" in
  show)
    [ "${2:-}" = "finance-radar-worker.service" ] || exit 91
    [ "${3:-}" = "--property=ActiveState" ] || exit 92
    [ "${4:-}" = "--value" ] || exit 93
    printf '%s\\n' "$state"
    ;;
  is-active)
    [ "${2:-}" = "--quiet" ] || exit 94
    [ "${3:-}" = "finance-radar-worker.service" ] || exit 95
    [ "$state" = active ]
    ;;
  stop)
    [ "${2:-}" = "finance-radar-worker.service" ] || exit 96
    printf 'inactive' > "$FAKE_STATE"
    ;;
  start)
    [ "${2:-}" = "finance-radar-worker.service" ] || exit 97
    [ "${FAKE_START_RC:-0}" = 0 ] || exit "$FAKE_START_RC"
    printf 'active' > "$FAKE_STATE"
    ;;
  *)
    exit 98
    ;;
esac
""",
    )
    _write_executable(
        fake_bin / "runuser",
        """#!/usr/bin/env bash
set -euo pipefail
printf 'runuser %s PWD=%s PYTHONPATH=%s\\n' "$*" "$PWD" "${PYTHONPATH:-}" >> "$FAKE_LOG"
if [ "${4:-}" = test ]; then
    exit 0
fi
if [ "${4:-}" = env ]; then
    exit "${FAKE_BACKUP_RC:-0}"
fi
exit 98
        """,
    )
    _write_executable(
        fake_bin / "readlink",
        """#!/usr/bin/env bash
set -euo pipefail
if [ "${1:-}" = -f ]; then
    shift
fi
if [ "${1:-}" = -- ]; then
    shift
fi
target="${1:?readlink target required}"
if [ "$target" = "$FINANCE_RADAR_BASE/current" ]; then
    printf '%s\\n' "$FINANCE_RADAR_CURRENT_RELEASE_ROOT"
    exit 0
fi
if [ "$target" = "$FINANCE_RADAR_BASE/releases" ]; then
    printf '%s\\n' "$FINANCE_RADAR_RELEASES_ROOT"
    exit 0
fi
case "$target" in
    "$FINANCE_RADAR_BASE/releases/"*)
        printf '%s\\n' "$target"
        exit 0
        ;;
esac
exec /usr/bin/readlink -f "$target"
""",
    )
    base = tmp_path / "finance-radar"
    python_bin = base / "venv" / "bin" / "python"
    python_bin.parent.mkdir(parents=True)
    _write_executable(python_bin, "#!/usr/bin/env bash\nexit 0\n")
    releases = base / "releases"
    current_release = releases / "20260805T000000Z-current"
    candidate_release = releases / "20260805T000001Z-candidate"
    for release in (current_release, candidate_release):
        (release / "app" / "ops").mkdir(parents=True)
        (release / "app" / "ops" / "backup.py").write_text(
            "# synthetic backup source\n", encoding="utf-8"
        )
    inhibit_path = tmp_path / "worker-resume.inhibit"
    if inhibit:
        inhibit_path.write_text("protected cutover", encoding="utf-8")
    backup_start_inhibit_path = tmp_path / "backup-start.inhibit"
    if backup_start_inhibit:
        backup_start_inhibit_path.write_text("deployment stabilization", encoding="utf-8")
    driver = tmp_path / "run-wrapper.sh"
    driver.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'PATH="$FAKE_BIN:$PATH"\n'
        "id() {\n"
        '  if [ "${1:-}" = "-u" ]; then printf \'0\\n\'; return 0; fi\n'
        '  command id "$@"\n'
        "}\n"
        'source "$WRAPPER_PATH"\n',
        encoding="utf-8",
        newline="\n",
    )
    driver.chmod(0o755)
    return subprocess.run(
        [_bash(), driver.as_posix()],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        env={
            **os.environ,
            # PATH itself is parsed as POSIX by Git Bash, so a drive-letter
            # path would split at ``C:``.  Use its /c/... spelling here.
            "FAKE_BIN": _path_for_bash(fake_bin),
            "WRAPPER_PATH": WRAPPER.as_posix(),
            "FAKE_STATE": str(state_file),
            "FAKE_LOG": str(log_file),
            "FAKE_BACKUP_RC": str(backup_rc),
            "FAKE_START_RC": str(start_rc),
            "FINANCE_RADAR_BASE": base.as_posix(),
            "FINANCE_RADAR_CURRENT_RELEASE_ROOT": current_release.as_posix(),
            "FINANCE_RADAR_RELEASES_ROOT": releases.as_posix(),
            "FINANCE_RADAR_WORKER_RESUME_INHIBIT": str(inhibit_path),
            "FINANCE_RADAR_BACKUP_START_INHIBIT": str(backup_start_inhibit_path),
            "FINANCE_RADAR_OPS_DB": "/tmp/finance-radar-operations-test.sqlite3",
            # These unit tests exercise quiesce/resume on Windows Git Bash,
            # where there is no root account or production backup database.
            # Root-attestation behavior has its own deployment contract tests.
            "FINANCE_RADAR_SKIP_ROOT_BACKUP_ATTESTATION": "1",
            **(
                {
                    "FINANCE_RADAR_BACKUP_SOURCE_ROOT": candidate_release.as_posix(),
                    "FINANCE_RADAR_PREDEPLOY_BRIDGE": "1",
                }
                if candidate_source
                else {}
            ),
        },
    )


def _calls(tmp_path: Path) -> list[str]:
    return (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()


def test_path_for_bash_handles_windows_and_posix_paths() -> None:
    assert _path_for_bash(PureWindowsPath("C:/Temp/fake-bin")) == "/c/Temp/fake-bin"
    assert _path_for_bash(PurePosixPath("/tmp/fake-bin")) == "/tmp/fake-bin"


def test_wrapper_quiesces_and_restores_an_active_worker(tmp_path: Path) -> None:
    result = _run_wrapper(tmp_path, worker_state="active")

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    stop = "systemctl stop finance-radar-worker.service"
    backup = "runuser -u finance-radar -- env "
    start = "systemctl start finance-radar-worker.service"
    assert stop in calls
    assert start in calls
    assert any(call.startswith(backup) for call in calls)
    assert calls.index(stop) < next(index for index, call in enumerate(calls) if call.startswith(backup)) < calls.index(start)
    assert "backup_worker_quiesce=PASS" in result.stdout
    assert "backup_worker_resume=PASS" in result.stdout


def test_wrapper_never_revives_a_worker_it_did_not_stop(tmp_path: Path) -> None:
    result = _run_wrapper(tmp_path, worker_state="inactive")

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert "systemctl stop finance-radar-worker.service" not in calls
    assert "systemctl start finance-radar-worker.service" not in calls
    assert "backup_worker_quiesce=NOT_OWNED state=inactive" in result.stdout


def test_wrapper_honors_the_protected_cutover_resume_inhibit(tmp_path: Path) -> None:
    result = _run_wrapper(tmp_path, worker_state="active", inhibit=True)

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    assert "systemctl stop finance-radar-worker.service" in calls
    assert "systemctl start finance-radar-worker.service" not in calls
    assert "backup_worker_resume=INHIBITED" in result.stderr


def test_wrapper_refuses_scheduled_start_while_deployment_stabilizes(tmp_path: Path) -> None:
    result = _run_wrapper(
        tmp_path,
        worker_state="inactive",
        backup_start_inhibit=True,
    )

    assert result.returncode == 3
    assert "scheduled backup start is inhibited" in result.stderr


def test_candidate_bridge_may_run_behind_backup_start_inhibit(tmp_path: Path) -> None:
    result = _run_wrapper(
        tmp_path,
        worker_state="inactive",
        backup_start_inhibit=True,
        candidate_source=True,
    )

    assert result.returncode == 0, result.stderr
    assert any("--predeploy-bridge" in call for call in _calls(tmp_path))


def test_wrapper_explicitly_uses_the_candidate_source_for_a_predeploy_bridge(tmp_path: Path) -> None:
    result = _run_wrapper(tmp_path, worker_state="inactive", candidate_source=True)

    assert result.returncode == 0, result.stderr
    calls = _calls(tmp_path)
    backup_call = next(call for call in calls if " -m app.ops.backup " in call)
    assert "--predeploy-bridge" in backup_call
    assert "20260805T000001Z-candidate" in backup_call
    assert "20260805T000001Z-candidate" in backup_call.split(" PWD=", 1)[1]


def test_wrapper_resumes_its_worker_after_a_failed_backup_but_preserves_failure(tmp_path: Path) -> None:
    result = _run_wrapper(tmp_path, worker_state="active", backup_rc=23)

    assert result.returncode == 23
    calls = _calls(tmp_path)
    assert "systemctl start finance-radar-worker.service" in calls
    assert "backup_worker_resume=PASS" in result.stdout


def test_wrapper_fails_closed_for_transitional_worker_state(tmp_path: Path) -> None:
    result = _run_wrapper(tmp_path, worker_state="activating")

    assert result.returncode == 3
    calls = _calls(tmp_path)
    assert not any(call.startswith("runuser -u finance-radar -- env ") for call in calls)
    assert "refusing backup while worker has a transitional/unknown state" in result.stderr
