from __future__ import annotations

import argparse
import atexit
import re
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class InternalUiRole:
    key: str
    label: str
    unit: str
    remote_port: int
    base_path: str


ROLE_SPECS: dict[str, InternalUiRole] = {
    "admin": InternalUiRole(
        key="admin",
        label="Admin / 管理总览",
        unit="finance-radar-admin.service",
        remote_port=18502,
        base_path="/radar-admin/",
    ),
    "reviewer": InternalUiRole(
        key="reviewer",
        label="Reviewer / 人工复核",
        unit="finance-radar-reviewer.service",
        remote_port=18503,
        base_path="/radar-review/",
    ),
    "operator": InternalUiRole(
        key="operator",
        label="Operator / 运行维护",
        unit="finance-radar-operator.service",
        remote_port=18504,
        base_path="/radar-ops/",
    ),
}

_SAFE_HOST = re.compile(r"[A-Za-z0-9_.@:\[\]-]+\Z")


def validated_host(value: str) -> str:
    """Accept an explicit OpenSSH destination, never a shell expression."""

    host = value.strip()
    if not host or host.startswith("-") or not _SAFE_HOST.fullmatch(host):
        raise argparse.ArgumentTypeError(
            "--host must be an explicit OpenSSH destination such as ubuntu@server.example"
        )
    return host


def validated_port(value: str | int) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def ssh_base_command(
    *, ssh_command: str, ssh_port: int, identity_file: Path | None
) -> list[str]:
    command = [
        ssh_command,
        "-p",
        str(ssh_port),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=12",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
    ]
    if identity_file is not None:
        command.extend(["-i", str(identity_file)])
    return command


def service_command(
    base: Sequence[str], host: str, action: str, unit: str
) -> list[str]:
    if action not in {"start", "stop", "is-active"}:
        raise ValueError(f"unsupported systemd action: {action}")
    suffix = ["--quiet"] if action == "is-active" else []
    return [
        *base,
        host,
        "sudo",
        "-n",
        "systemctl",
        action,
        *suffix,
        unit,
    ]


def tunnel_command(
    base: Sequence[str], host: str, *, local_port: int, remote_port: int
) -> list[str]:
    return [
        *base,
        "-o",
        "ExitOnForwardFailure=yes",
        "-N",
        "-L",
        f"{local_port}:127.0.0.1:{remote_port}",
        host,
    ]


def local_url(role: InternalUiRole, local_port: int) -> str:
    return f"http://127.0.0.1:{local_port}{role.base_path}"


def display_command(command: Sequence[str]) -> str:
    """Render argv for review without ever executing through a shell."""

    return subprocess.list2cmdline(list(command))


def choose_role() -> InternalUiRole:
    choices = (ROLE_SPECS["admin"], ROLE_SPECS["reviewer"], ROLE_SPECS["operator"])
    print("选择内部工作面：")
    for index, role in enumerate(choices, start=1):
        print(f"  {index}. {role.label}")
    while True:
        selected = input("请输入 1、2 或 3：").strip()
        if selected in {"1", "2", "3"}:
            return choices[int(selected) - 1]
        print("无效选择，请重试。")


def _ensure_identity(identity_file: Path | None) -> Path | None:
    if identity_file is None:
        return None
    resolved = identity_file.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"SSH identity file does not exist: {resolved}")
    return resolved


def _ensure_local_port_available(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(
                f"local port {port} is already in use; pass --local-port with a free port"
            ) from exc


def _wait_for_tunnel(port: int, process: subprocess.Popen[bytes], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"SSH tunnel exited before it became ready (code {exit_code})")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"SSH tunnel did not open local port {port} within {timeout:.0f}s")


class SessionCleanup:
    def __init__(self, stop_command: Sequence[str]) -> None:
        self.stop_command = list(stop_command)
        self.tunnel: subprocess.Popen[bytes] | None = None
        self.service_started = False
        self.completed = False

    def run(self) -> None:
        if self.completed:
            return
        self.completed = True
        try:
            if self.tunnel is not None and self.tunnel.poll() is None:
                self.tunnel.terminate()
                try:
                    self.tunnel.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.tunnel.kill()
                    self.tunnel.wait(timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"警告：本机 SSH 隧道清理失败：{type(exc).__name__}", file=sys.stderr)
        if self.service_started:
            print("正在停止本次启动的内部 UI 服务……")
            try:
                result = subprocess.run(
                    self.stop_command,
                    check=False,
                    timeout=20,
                    stdin=subprocess.DEVNULL,
                )
                stopped = result.returncode == 0
            except (OSError, subprocess.SubprocessError):
                stopped = False
            if not stopped:
                print(
                    "警告：远端服务未能自动停止，请人工执行上方 dry-run 中的 stop 命令。",
                    file=sys.stderr,
                )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "按需启动 Finance Radar 的回环内部 UI，建立 SSH 隧道并打开本机浏览器。"
            "退出时只停止由本次会话启动的内部 UI；不会启动或修改公网服务。"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--host",
        required=True,
        type=validated_host,
        help="显式 SSH 目标，例如 ubuntu@server.example；仓库不保存默认主机",
    )
    parser.add_argument(
        "--role",
        choices=tuple(ROLE_SPECS),
        help="内部角色；省略时显示 Admin/Reviewer/Operator 选择菜单",
    )
    parser.add_argument(
        "--identity-file",
        type=Path,
        help="可选 SSH 私钥路径，可直接位于 D:；不会复制到临时目录",
    )
    parser.add_argument("--ssh-port", type=int, default=22, help="SSH 端口")
    parser.add_argument(
        "--local-port",
        type=validated_port,
        help="本机回环端口；默认使用所选角色的服务端端口",
    )
    parser.add_argument("--ssh-command", default="ssh", help="OpenSSH 客户端命令")
    parser.add_argument(
        "--no-browser", action="store_true", help="隧道就绪后不自动打开浏览器"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印检查、启动、隧道和清理命令，不连接服务器",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.ssh_port <= 65535:
        raise SystemExit("--ssh-port must be between 1 and 65535")
    role = ROLE_SPECS[args.role] if args.role else choose_role()
    local_port = args.local_port or role.remote_port
    identity = args.identity_file if args.dry_run else _ensure_identity(args.identity_file)
    base = ssh_base_command(
        ssh_command=args.ssh_command,
        ssh_port=args.ssh_port,
        identity_file=identity,
    )
    active_checks = [
        service_command(base, args.host, "is-active", spec.unit)
        for spec in ROLE_SPECS.values()
    ]
    start = service_command(base, args.host, "start", role.unit)
    stop = service_command(base, args.host, "stop", role.unit)
    tunnel = tunnel_command(
        base,
        args.host,
        local_port=local_port,
        remote_port=role.remote_port,
    )
    url = local_url(role, local_port)

    if args.dry_run:
        print(f"角色: {role.label}")
        print("活动会话检查（任一返回 active 都会拒绝接管）：")
        for command in active_checks:
            print(f"  {display_command(command)}")
        print(f"启动: {display_command(start)}")
        print(f"隧道: {display_command(tunnel)}")
        print(f"浏览器: {url}")
        print(f"退出清理: {display_command(stop)}")
        return 0

    _ensure_local_port_available(local_port)
    for spec, command in zip(ROLE_SPECS.values(), active_checks, strict=True):
        result = subprocess.run(
            command,
            check=False,
            timeout=20,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            raise RuntimeError(
                f"{spec.label} is already active; refusing to take ownership of another session"
            )
        if result.returncode not in {3}:
            raise RuntimeError(
                f"could not prove {spec.unit} inactive (SSH/systemctl code {result.returncode})"
            )

    print(f"正在启动 {role.label}（仅服务器 127.0.0.1）……")
    subprocess.run(start, check=True, timeout=30, stdin=subprocess.DEVNULL)
    cleanup = SessionCleanup(stop)
    cleanup.service_started = True
    atexit.register(cleanup.run)

    def stop_on_signal(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, stop_on_signal)

    try:
        cleanup.tunnel = subprocess.Popen(
            tunnel,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )
        _wait_for_tunnel(local_port, cleanup.tunnel)
        print(f"隧道已就绪：{url}")
        print("保持本窗口开启；完成后按 Ctrl+C，启动器会关闭隧道并停止内部 UI。")
        if not args.no_browser:
            webbrowser.open(url, new=2)
        while cleanup.tunnel.poll() is None:
            time.sleep(0.5)
        if cleanup.tunnel.returncode:
            raise RuntimeError(f"SSH tunnel closed unexpectedly (code {cleanup.tunnel.returncode})")
        return 0
    except KeyboardInterrupt:
        print("\n正在结束内部会话……")
        return 0
    finally:
        cleanup.run()


def main() -> None:
    try:
        raise SystemExit(run())
    except (FileNotFoundError, RuntimeError, subprocess.SubprocessError, TimeoutError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
