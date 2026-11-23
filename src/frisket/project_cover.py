"""A project's cover seed: the short opaque string its card cover derives from.

The seed is chosen when the project is created, so the New project preview
and the created card show the same cover. A project without one derives its
cover from its id, which is how every project created before seeds existed
keeps the cover it always had; nothing is migrated.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from pydantic import StringConstraints

COVER_SEED_META_KEY = "cover_seed"
COVER_SEED_PATTERN = r"^[A-Za-z0-9_-]{1,32}$"

CoverSeed = Annotated[str, StringConstraints(pattern=COVER_SEED_PATTERN)]

_COVER_SEED = re.compile(COVER_SEED_PATTERN)


def readable_cover_seed(value: object) -> str | None:
    """A stored seed, or None when it is absent or not a seed."""
    return value if isinstance(value, str) and _COVER_SEED.match(value) else None


def project_cover_seed(project: Any) -> str | None:
    """The seed a project stores, from its db like its other settings."""
    return readable_cover_seed(project.get_meta(COVER_SEED_META_KEY))


def write_cover_seed(project: Any, seed: str) -> None:
    """Store the seed in the project's meta and its manifest projection."""
    project.set_meta(COVER_SEED_META_KEY, seed)
    project._write_manifest(cover_seed=seed)
