#!/usr/bin/env python3
"""Pin the ambient-environment reads of the transcription dispatch files.

The routed path takes placement facts (endpoint, credentials, hardware class,
deployment shape) from the route binding it was admitted under — a
``ConnectionConfig`` resolved once at mint — never from ``os.environ`` at
dispatch time. An env read that survives here is a fact that can differ
between the estimate the user consented to and the call that bills them, and
one such read handed boto3 an AMBIENT-credential client whenever the
configured key was missing.

So each governed file declares the exact multiset of env names it may read,
and any new read fails until someone widens the pin deliberately. Structural
(``ast``), not a grep: a name in a comment or a docstring is not a read.

This lives outside pytest because a source-text check is not a test.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src" / "frisket"

# Documented pre-route fallbacks; sorted, with duplicates counted.
ALLOWED: dict[str, list[str]] = {
    "engine/executor/transcription_read.py": [],
    "sdk/ops/transcribe_engines.py": [],
    "sdk/ops/transcription/common.py": [],
    "sdk/ops/transcription/faster_whisper.py": [],
    "sdk/ops/transcription/hosted.py": [],
    "sdk/ops/transcription/parakeet.py": [],
    "sdk/ops/transcription/sidecar.py": [],
    "ops/_sidecar.py": [
        # STATUS/DOCTOR only; sidecar_post receives an explicit binding.
        "FRISKET_MODELS_TOKEN",
        "FRISKET_MODELS_URL",
    ],
}


def env_names(tree: ast.Module) -> list[str]:
    """Literal keys read out of ``os.environ`` — ``os.environ["X"]``,
    ``os.environ.get("X")``, ``os.getenv("X")``."""
    found: list[str] = []

    def is_environ(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )

    for node in ast.walk(tree):
        key: ast.expr | None = None
        if isinstance(node, ast.Subscript) and is_environ(node.value):
            key = node.slice
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr in {"get", "pop", "setdefault"}
                and is_environ(func.value)
            ) or (
                isinstance(func, ast.Attribute)
                and func.attr in {"getenv", "environb"}
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ):
                key = node.args[0] if node.args else None
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            found.append(key.value)
    return sorted(found)


def main() -> int:
    failures: list[str] = []
    for rel, allowed in sorted(ALLOWED.items()):
        path = SRC / rel
        if not path.is_file():
            failures.append(f"{rel}: governed file missing — retire or move the pin")
            continue
        found = env_names(
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        )
        if found != sorted(allowed):
            failures.append(f"{rel}: reads {found}, pin allows {sorted(allowed)}")

    if failures:
        print(
            "placement env-read pin failed — the routed path must take placement "
            "facts from the route binding (ConnectionConfig):",
            file=sys.stderr,
        )
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
