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

import importlib
import importlib.util
import fnmatch
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOUNDARY_CONFIG = ROOT / "scripts" / "ci" / "import_boundaries.json"

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


def test_every_protected_member_resolves_to_a_live_owner_api() -> None:
    # rule19: diffs independently maintained boundary policy against live owner APIs
    payload = json.loads(BOUNDARY_CONFIG.read_text(encoding="utf-8"))
    missing: list[str] = []
    for rule in payload["rules"]:
        for reference in rule.get("forbidden_members", ()):
            parts = reference.split(".")
            owner = None
            members: list[str] = []
            for count in range(len(parts) - 1, 0, -1):
                module_name = ".".join(parts[:count])
                try:
                    spec = importlib.util.find_spec(module_name)
                except (ModuleNotFoundError, AttributeError):
                    continue
                if spec is not None:
                    owner = importlib.import_module(module_name)
                    members = parts[count:]
                    break
            for member in members:
                matches = fnmatch.filter(dir(owner), member)
                if not matches:
                    owner = None
                    break
                if "*" not in member:
                    owner = getattr(owner, member)
            if owner is None:
                missing.append(reference)
    assert not missing, f"protected member rules fence missing APIs: {missing}"
