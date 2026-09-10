from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB_WORKBENCH = ROOT / "web" / "src" / "workbench"
PLUGIN_RUNTIME_DESCRIPTORS = WEB_WORKBENCH / "pluginRuntimeDescriptors.ts"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _plugin_legal_placement_pairs() -> dict[str, set[tuple[str, str]]]:
    match = re.search(
        r"export const PLUGIN_LEGAL_PLACEMENTS[^=]*=\s*\{(?P<body>.*?)\n\};",
        _source(PLUGIN_RUNTIME_DESCRIPTORS),
        flags=re.DOTALL,
    )
    assert match, "PLUGIN_LEGAL_PLACEMENTS table not found"
    pairs: dict[str, set[tuple[str, str]]] = {}
    for kind_match in re.finditer(
        r"(?P<kind>\w+):\s*\[(?P<rows>[^\]]*)\]", match.group("body")
    ):
        rows = set()
        for row in re.finditer(
            r"\{\s*host:\s*'(?P<host>[A-Za-z]+)',\s*mode:\s*'(?P<mode>[A-Za-z]+)'\s*\}",
            kind_match.group("rows"),
        ):
            rows.add((row.group("host"), row.group("mode")))
        pairs[kind_match.group("kind")] = rows
    return pairs
