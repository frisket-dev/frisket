"""Best-effort local disk headroom checks before bounded writes."""

from __future__ import annotations

import errno
import shutil
from pathlib import Path


DISK_HEADROOM_BYTES = 256 * 1024 * 1024


def require_disk_headroom(
    destination: str | Path,
    imminent_write_bytes: int,
    *,
    margin_bytes: int = DISK_HEADROOM_BYTES,
) -> None:
    """Require current free space for one imminent write plus fixed headroom.

    This is a point-in-time safeguard, not a reservation or quota. Bytes already
    written are already reflected in filesystem free space and are not subtracted
    again.
    """

    if (
        type(imminent_write_bytes) is not int
        or imminent_write_bytes < 0
        or type(margin_bytes) is not int
        or margin_bytes < 0
    ):
        raise ValueError("disk headroom sizes must be non-negative integers")
    free = shutil.disk_usage(Path(destination)).free
    required = imminent_write_bytes + margin_bytes
    if free < required:
        raise OSError(
            errno.ENOSPC,
            "insufficient disk space for the next write and safety headroom",
            str(destination),
        )


__all__ = ["DISK_HEADROOM_BYTES", "require_disk_headroom"]
