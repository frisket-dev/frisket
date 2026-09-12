#!/usr/bin/env python3
"""Run the installed Mac tests without new external connections.

PF avoids nesting another Seatbelt profile around Chromium's own sandbox.
Only an ephemeral child anchor and our enable reference are owned here.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ANCHOR = f"com.apple/frisket-desktop-{os.getpid()}"
RULES = "pass out quick on ! lo0 proto tcp flags A/A no state\nblock drop out quick on ! lo0 all\n"
PROBE = ("1.1.1.1", 443)


def pf(*args: str) -> str:
    result = subprocess.run(
        ["sudo", "-n", "/sbin/pfctl", *args], capture_output=True, text=True, check=True
    )
    return result.stdout + result.stderr


def interrupted(_signum, _frame):
    raise KeyboardInterrupt


def main() -> None:
    # Setup probe: no HTTP request, credentials, or application data is sent.
    with socket.create_connection(PROBE, timeout=5):
        pass
    if 'anchor "com.apple/*"' not in pf("-sr"):
        raise RuntimeError(
            "Expected macOS PF anchor is absent; refusing to replace system rules"
        )
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupted)
    token = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pf") as rules:
            rules.write(RULES)
            rules.flush()
            pf("-a", ANCHOR, "-f", rules.name)
        enabled = pf("-E")
        match = re.search(r"Token\s*:\s*(\d+)", enabled)
        if not match:
            raise RuntimeError("PF did not return an enable token")
        token = match[1]
        try:
            with socket.create_connection(PROBE, timeout=2):
                raise RuntimeError("PF allowed a fresh external connection")
        except (TimeoutError, ConnectionError, OSError):
            pass
        counters = pf("-a", ANCHOR, "-vvs", "rules")
        block = counters.split("block drop out", 1)[-1]
        packets = re.search(r"Packets:\s*(\d+)", block)
        if not packets or int(packets[1]) == 0:
            raise RuntimeError("PF did not record blocking the external connection")
        print(
            "PF verified: new external connections blocked; Chromium sandbox remains enabled",
            flush=True,
        )
        env = {**os.environ, "UV_OFFLINE": "1"}
        subprocess.run(
            ["npm", "--prefix", "desktop", "run", "test:installed"],
            cwd=ROOT,
            env=env,
            check=True,
        )
        subprocess.run(
            ["node", "desktop/scripts/check-native.mjs"], cwd=ROOT, env=env, check=True
        )
    finally:
        try:
            pf("-a", ANCHOR, "-F", "rules")
        finally:
            if token:
                pf("-X", token)


if __name__ == "__main__":
    main()
