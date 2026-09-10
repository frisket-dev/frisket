from __future__ import annotations

import contextlib
import io
import json
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from frisket.authoring.plugin_build import FRONTEND_ENTRY_CANDIDATES, plugin_build

TRUSTED_LOCAL_BACKEND_CAPABILITY = "plugin:trusted_local_backend"

# plugin.config.mjs and plugin.py are always source. `_mtime_snapshot` also
# watches every other top-level *.py file next to plugin.py (a shared helper
# module an author splits out), so this tuple only needs to name the two
# non-.py-glob-matched/always-required files. frontend/plugin.js is source
# only when there is no .ts/.tsx/.jsx sibling for build to compile it from --
# otherwise it is generated output (plugin_build.py's own compile target)
# and watching it would make dev chase its own writes. plugin.json,
# workbench-descriptors.json, and .frisket-sdk/ are never watched: plugin_build
# never writes a *.py file at plugin_root, and none of them are under frontend/.
_ALWAYS_WATCHED_RELATIVE_PATHS = ("plugin.config.mjs", "plugin.py")


class _ServerCallError(RuntimeError):
    """Raised when an install/activate route does not report success."""


def plugin_dev(
    plugin_root: Path,
    *,
    project_id: str,
    server: str,
    once: bool,
    poll_interval: float = 0.5,
) -> int:
    plugin_root = plugin_root.resolve()
    with httpx.Client(base_url=server, timeout=30.0) as client:
        if once:
            return _run_cycle(client, plugin_root, project_id=project_id)
        return _watch(
            client,
            plugin_root,
            project_id=project_id,
            poll_interval=poll_interval,
        )


def _build(plugin_root: Path) -> int:
    """Run `plugin_build` as a black box, but keep its stdout (the success-path
    validation report) off stdout: dev's stdout is reserved for the single
    receipt JSON blob a caller/test parses. `plugin_build` writes errors
    straight to stderr already (unaffected by this redirect), so failure
    messages are unchanged."""
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        result = plugin_build(plugin_root)
    report = captured.getvalue()
    if report:
        sys.stderr.write(report)
    return result


def _run_cycle(client: httpx.Client, plugin_root: Path, *, project_id: str) -> int:
    build_result = _build(plugin_root)
    if build_result != 0:
        print(
            f"error: build failed for {plugin_root} (see errors above); "
            "prior receipt undisturbed",
            file=sys.stderr,
        )
        return build_result

    manifest_path = plugin_root / "plugin.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: could not read {manifest_path}: {exc}", file=sys.stderr)
        return 1

    plugin_id = str(manifest.get("id") or "")
    if not plugin_id:
        print(f"error: {manifest_path} has no plugin id", file=sys.stderr)
        return 1
    requires = manifest.get("requires")
    raw_capabilities = (
        requires.get("capabilities", []) if isinstance(requires, dict) else []
    )
    capabilities = [item for item in raw_capabilities if isinstance(item, str)]

    try:
        receipt_id = _install_local(
            client,
            project_id=project_id,
            plugin_id=plugin_id,
            plugin_root=plugin_root,
        )
        _activate(
            client,
            project_id=project_id,
            plugin_id=plugin_id,
            receipt_id=receipt_id,
            capabilities=capabilities,
        )
        backend_activated = False
        if TRUSTED_LOCAL_BACKEND_CAPABILITY in capabilities:
            _activate_backend(client, project_id=project_id, plugin_id=plugin_id)
            backend_activated = True
    except _ServerCallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        # A single line, no traceback: unreachable/dropped --server connections
        # (httpx.ConnectError, ReadTimeout, ...) are an expected dev-loop event,
        # not a bug -- --once must exit clean and watch mode (_watch, which
        # always calls through this same function) must report and keep
        # polling rather than dying with a stack trace.
        print(
            f"error: could not reach {client.base_url} "
            f"({exc.__class__.__name__}): {exc}",
            file=sys.stderr,
        )
        return 1

    print(
        json.dumps(
            {
                "pluginId": plugin_id,
                "projectId": project_id,
                "receiptId": receipt_id,
                "backendActivated": backend_activated,
            },
            indent=2,
            sort_keys=True,
        ),
        flush=True,
    )
    return 0


def _install_local(
    client: httpx.Client,
    *,
    project_id: str,
    plugin_id: str,
    plugin_root: Path,
) -> str:
    response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/install-local",
        json={
            "source": {"kind": "localPath", "value": str(plugin_root)},
            "arbitraryPackageLoadAllowed": False,
        },
    )
    _raise_if_not_ok(response, step="install-local")
    receipt_id = str(response.json().get("receiptId") or "")
    if not receipt_id:
        raise _ServerCallError(f"install-local returned no receiptId: {response.text}")
    return receipt_id


def _activate(
    client: httpx.Client,
    *,
    project_id: str,
    plugin_id: str,
    receipt_id: str,
    capabilities: list[str],
) -> None:
    response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/activate",
        json={
            "receiptId": receipt_id,
            "trustAcknowledged": True,
            "permissionsAccepted": capabilities,
            "arbitraryPackageLoadAllowed": False,
        },
    )
    _raise_if_not_ok(response, step="activate")


def _activate_backend(client: httpx.Client, *, project_id: str, plugin_id: str) -> None:
    response = client.post(
        f"/api/projects/{project_id}/workbench/plugins/{plugin_id}/backend/activate",
        json={
            "trustAcknowledged": True,
            "arbitraryPackageLoadAllowed": False,
            "executableHandlersAllowed": True,
        },
    )
    _raise_if_not_ok(response, step="backend/activate")


def _raise_if_not_ok(response: httpx.Response, *, step: str) -> None:
    if response.status_code != 200:
        raise _ServerCallError(
            f"{step} failed: HTTP {response.status_code}: {response.text}"
        )


def _watch(
    client: httpx.Client,
    plugin_root: Path,
    *,
    project_id: str,
    poll_interval: float,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    print(
        f"frisket plugin dev: watching {plugin_root} for changes (Ctrl-C to stop)",
        flush=True,
    )
    exit_code = _run_cycle(client, plugin_root, project_id=project_id)
    snapshot = _mtime_snapshot(plugin_root)
    try:
        while True:
            sleep(poll_interval)
            current = _mtime_snapshot(plugin_root)
            if current != snapshot:
                snapshot = current
                exit_code = _run_cycle(client, plugin_root, project_id=project_id)
    except KeyboardInterrupt:
        print("frisket plugin dev: stopped", flush=True)
    return exit_code


def _mtime_snapshot(plugin_root: Path) -> dict[str, float]:
    snapshot: dict[str, float] = {}
    for relative in _ALWAYS_WATCHED_RELATIVE_PATHS:
        path = plugin_root / relative
        if path.is_file():
            snapshot[relative] = path.stat().st_mtime

    # Workspace-root *.py siblings of plugin.py (e.g. a shared helper module
    # plugin.py imports) are source too, so watch every top-level *.py file,
    # not just plugin.py -- editing a sibling must trigger a rebuild.
    # Top-level only (not recursive): frontend/*.py, if any, is already
    # covered by the frontend/ walk below, and plugin_build.py never writes a
    # *.py file at plugin_root (only plugin.json, workbench-descriptors.json,
    # and .frisket-sdk/*.mjs), so there is no generated artifact to exclude.
    for path in sorted(plugin_root.glob("*.py")):
        if not path.is_file():
            continue
        relative = path.name
        if relative not in snapshot:
            snapshot[relative] = path.stat().st_mtime

    entry = next(
        (
            candidate
            for candidate in FRONTEND_ENTRY_CANDIDATES
            if (plugin_root / candidate).is_file()
        ),
        None,
    )
    generated_compiled_output = (
        "frontend/plugin.js"
        if entry is not None and entry != "frontend/plugin.js"
        else None
    )
    frontend_dir = plugin_root / "frontend"
    if frontend_dir.is_dir():
        for path in frontend_dir.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(plugin_root).as_posix()
            if relative == generated_compiled_output:
                continue
            snapshot[relative] = path.stat().st_mtime
    return snapshot
