"""Optional process-wide network guard for the Finance Radar offline demo.

Python imports ``sitecustomize`` automatically when this file is on
``PYTHONPATH``.  The launcher enables the guard explicitly; normal development
and production processes are unchanged.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any


class OfflineNetworkBlocked(OSError):
    """Raised when an offline-demo process attempts an external connection."""


_INSTALLED = False
_ORIGINAL_GETADDRINFO = socket.getaddrinfo
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_SENDTO = socket.socket.sendto


def _loopback_host(host: Any) -> bool:
    if host in (None, "", "localhost", "localhost.localdomain"):
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="ignore")
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _address_host(address: Any) -> Any:
    if isinstance(address, tuple) and address:
        return address[0]
    return None


def _blocked(host: Any) -> OfflineNetworkBlocked:
    return OfflineNetworkBlocked(
        f"Finance Radar offline guard blocked external network destination: {host!r}"
    )


def install_guard() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any):
        if not _loopback_host(host):
            raise _blocked(host)
        return _ORIGINAL_GETADDRINFO(host, *args, **kwargs)

    def guarded_connect(sock: socket.socket, address: Any):
        host = _address_host(address)
        if host is not None and not _loopback_host(host):
            raise _blocked(host)
        return _ORIGINAL_CONNECT(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: Any):
        host = _address_host(address)
        if host is not None and not _loopback_host(host):
            raise _blocked(host)
        return _ORIGINAL_CONNECT_EX(sock, address)

    def guarded_sendto(sock: socket.socket, data: bytes, *args: Any):
        address = args[-1] if args else None
        host = _address_host(address)
        if host is not None and not _loopback_host(host):
            raise _blocked(host)
        return _ORIGINAL_SENDTO(sock, data, *args)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.socket.sendto = guarded_sendto
    _INSTALLED = True


if os.getenv("FINANCE_RADAR_OFFLINE_NETWORK_GUARD") == "1":
    install_guard()
