"""Unused names for newly created sheets."""

from typing import Any


def available_sheet_name(db: Any, requested: str, *, max_length: int = 80) -> str:
    base = requested.strip()[:max_length]
    name = base
    suffix = 2
    while db.execute("SELECT 1 FROM sheets WHERE name=?", (name,)).fetchone():
        ending = f"-{suffix}"
        name = f"{base[: max_length - len(ending)]}{ending}"
        suffix += 1
    return name
