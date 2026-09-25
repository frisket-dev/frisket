"""Bounded, secret-free diagnostics for the native Windows service guardian."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from frisket.runtime.model_install import model_child_environment


def _stage(name: str) -> None:
    print(f"windows-service-diagnostic: {name}", flush=True)


def _parser_matrix(
    powershell: Path,
    script: Path,
    config: Path,
    base: dict[str, str],
    variants: dict[str, tuple[str, ...]],
    *,
    explicit_utility: bool = False,
) -> dict[str, bool]:
    processes: dict[str, subprocess.Popen] = {}
    for name, additions in variants.items():
        environment = dict(base)
        environment.update(
            {
                variable: os.environ[variable]
                for variable in additions
                if variable in os.environ
            }
        )
        processes[name] = subprocess.Popen(
            [
                str(powershell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(script),
                "-Config",
                str(config),
                *(["-ExplicitUtility"] if explicit_utility else []),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
        )
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and any(
        process.poll() is None for process in processes.values()
    ):
        time.sleep(0.05)
    results: dict[str, bool] = {}
    for name, process in processes.items():
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
            _stage(f"parser-{name}-timeout")
            results[name] = False
        else:
            _stage(f"parser-{name}-exit-{process.returncode}")
            results[name] = process.returncode == 0
    return results


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
        config = root / "config.json"
        config.write_text(json.dumps({"ok": True}))
        parser = root / "parse-config.ps1"
        parser.write_text(
            "param([Parameter(Mandatory=$true)][string]$Config,"
            "[switch]$ExplicitUtility)\n"
            "$ErrorActionPreference = 'Stop'\n"
            "if ($ExplicitUtility) {\n"
            ' Import-Module "$PSHOME\\Modules\\Microsoft.PowerShell.Utility\\Microsoft.PowerShell.Utility.psd1"\n'
            " $text = [System.IO.File]::ReadAllText($Config)\n"
            "} else { $text = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 }\n"
            "$parsed = $text | ConvertFrom-Json\n"
            "if ($parsed.ok -ne $true) { throw 'invalid parsed value' }\n"
            "exit 0\n"
        )
        explicit_results = _parser_matrix(
            powershell,
            parser,
            config,
            environment,
            {"explicit-utility": ()},
            explicit_utility=True,
        )
        groups = {
            "filtered": (),
            "full": tuple(os.environ),
            "program-paths": ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"),
            "shared-profile": ("PROGRAMDATA", "ALLUSERSPROFILE", "SYSTEMDRIVE"),
            "machine-user": ("USERDOMAIN", "USERNAME", "COMPUTERNAME"),
        }
        group_results = _parser_matrix(powershell, parser, config, environment, groups)
        candidates = {
            variable
            for name, variables in groups.items()
            if name not in {"filtered", "full"} and group_results[name]
            for variable in variables
        }
        if not candidates:
            candidates = {
                variable
                for name, variables in groups.items()
                if name not in {"filtered", "full"}
                for variable in variables
            }
        individual_results = _parser_matrix(
            powershell,
            parser,
            config,
            environment,
            {f"only-{name}": (name,) for name in sorted(candidates)},
        )
    return (
        0
        if explicit_results["explicit-utility"] or any(individual_results.values())
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
