"""Every module path named in the boundary config must actually resolve.

Boundary rules decay silently when a governed module is renamed:
scripts/ci/import_boundaries.json rules simply match nothing, and a
forbidden-import entry keeps "passing" against a module that no longer
exists. This test pins every frisket-rooted module reference in the config
to a real importable module, so a rename that misses a config entry fails
loudly here instead of leaving a fence around a ghost.

Deliberate exception: the no-public-to-private rule names an optional
composition package's namespace (frisket.hosted / frisket.composition /
frisket_composition), which is absent from this tree BY DESIGN — the fence
exists precisely so this code never imports it. Those prefixes are
allowlisted.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOUNDARY_CONFIG = ROOT / "scripts" / "ci" / "import_boundaries.json"
CHECKER = ROOT / "scripts" / "ci" / "check_import_boundaries.py"

# Prefixes that are intentionally absent from this tree (an optional
# composition package). Everything else named in the boundary config must
# resolve.
DELIBERATELY_ABSENT_PREFIXES = (
    "frisket.hosted",
    "frisket.composition",
    "frisket_composition",
)


def _boundary_json_references() -> set[str]:
    # rule19: diffs two independently maintained sources (boundary config vs live import tree)
    payload = json.loads(BOUNDARY_CONFIG.read_text(encoding="utf-8"))
    names: set[str] = set()
    for rule in payload["rules"]:
        for field in ("modules", "exclude", "allowed_imports", "forbidden_imports"):
            names.update(rule.get(field, ()))
    return names


def _checkable(name: str) -> str | None:
    """Reduce a config reference to an importable module path, or None."""
    base = name[:-2] if name.endswith(".*") else name
    if not base.startswith("frisket"):
        return None  # external package reference (e.g. yt_dlp)
    if base.startswith(DELIBERATELY_ABSENT_PREFIXES):
        return None
    return base


def test_every_import_boundaries_json_module_resolves() -> None:
    missing: list[str] = []
    for name in sorted(_boundary_json_references()):
        base = _checkable(name)
        if base is None:
            continue
        try:
            spec = importlib.util.find_spec(base)
        except ModuleNotFoundError:
            spec = None
        if spec is None:
            missing.append(name)
    assert not missing, (
        f"{BOUNDARY_CONFIG} names module paths that do not resolve — the rule "
        f"is fencing ghosts and must be updated: {missing}"
    )


def test_import_boundary_rules_hold() -> None:
    # A subprocess keeps the checker's filesystem walk and exit handling
    # identical to the CI invocation, and keeps any process-scoped side
    # effects out of the pytest run.
    proc = subprocess.run(
        [
            sys.executable,
            str(CHECKER),
            "--source-root",
            str(ROOT / "src"),
            "--config",
            str(BOUNDARY_CONFIG),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        "import boundary rules broke — a module reached across an "
        "architectural layer, or a rule's governed-module pattern no longer "
        "resolves (`governed module missing`). Reproduce with `uv run python "
        "scripts/ci/check_import_boundaries.py --source-root src --config "
        f"scripts/ci/import_boundaries.json`.\n{proc.stdout}{proc.stderr}"
    )
