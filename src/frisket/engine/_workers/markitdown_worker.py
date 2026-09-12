"""One-shot MarkItDown worker with a JSON-in/file-out protocol.

Optional conversion libraries stay inside :func:`main`, after the sandbox owns
the process.
"""

from __future__ import annotations

import json
import logging
import sys
import warnings
from typing import Any


def _write(payload: dict[str, Any], value: dict[str, str]) -> None:
    with open(payload["out"], "w") as handle:
        json.dump(value, handle)


def main() -> None:
    payload = json.load(sys.stdin)
    logging.disable(logging.WARNING)
    warnings.filterwarnings("ignore")
    try:
        from markitdown import MarkItDown
    except ImportError:
        _write(
            payload,
            {
                "error": "markitdown not installed in this environment even though "
                "it is part of the base Frisket install; repair or reinstall Frisket"
            },
        )
        return
    converter = MarkItDown(enable_plugins=False)
    try:
        result = converter.convert(payload["path"])
    except Exception as error:  # markitdown raises per-format exceptions
        _write(payload, {"error": f"{type(error).__name__}: {error}"})
        return
    text = getattr(result, "markdown", None)
    if text is None:
        text = getattr(result, "text_content", "")
    _write(payload, {"markdown": text or ""})


if __name__ == "__main__":
    main()
