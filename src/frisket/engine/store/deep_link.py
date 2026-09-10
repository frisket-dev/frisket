from __future__ import annotations


def compose_deep_link(
    *,
    webpage_url: str | None,
    extractor: str | None,
    yt_dlp_id: str | None,
    start_ms: int | None,
) -> str | None:
    """Return an "open at timestamp" URL for a temporal span, or ``None``.

    ``webpage_url`` is the only hard requirement -- without it there is
    nothing to link to. When the extractor is recognized (YouTube today)
    and a video id + a non-negative ``start_ms`` are available, the
    timestamp is composed into a canonical watch URL; otherwise the plain
    ``webpage_url`` is returned unchanged (still useful -- the platform page
    itself -- just without a timestamp we can't honestly construct).
    """
    if not webpage_url:
        return None
    if (
        yt_dlp_id
        and start_ms is not None
        and start_ms >= 0
        and _is_youtube(extractor, webpage_url)
    ):
        return f"https://www.youtube.com/watch?v={yt_dlp_id}&t={start_ms // 1000}s"
    return webpage_url


def _is_youtube(extractor: str | None, webpage_url: str) -> bool:
    if extractor and "youtube" in extractor.lower():
        return True
    lowered = webpage_url.lower()
    return "youtube.com" in lowered or "youtu.be" in lowered
