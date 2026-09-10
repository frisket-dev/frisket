"""The built-in first-party matcher table.

Registered at import time (via the package ``__init__``). Delegation posture
(§1.6): the table owns only the media-vs-collection-vs-page automatic ROUTING
decision. These matchers power suggestions and implicit routing for platforms
Frisket recognizes; they are not an admission list. The explicit Download media
action accepts any HTTP(S) URL and lets yt-dlp determine extractor support.

All first-party matchers reserve ``priority >= PLUGIN_PRIORITY_CEILING`` (1000);
the ``page`` fallback is universal (no domains) and deliberately last-resort.
"""

from __future__ import annotations

from frisket.features.url_classification.matchers import (
    PLUGIN_PRIORITY_CEILING,
    UrlMatcher,
)

_MEDIA_CAP = ("external:media_download",)
_COLLECTION_CAP = ("external:collection_enumerate",)
_PAGE_CAP = ("external:http_fetch",)

FP = PLUGIN_PRIORITY_CEILING  # first-party base priority (1000)
YTDLP_DOWNLOAD_ACTION_KIND = "media.ytdlp_download"

YTDLP_SINGLE_MEDIA_MATCHERS: tuple[UrlMatcher, ...] = (
    # --- YouTube video (media) -----------------------------------------
    UrlMatcher(
        matcher_id="firstparty.youtube.video.watch",
        provider="youtube",
        registered_domains=("youtube.com",),
        kind="media",
        path_regex=r"/watch/?(?:\?.*)?",
        query_requires=("v",),
        handler_action_kind=YTDLP_DOWNLOAD_ACTION_KIND,
        params_hints={"media_type": "audio"},
        capabilities=_MEDIA_CAP,
        priority=FP,
    ),
    UrlMatcher(
        matcher_id="firstparty.youtube.video.shorts",
        provider="youtube",
        registered_domains=("youtube.com",),
        kind="media",
        path_regex=r"/shorts/[A-Za-z0-9_-]+/?(?:\?.*)?",
        handler_action_kind=YTDLP_DOWNLOAD_ACTION_KIND,
        params_hints={"media_type": "video"},
        capabilities=_MEDIA_CAP,
        priority=FP,
    ),
    UrlMatcher(
        matcher_id="firstparty.youtube.video.live",
        provider="youtube",
        registered_domains=("youtube.com",),
        kind="media",
        path_regex=r"/live/[A-Za-z0-9_-]{4,}/?(?:\?.*)?",
        handler_action_kind=YTDLP_DOWNLOAD_ACTION_KIND,
        params_hints={"media_type": "video"},
        capabilities=_MEDIA_CAP,
        priority=FP,
    ),
    UrlMatcher(
        matcher_id="firstparty.youtube.video.embed",
        provider="youtube",
        registered_domains=("youtube.com",),
        kind="media",
        path_regex=r"/embed/[A-Za-z0-9_-]{4,}/?(?:\?.*)?",
        handler_action_kind=YTDLP_DOWNLOAD_ACTION_KIND,
        params_hints={"media_type": "video"},
        capabilities=_MEDIA_CAP,
        priority=FP,
    ),
    UrlMatcher(
        matcher_id="firstparty.youtube.video.short",
        provider="youtube",
        registered_domains=("youtu.be",),
        kind="media",
        # youtu.be short links are a single video-id path segment.
        path_regex=r"/[A-Za-z0-9_-]{4,}/?(?:\?.*)?",
        handler_action_kind=YTDLP_DOWNLOAD_ACTION_KIND,
        params_hints={"media_type": "audio"},
        capabilities=_MEDIA_CAP,
        priority=FP,
    ),
    # --- TikTok video ---------------------------------------------------
    UrlMatcher(
        matcher_id="firstparty.tiktok.video",
        provider="tiktok",
        registered_domains=("tiktok.com",),
        kind="media",
        path_regex=r"/@[^/]+/video/\d+/?(?:\?.*)?",
        handler_action_kind=YTDLP_DOWNLOAD_ACTION_KIND,
        capabilities=_MEDIA_CAP,
        priority=FP + 10,
    ),
    UrlMatcher(
        matcher_id="firstparty.tiktok.video.short",
        provider="tiktok",
        registered_domains=("vm.tiktok.com", "vt.tiktok.com"),
        kind="media",
        # vm.tiktok.com/<token> and vt.tiktok.com/<token> share links.
        path_regex=r"/[A-Za-z0-9_-]+/?(?:\?.*)?",
        handler_action_kind=YTDLP_DOWNLOAD_ACTION_KIND,
        capabilities=_MEDIA_CAP,
        priority=FP,
    ),
    UrlMatcher(
        matcher_id="firstparty.tiktok.video.short.www",
        provider="tiktok",
        registered_domains=("www.tiktok.com",),
        kind="media",
        path_regex=r"/t/[A-Za-z0-9_-]+/?(?:\?.*)?",
        handler_action_kind=YTDLP_DOWNLOAD_ACTION_KIND,
        capabilities=_MEDIA_CAP,
        priority=FP,
    ),
    # --- Vimeo video ----------------------------------------------------
    UrlMatcher(
        matcher_id="firstparty.vimeo.video",
        provider="vimeo",
        registered_domains=("vimeo.com",),
        kind="media",
        path_regex=r"/(?:video/)?\d+/?(?:\?.*)?",
        handler_action_kind=YTDLP_DOWNLOAD_ACTION_KIND,
        capabilities=_MEDIA_CAP,
        priority=FP,
    ),
    # --- Kick VOD / clip (live channel URLs are intentionally excluded) --
    UrlMatcher(
        matcher_id="firstparty.kick.vod",
        provider="kick",
        registered_domains=("kick.com",),
        kind="media",
        path_regex=(
            r"/[\w-]+/videos/[\da-fA-F]{8}-(?:[\da-fA-F]{4}-){3}"
            r"[\da-fA-F]{12}/?(?:\?.*)?"
        ),
        handler_action_kind=YTDLP_DOWNLOAD_ACTION_KIND,
        capabilities=_MEDIA_CAP,
        priority=FP + 10,
    ),
    UrlMatcher(
        matcher_id="firstparty.kick.clip.path",
        provider="kick",
        registered_domains=("kick.com",),
        kind="media",
        path_regex=r"/[\w-]+/clips/clip_[\w-]+/?(?:\?.*)?",
        handler_action_kind=YTDLP_DOWNLOAD_ACTION_KIND,
        capabilities=_MEDIA_CAP,
        priority=FP + 10,
    ),
    UrlMatcher(
        matcher_id="firstparty.kick.clip.query",
        provider="kick",
        registered_domains=("kick.com",),
        kind="media",
        path_regex=r"/[\w-]+/?\?.*",
        query_requires=("clip",),
        handler_action_kind=YTDLP_DOWNLOAD_ACTION_KIND,
        capabilities=_MEDIA_CAP,
        priority=FP + 10,
    ),
)

YTDLP_SINGLE_MEDIA_MATCHER_IDS = frozenset(
    matcher.matcher_id for matcher in YTDLP_SINGLE_MEDIA_MATCHERS
)
YTDLP_SINGLE_MEDIA_PROVIDERS = frozenset(
    matcher.provider for matcher in YTDLP_SINGLE_MEDIA_MATCHERS
)

FIRST_PARTY_MATCHERS: tuple[UrlMatcher, ...] = (
    *YTDLP_SINGLE_MEDIA_MATCHERS,
    # --- YouTube collection (channel / playlist) -----------------------
    UrlMatcher(
        matcher_id="firstparty.youtube.channel",
        provider="youtube",
        registered_domains=("youtube.com",),
        kind="collection",
        path_prefix=("/channel/", "/@", "/c/", "/user/"),
        query_forbids=("list",),
        handler_action_kind="derive.collection_expand",
        capabilities=_COLLECTION_CAP,
        expansion={
            "unit": "video",
            "enumerator": "youtube_channel",
            "default_cap": 100,
            "hard_cap": None,
            "requires_preview_count": True,
        },
        priority=FP,
    ),
    UrlMatcher(
        matcher_id="firstparty.youtube.playlist",
        provider="youtube",
        registered_domains=("youtube.com",),
        kind="collection",
        path_prefix=("/playlist",),
        query_requires=("list",),
        handler_action_kind="derive.collection_expand",
        capabilities=_COLLECTION_CAP,
        expansion={
            "unit": "video",
            "enumerator": "youtube_playlist",
            "default_cap": 100,
            "hard_cap": None,
            "requires_preview_count": True,
        },
        priority=FP,
    ),
    # --- Universal page fallback (last-resort, no domains) -------------
    UrlMatcher(
        matcher_id="firstparty.page",
        provider="generic",
        registered_domains=(),
        kind="page",
        handler_action_kind="media.fetch_url",
        capabilities=_PAGE_CAP,
        priority=FP // 2,
    ),
)


def register_first_party_matchers(*, replace: bool = False) -> None:
    """Register the whole first-party table (idempotent with replace=True)."""
    from frisket.features.url_classification.registry import register_matcher

    for matcher in FIRST_PARTY_MATCHERS:
        register_matcher(matcher, replace=replace)
