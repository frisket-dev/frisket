"""Named worker for one user-supplied Python recipe.

The fixed runtime bootstrap installs the sandbox policy before importing this
module. Recipe source and its payload arrive as stdin request data, never as
argv or generated bootstrap code.
"""

from __future__ import annotations

import json
import io
import sys
from typing import Any


def _request(value: object) -> tuple[str, dict[str, Any]]:
    if not isinstance(value, dict) or set(value) != {"source", "payload"}:
        raise ValueError("recipe request must contain source and payload")
    source = value["source"]
    payload = value["payload"]
    if not isinstance(source, str):
        raise ValueError("recipe source must be a string")
    if not isinstance(payload, dict):
        raise ValueError("recipe payload must be an object")
    return source, payload


def main(_argv: list[str] | None = None) -> int:
    source, payload = _request(json.load(sys.stdin))
    # Preserve the public recipe contract: recipe source reads its row payload
    # from stdin exactly as it did under ``python -c``.  The outer envelope is
    # an implementation detail consumed before untrusted source starts.
    stream = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")))
    sys.stdin = stream
    sys.argv = ["-c"]
    namespace: dict[str, Any] = {"__name__": "__main__"}
    exec(compile(source, "<frisket-recipe>", "exec"), namespace, namespace)
    return 0
