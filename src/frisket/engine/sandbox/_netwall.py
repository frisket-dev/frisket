"""Install Frisket's CPython audit netwall in a managed worker.

This module is loaded by the fixed runtime bootstrap before it imports any
domain worker.  The broker endpoint remains in the scrubbed child environment:
it can contain an authentication token and must never travel in argv.
"""

from __future__ import annotations

import os
import sys
from urllib.parse import unquote, urlsplit


_SOCKET_EVENTS = {
    "socket.bind",
    "socket.connect",
    "socket.getaddrinfo",
    "socket.gethostbyaddr",
    "socket.gethostbyname",
    "socket.getnameinfo",
    "socket.sendmsg",
    "socket.sendto",
}


def install() -> None:
    """Deny Python socket/process APIs except the exact configured broker."""

    allowed_unix: str | None = None
    allowed_tcp: tuple[str, int] | None = None
    endpoint = os.environ.get("FRISKET_BROKER_ENDPOINT")
    if endpoint:
        parts = urlsplit(endpoint)
        if parts.scheme == "unix":
            allowed_unix = unquote(parts.path)
        elif (
            parts.scheme == "tcp"
            and parts.hostname == "127.0.0.1"
            and parts.port is not None
        ):
            allowed_tcp = ("127.0.0.1", parts.port)

    def audit(event: str, args: tuple[object, ...]) -> None:
        if event == "socket.connect":
            address = args[1] if len(args) > 1 else None
            if (
                allowed_unix is not None
                and isinstance(address, str)
                and address == allowed_unix
            ):
                return
            if (
                allowed_tcp is not None
                and isinstance(address, tuple)
                and address == allowed_tcp
            ):
                return
        if event in _SOCKET_EVENTS:
            raise PermissionError("network access is disabled by the Frisket sandbox")
        if event in {
            "subprocess.Popen",
            "os.system",
            "os.posix_spawn",
            "os.exec",
            "os.fork",
        }:
            raise PermissionError(
                "subprocesses are disabled by the Frisket sandbox netwall"
            )

    sys.addaudithook(audit)
