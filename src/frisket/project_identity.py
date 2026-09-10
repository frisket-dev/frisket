"""Stable physical identity for one project's durable stores."""

from __future__ import annotations

import string
from dataclasses import dataclass

# Windows reserved device stems (case-insensitive); a slug like "con" would
# collide with the CON device even as "con.frisket".
_RESERVED_WINDOWS_DEVICE_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{d}" for d in range(1, 10)}
    | {f"LPT{d}" for d in range(1, 10)}
)

_SLUG_ALLOWED_CHARS = frozenset(string.ascii_letters + string.digits + "-_")


def validate_project_slug(value: str) -> str:
    """Validate a project slug used as a filesystem path segment on every
    supported platform, including native Windows.

    Requires a ``str`` of length 1 through 96, without trimming or other
    normalization; allows only ASCII letters, ASCII digits, ``-``, and
    ``_``; and rejects (case-insensitively) the Windows reserved device
    stems CON, PRN, AUX, NUL, COM1-COM9, and LPT1-LPT9 -- a plain slug like
    "con" still collides with the CON device once suffixed (e.g.
    "con.frisket"). Returns the unchanged valid string, or raises
    ValueError.
    """
    if not isinstance(value, str):
        raise ValueError("project slug must be a string")
    if not 1 <= len(value) <= 96:
        raise ValueError("project slug must be 1 to 96 characters")
    if not _SLUG_ALLOWED_CHARS.issuperset(value):
        raise ValueError(
            "project slug may only contain ASCII letters, digits, '-', and '_'"
        )
    if value.upper() in _RESERVED_WINDOWS_DEVICE_STEMS:
        raise ValueError(f"project slug {value!r} is a reserved Windows device name")
    return value


@dataclass(frozen=True, slots=True)
class ProjectStorageKey:
    """Storage account plus project slug, independent of funding ownership."""

    storage_org_id: int
    project_slug: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.storage_org_id, int)
            or isinstance(self.storage_org_id, bool)
            or self.storage_org_id < 1
        ):
            raise ValueError("storage_org_id must be a positive integer")
        validate_project_slug(self.project_slug)
