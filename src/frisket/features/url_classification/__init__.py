"""Host-side URL classification registry.

Maps a URL to a small typed classification ``{kind, provider, capabilities,
handler, expansion?}`` via a matcher table. The registry is the single source
of truth for "what is this URL"; a JSON snapshot
(``src/frisket/data/url_classification_matchers.json``) is exported for the
frontend so backend validation and frontend affordances cannot drift.

Importing this package registers the first-party matcher table.

Regenerate the exported snapshot after changing the table::

    uv run python -m frisket.features.url_classification
"""

from __future__ import annotations

from collections.abc import Collection

from frisket.features.url_classification.contract import (
    ClassificationHandler,
    ExpansionHint,
    UrlClassification,
)
from frisket.features.url_classification.first_party import (
    YTDLP_DOWNLOAD_ACTION_KIND,
    YTDLP_SINGLE_MEDIA_MATCHER_IDS,
    YTDLP_SINGLE_MEDIA_PROVIDERS,
    register_first_party_matchers,
)
from frisket.features.url_classification.matchers import (
    MatcherRegistrationError,
    UrlMatcher,
)
from frisket.features.url_classification.registry import (
    classify_url,
    register_matcher,
    registered_matchers,
    unregister_matcher,
)

# Register the built-in matcher table at import time (replace=True keeps import
# idempotent under test reloads / repeated imports).
register_first_party_matchers(replace=True)


def is_supported_ytdlp_media_url(
    url: str, *, enabled_plugin_ids: Collection[str] = ()
) -> bool:
    """Whether ``url`` is recognized as media for automatic yt-dlp routing.

    This is an affordance helper, not an admission boundary. The explicit
    Download media action accepts any HTTP(S) URL and lets yt-dlp determine
    whether an extractor can handle it.
    """
    classification = classify_url(url, enabled_plugin_ids=enabled_plugin_ids)
    return bool(
        classification is not None
        and classification.kind == "media"
        and classification.handler.action_kind == YTDLP_DOWNLOAD_ACTION_KIND
    )


__all__ = [
    "ClassificationHandler",
    "ExpansionHint",
    "MatcherRegistrationError",
    "UrlClassification",
    "UrlMatcher",
    "YTDLP_DOWNLOAD_ACTION_KIND",
    "YTDLP_SINGLE_MEDIA_MATCHER_IDS",
    "YTDLP_SINGLE_MEDIA_PROVIDERS",
    "classify_url",
    "is_supported_ytdlp_media_url",
    "register_matcher",
    "registered_matchers",
    "unregister_matcher",
]
