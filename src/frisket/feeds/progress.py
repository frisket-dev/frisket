"""Metadata-listing progress through yt-dlp's supported per-entry filter hook."""

from collections.abc import Callable
from typing import Any


def listing_progress_options(
    on_progress: Callable[[int, int | None], None] | None,
    should_cancel: Callable[[], bool] | None,
    limit: int | None,
) -> dict[str, Any]:
    if on_progress is None and should_cancel is None:
        return {}
    seen: set[str] = set()

    def observe(info, *, incomplete=False):
        if should_cancel and should_cancel():
            raise RuntimeError("Feed import cancelled")
        # The filter is also called for playlist headers and again after an
        # entry is resolved. Count each discovered post once, not each hook.
        identity = info.get("id")
        if (
            identity
            and info.get("playlist_index") is not None
            and str(identity) not in seen
        ):
            seen.add(str(identity))
            if on_progress:
                on_progress(len(seen), limit)
        return None

    return {"match_filter": observe, "lazy_playlist": True}
