"""The in-process URL matcher registry and ``classify_url``.

Mirrors the source-poller registry shape (``frisket.sources.runtime``:
``register_source_poller`` / ``registered_source_kinds``) so the idiom is
familiar and the plugin-registration path has a precedent.
"""

from __future__ import annotations

from collections.abc import Collection

from frisket.features.url_classification.contract import (
    ClassificationHandler,
    ExpansionHint,
    UrlClassification,
)
from frisket.features.url_classification.matchers import (
    PLUGIN_PRIORITY_CEILING,
    MatcherRegistrationError,
    UrlMatcher,
    matcher_matches,
    normalize_url,
    validate_regex_pattern,
)

# Registration order is preserved (dict insertion order) as the final
# deterministic precedence tiebreak.
_MATCHERS: dict[str, UrlMatcher] = {}


def _validate_matcher(matcher: UrlMatcher) -> None:
    matcher_id = str(matcher.matcher_id).strip()
    if not matcher_id:
        raise MatcherRegistrationError("matcher_id must be non-empty")
    is_first_party = matcher.source == "first_party"
    if not is_first_party and matcher.priority >= PLUGIN_PRIORITY_CEILING:
        raise MatcherRegistrationError(
            f"plugin matcher priority must be < {PLUGIN_PRIORITY_CEILING} "
            f"(first-party reserved): {matcher_id}"
        )

    if matcher.path_regex is not None:
        validate_regex_pattern(matcher.path_regex, trusted=is_first_party)


def register_matcher(matcher: UrlMatcher, *, replace: bool = False) -> None:
    """Register a matcher after validating the trust caps (§1.4, §1.5)."""
    _validate_matcher(matcher)
    matcher_id = str(matcher.matcher_id).strip()
    existing = _MATCHERS.get(matcher_id)
    if existing is not None and existing is not matcher and not replace:
        raise MatcherRegistrationError(f"matcher already registered: {matcher_id}")
    _MATCHERS[matcher_id] = matcher


def unregister_matcher(matcher_id: str) -> None:
    _MATCHERS.pop(matcher_id, None)


def _restore_matcher(
    matcher_id: str,
    previous: UrlMatcher | None,
    *,
    expected_plugin_id: str,
) -> None:
    current = _MATCHERS.get(matcher_id)
    if current is previous:
        return
    if current is None or current.origin_plugin_id != expected_plugin_id:
        raise RuntimeError(
            f"URL matcher changed while restoring activation: {matcher_id}"
        )
    if previous is None:
        _MATCHERS.pop(matcher_id, None)
    else:
        _MATCHERS[matcher_id] = previous


def registered_matchers() -> tuple[UrlMatcher, ...]:
    return tuple(_MATCHERS.values())


def _precedence_key(matcher: UrlMatcher, order: int) -> tuple:
    # Higher is better for the first three; lower registration order breaks
    # the final tie. Negate ``order`` so the tuple sorts "best first" under a
    # plain descending max().
    return (
        1 if matcher.source == "first_party" else 0,
        matcher.specificity(),
        matcher.priority,
        -order,
    )


def classify_url(
    url: str, *, enabled_plugin_ids: Collection[str] = ()
) -> UrlClassification | None:
    """Classify a URL against the registry, or None if unmatchable.

    Domain-specific matchers are resolved first by the precedence key (trust
    tier, specificity, priority, registration order). The universal ``page``
    fallback is deliberately last-resort: it only wins when no domain-specific
    matcher accepted the URL, so a plugin scraper still routes its own domain
    over the fallback.
    """
    norm = normalize_url(url)
    if norm is None:
        return None

    specific: list[tuple[UrlMatcher, int]] = []
    universal: list[tuple[UrlMatcher, int]] = []
    for index, matcher in enumerate(_MATCHERS.values()):
        if (
            matcher.source != "first_party"
            and matcher.origin_plugin_id not in enabled_plugin_ids
        ):
            continue
        if not matcher_matches(matcher, norm):
            continue
        (universal if matcher.is_universal else specific).append((matcher, index))

    pool = specific or universal
    if not pool:
        return None

    winner, _ = max(pool, key=lambda pair: _precedence_key(pair[0], pair[1]))
    return _to_classification(winner)


def _to_classification(matcher: UrlMatcher) -> UrlClassification:
    handler = ClassificationHandler(
        action_kind=matcher.handler_action_kind or "media.fetch_url",
        params_hints=dict(matcher.params_hints),
    )
    expansion = (
        ExpansionHint(**dict(matcher.expansion))
        if matcher.expansion is not None
        else None
    )
    return UrlClassification(
        kind=matcher.kind,
        provider=matcher.provider,
        matcher_id=matcher.matcher_id,
        source=matcher.source,
        origin_plugin_id=matcher.origin_plugin_id,
        capabilities=list(matcher.capabilities),
        handler=handler,
        expansion=expansion,
    )
