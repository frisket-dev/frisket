"""Bounded, secret-free diagnostics for the native Windows service guardian."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from frisket.runtime.model_install import model_child_environment
from frisket.runtime.supervisor import spawn_service, stop_service


def _stage(name: str) -> None:
    print(f"windows-service-diagnostic: {name}", flush=True)


def main() -> int:
    if os.name != "nt":
        _stage("skipped-non-windows")
        return 0
    environment = model_child_environment(os.environ)
    with tempfile.TemporaryDirectory(prefix="frisket-guard-diagnostic-") as raw:
        root = Path(raw)
        environment["HF_HOME"] = str(root / "cache")
        environment["PYTHONNOUSERSITE"] = "1"
        system_root = environment.get("SYSTEMROOT") or environment.get("SystemRoot")
        if not system_root:
            raise RuntimeError("stage-env: SYSTEMROOT missing")
        powershell = (
            Path(system_root) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )

        _stage("raw-powershell-start")
        raw_result = subprocess.run(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "exit 0",
            ],
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            stderr=sys.stderr,
            env=environment,
            timeout=15,
            check=False,
        )
        _stage(f"raw-powershell-exit-{raw_result.returncode}")
        if raw_result.returncode:
            return 1

        failures = 0

        def run_trivial(name: str, stdout, stderr) -> None:
            nonlocal failures
            _stage(f"guarded-trivial-{name}-start")
            process = spawn_service(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import sys; print('child-ok',file=sys.stderr)",
                ],
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                env=environment,
            )
            control = getattr(process, "_frisket_guard_control", None)
            try:
                code = process.wait(timeout=20)
                proof = bool(control and control[2].is_file())
                _stage(f"guarded-trivial-{name}-exit-{code}-proof-{int(proof)}")
                stop_service(process)
                failures += int(bool(code) or not proof)
            except subprocess.TimeoutExpired:
                failures += 1
                _stage(f"guarded-trivial-{name}-timeout")
                process.kill()
                process.wait(timeout=5)
                if control:
                    shutil.rmtree(control[0], ignore_errors=True)

        run_trivial("devnull", subprocess.DEVNULL, subprocess.DEVNULL)
        with (
            (root / "stdout.log").open("wb") as stdout_file,
            (root / "stderr.log").open("wb") as stderr_file,
        ):
            run_trivial("separate-files", stdout_file, stderr_file)
        _stage(
            "guarded-trivial-separate-files-bytes-"
            f"{(root / 'stdout.log').stat().st_size}-"
            f"{(root / 'stderr.log').stat().st_size}"
        )
        run_trivial("same-stderr", sys.stderr, sys.stderr)

        marker = root / "ready"
        _stage("guarded-stop-start")
        service = spawn_service(
            [
                sys.executable,
                "-I",
                "-c",
                "from pathlib import Path; import sys,time; "
                "Path(sys.argv[1]).write_text('ready'); time.sleep(60)",
                str(marker),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
        )
        deadline = time.monotonic() + 15
        while not marker.is_file():
            if service.poll() is not None:
                raise RuntimeError(
                    f"stage-guarded-stop: guardian exited {service.returncode}"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError("stage-guarded-stop: target did not start")
            time.sleep(0.05)
        _stage("guarded-stop-request")
        stop_service(service)
        _stage("guarded-stop-clean")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
