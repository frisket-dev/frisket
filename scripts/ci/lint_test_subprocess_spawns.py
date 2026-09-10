#!/usr/bin/env python3
"""Subprocess-spawn lint: tests don't spawn Python for CLI output/effects.

Every `sys.executable` spawn pays ~1.1-1.3s of interpreter+import cost.
Tests that only drive `frisket.cli` for its output or effects go through
`tests/helpers.py:run_cli` (in-process; snapshots argv/stdin/env/cwd/logging)
instead — the 2026-07 speed-up migration moved every such site. A real
subprocess stays legitimate ONLY when the test asserts a genuine process
property: signals and crash exit codes, child liveness/supervision, env
inheritance or scrubbing across exec, sandbox/network isolation of a real
child, cold-import semantics of a fresh interpreter, console-script or
`python -m` entry-point wiring, or cross-process visibility (locks, SQLite).

A new `sys.executable` in tests therefore fails this lint unless the file
carries a `# subprocess-boundary: <reason>` marker (on the offending line,
the line above, or file-level near the spawner it justifies) or an ALLOWED
entry below. Heuristic and false-negative-tolerant by design (a token inside
a string literal counts; that is what the marker is for).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
TEST_DIRS = ("tests", "sidecar/tests")

TOKEN = re.compile(r"\bsys\.executable\b")
MARKER = re.compile(r"#\s*subprocess-boundary:\s*\S")

# Exceptions must name the process property under test. Delete entries migrated
# to run_cli; prefer an in-file marker for new exceptions.
ALLOWED: dict[str, str] = {
    "tests/helpers.py": "run_cli docstring cites the subprocess.run spelling it replaces",
    "tests/engine/test_sandbox.py": "sandbox network/filesystem isolation of real children",
    "tests/engine/test_sandbox_process.py": "sandboxed child process semantics",
    "tests/engine/test_sandbox_cancellation.py": "kills real process trees; liveness/exit codes",
    "tests/engine/test_media_metadata.py": "resolved-interpreter sandbox spawns and process trees",
    "tests/ops/test_ytdlp_download.py": "realtime child kill/timeout semantics",
    "tests/ops/test_ocr_runtime_availability.py": "real OCR runtime probes in fresh interpreters",
    "tests/features/test_entities_extra_gating.py": "real frisket.plugins.subprocess_runner children",
    "tests/authoring/test_authoring_cli_polish.py": "plugin dev watch loops: liveness, terminate/kill, streamed stdout",
    "tests/engine/test_worker.py": "asserts `python -m frisket.cli worker` module runnability serve relies on",
    "tests/engine/test_transcribe_engines.py": "worker stdio protocol child with PYTHONPATH injection",
    "tests/engine/test_job_queue.py": "cross-process queue-db visibility",
    "tests/engine/test_queue_schema_migrations.py": "hosted-worker children against real postgres, env+timeout",
    "tests/runtime_foundation_test_helpers.py": "queue provisioning children: env, timeouts, long-lived Popen",
    "tests/test_import_boundaries_resolve.py": "fresh-interpreter import-boundary resolution",
    "tests/test_direct_action_validation_registry.py": "fresh-interpreter registry closure",
    "tests/test_action_contract_definition_registry.py": "fresh-interpreter registry closure",
    "tests/engine/test_grounding_conformance.py": "fresh-interpreter conformance probe",
    "tests/server/test_server_workspace_schema_boundary.py": "cold-import boundary of frisket.server.app",
    "tests/server/test_recipe_registry.py": "fresh-interpreter registry probe",
    "tests/test_cli_main.py": "[project.scripts] frisket console-script process behavior",
    "tests/test_remote_cli.py": "console-script wiring over a subprocess (test says so by name)",
    "tests/engine/test_cli_doctor.py": "console-script doctor: clean stdout/stderr at process boundary",
    "tests/operability/test_embeddings_doctor.py": "console-script doctor --json parses process stdout",
    "tests/plugin_contract/test_plugin_backend_gauntlet.py": "console-script gauntlet: unpolluted stdout/stderr",
    "tests/server/test_http_contracts_sync.py": "runs scripts/ci/gen_http_contracts.py --check as a process",
    "tests/server/test_ocr_engine_catalog_sync.py": "runs scripts/dev/sync_ocr_engine_catalog.py --check as a process",
    "tests/team/test_split_team_entrypoint.py": "cross-process key issue/save visibility",
    "tests/team/test_team_entrypoint_hardening.py": "parallel real processes against one deployment",
    "tests/team/test_team_bootstrap_queue_table_allowlist.py": "fresh-process bootstrap allowlist probe",
    "tests/team/test_byo_keys.py": "cross-process key material handling",
    "sidecar/tests/test_gpu_lease.py": "real child processes contending a GPU lease lock file",
    "tests/ops/test_media_proxy.py": "asserts the argv frisket builds for yt-dlp; nothing is spawned",
    "tests/ops/test_media_ytdlp_managed_runtime_cutover.py": "asserts the argv frisket builds for yt-dlp; nothing is spawned",
}


def offending_lines(path: Path) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    offenders: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        if not TOKEN.search(line):
            continue
        if MARKER.search(line) or (idx > 0 and MARKER.search(lines[idx - 1])):
            continue
        offenders.append((idx + 1, line.strip()))
    return offenders


def main() -> int:
    found: dict[str, list[tuple[int, str]]] = {}
    for test_dir in TEST_DIRS:
        root = REPO / test_dir
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            offenders = offending_lines(path)
            if offenders:
                found[rel] = offenders

    failures: list[str] = []
    for rel, offenders in sorted(found.items()):
        if rel in ALLOWED:
            continue
        for lineno, text in offenders:
            print(f"{rel}:{lineno}: {text}")
        failures.append(
            f"{rel}: {len(offenders)} sys.executable line(s) — drive the CLI "
            "in-process via tests/helpers.py:run_cli, or (only for a genuine "
            "process-boundary assertion) annotate `# subprocess-boundary: "
            "<reason>`"
        )
    for rel in sorted(set(ALLOWED) - set(found)):
        failures.append(f"{rel}: allowlisted but clean (or gone) — delete its entry")

    if failures:
        print(
            "\nsubprocess-spawn lint failed (tests/helpers.py run_cli is the "
            "in-process route):",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
