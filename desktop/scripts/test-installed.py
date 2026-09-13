#!/usr/bin/env python3
"""Run installed desktop checks without new external connections.

macOS uses a temporary PF anchor. Windows uses an ephemeral outbound Firewall
rule on its disposable runner, which also covers the private Python executable
and descendants whose paths are not known before first launch. Both retain
loopback for Electron's authenticated local server.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[2]
PROBE = ("1.1.1.1", 443)


def interrupted(_signum, _frame):
    raise KeyboardInterrupt


def run_installed_checks() -> None:
    env = {**os.environ, "UV_OFFLINE": "1"}
    ui = subprocess.run(
        command_argv("npm", "--prefix", "desktop", "run", "test:installed"),
        cwd=ROOT,
        env=env,
        check=False,
    )
    native = subprocess.run(
        command_argv("node", "desktop/scripts/check-native.mjs"),
        cwd=ROOT,
        env=env,
        check=False,
    )
    ui.check_returncode()
    native.check_returncode()


def command_argv(command: str, *args: str) -> list[str]:
    return [str(required_executable(command)), *args]


def required_executable(command: str) -> Path:
    resolved = shutil.which(command)
    if not resolved:
        raise RuntimeError(f"Required test command is unavailable: {command}")
    return Path(resolved).resolve()


def required_environment_executable(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required Windows executable variable is unavailable: {name}"
        )
    return Path(value).resolve()


def pf(*args: str) -> str:
    result = subprocess.run(
        ["sudo", "-n", "/sbin/pfctl", *args], capture_output=True, text=True, check=True
    )
    return result.stdout + result.stderr


def macos() -> None:
    anchor = f"com.apple/frisket-desktop-{os.getpid()}"
    rules = "pass out quick on ! lo0 proto tcp flags A/A no state\nblock drop out quick on ! lo0 all\n"
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
        with tempfile.NamedTemporaryFile(mode="w", suffix=".pf") as rules_file:
            rules_file.write(rules)
            rules_file.flush()
            pf("-a", anchor, "-f", rules_file.name)
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
        counters = pf("-a", anchor, "-vvs", "rules")
        block = counters.split("block drop out", 1)[-1]
        packets = re.search(r"Packets:\s*(\d+)", block)
        if not packets or int(packets[1]) == 0:
            raise RuntimeError("PF did not record blocking the external connection")
        print(
            "PF verified: new external connections blocked; Chromium sandbox remains enabled",
            flush=True,
        )
        run_installed_checks()
    finally:
        try:
            pf("-a", anchor, "-F", "rules")
        finally:
            if token:
                pf("-X", token)


def powershell(script: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        cwd=ROOT,
        env=env,
        check=True,
    )


def socket_reaches(host: str, port: int, timeout: float = 3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def windows_firewall_programs() -> list[Path]:
    profile = Path(os.environ["FRISKET_DESKTOP_PROFILE"]).resolve()
    app = Path(os.environ["FRISKET_DESKTOP_APP"]).resolve()
    if not app.is_file() or app.suffix.lower() != ".exe":
        raise RuntimeError(
            "FRISKET_DESKTOP_APP must name the installed Windows executable"
        )
    if not profile.is_dir():
        raise RuntimeError(
            "FRISKET_DESKTOP_PROFILE must name the prepared test profile"
        )
    saved_runtime = profile / ".frisket-prewarm-runtime-python"
    try:
        runtime_python = Path(
            saved_runtime.read_text(encoding="utf-8").strip()
        ).resolve()
    except OSError as error:
        raise RuntimeError(
            "Network prewarm did not record the private runtime path"
        ) from error
    if runtime_python.suffix.lower() != ".exe" or not runtime_python.is_relative_to(
        profile
    ):
        raise RuntimeError("Recorded private runtime path escapes the test profile")
    resources = app.parent / "resources"
    required = [
        app,
        Path(sys.executable).resolve(),
        required_executable("node"),
        required_executable("powershell.exe"),
        required_environment_executable("COMSPEC"),
        runtime_python,
        *(
            resources / "bin" / f"{name}.exe"
            for name in ("uv", "ffmpeg", "ffprobe", "deno")
        ),
    ]
    base_pythons = list((profile / "python").rglob("python.exe"))
    if not base_pythons:
        raise RuntimeError(
            "Network prewarm did not retain a private Python base interpreter"
        )
    browser_executables = list((profile / "cache" / "playwright").rglob("*.exe"))
    if not browser_executables:
        raise RuntimeError("Network prewarm did not retain a Chromium executable")
    programs = [*required, *base_pythons, *browser_executables]
    for program in programs:
        if program == runtime_python:
            continue
        if not program.is_file() or program.suffix.lower() != ".exe":
            raise RuntimeError(
                f"Expected Windows firewall program is unavailable: {program}"
            )
    return list(dict.fromkeys(programs))


def assert_loopback() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            pass


def windows() -> None:
    name = f"Frisket desktop offline proof {os.getpid()}"
    programs = windows_firewall_programs()
    if not socket_reaches(*PROBE, timeout=5):
        raise RuntimeError(
            "The pre-rule external TCP probe failed; refusing an inconclusive egress test"
        )
    # Keep the runner's GitHub control channel intact. The listed programs cover
    # Electron, Chromium, native tools, the command wrappers, current harness
    # Python, and both private Python paths used before and after first launch.
    create = """
      $ErrorActionPreference = 'Stop'
      $programs = $env:FRISKET_DESKTOP_FIREWALL_PROGRAMS | ConvertFrom-Json
      foreach ($program in $programs) {
        New-NetFirewallRule -DisplayName $env:FRISKET_DESKTOP_FIREWALL_NAME -Direction Outbound -Action Block -Program $program -RemoteAddress Any -Profile Any | Out-Null
      }
      if (@(Get-NetFirewallRule -DisplayName $env:FRISKET_DESKTOP_FIREWALL_NAME).Count -ne $programs.Count) {
        throw 'Windows Firewall did not create every bounded offline rule.'
      }
    """
    firewall_env = {
        **os.environ,
        "FRISKET_DESKTOP_FIREWALL_NAME": name,
        "FRISKET_DESKTOP_FIREWALL_PROGRAMS": json.dumps(
            [str(program) for program in programs]
        ),
    }
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, interrupted)
    try:
        powershell(create, firewall_env)
        if socket_reaches(*PROBE):
            raise RuntimeError(
                "The temporary Windows Firewall rules allowed an external TCP connection"
            )
        assert_loopback()
        print(
            "Windows Firewall verified: external TCP blocked and loopback retained; Chromium sandbox remains enabled."
        )
        run_installed_checks()
    finally:
        powershell(
            "$ErrorActionPreference = 'Stop'; "
            "Get-NetFirewallRule | "
            f"Where-Object {{ $_.DisplayName -eq '{name}' }} | "
            "Remove-NetFirewallRule"
        )


def main() -> None:
    if platform.system() == "Darwin":
        macos()
    elif platform.system() == "Windows":
        windows()
    else:
        raise RuntimeError(
            "Installed desktop proof supports macOS Apple Silicon and Windows x64 only."
        )


if __name__ == "__main__":
    main()
