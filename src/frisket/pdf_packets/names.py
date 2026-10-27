"""Render safe, literal PDF child filenames for review and publication."""

import re
from pathlib import Path


_TOKEN = re.compile(r"\{([^{}]*)\}")


def child_filename(
    pattern: str, source_filename: str, index: int, start: int, end: int
) -> str:
    fields = {
        "packet": re.sub(
            r"\.pdf$",
            "",
            Path(source_filename.replace("\\", "/")).name,
            flags=re.IGNORECASE,
        ),
        "index": str(index),
        "start": str(start),
        "end": str(end),
        "page": str(start),
    }
    if any(match.group(1) not in fields for match in _TOKEN.finditer(pattern)):
        raise ValueError("name pattern uses an unknown field")
    rest = _TOKEN.sub("", pattern)
    if "{" in rest or "}" in rest:
        raise ValueError("name pattern has invalid braces")
    name = _TOKEN.sub(lambda match: fields[match.group(1)], pattern).strip()
    if not name:
        raise ValueError("name pattern produced an empty filename")
    name = name if name.lower().endswith(".pdf") else f"{name}.pdf"
    if "/" in name or "\\" in name or "\x00" in name or len(name.encode("utf-8")) > 255:
        raise ValueError("generated filenames must fit one component and 255 bytes")
    return name
