"""Host-side registration of plugin-contributed URL matchers.

Bridges the validated ``PluginManifest.runtime.matchers`` surface
(``frisket.contracts.plugin``) into the shared in-process classification registry
(``frisket.url_classification.registry``). The trust posture:

- Matcher EVALUATION is host-side and cheap — pure string work on the normalized
  URL parts. No plugin code runs during classification.
- The declared ``handler_action_kind`` runs LATER, in the plugin subprocess under
  its declared capabilities. Contributing a matcher never elevates a plugin's
  execution trust.

Precedence is enforced by the registry: plugin matchers carry ``source="plugin"``
and are validated at registration to sit strictly below the first-party reserved
priority ceiling, so a plugin can never hijack a first-party domain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from frisket.features.url_classification.matchers import UrlMatcher
from frisket.features.url_classification.registry import (
    _validate_matcher,
    register_matcher,
    registered_matchers,
    unregister_matcher,
)

if TYPE_CHECKING:  # avoid a contracts<-url_classification import at module load
    from frisket.contracts.plugin import PluginManifest, PluginManifestMatcher


def _to_url_matcher(matcher: "PluginManifestMatcher", *, plugin_id: str) -> UrlMatcher:
    """Project one validated manifest matcher into a registry ``UrlMatcher``."""
    return UrlMatcher(
        matcher_id=matcher.matcher_id,
        provider=matcher.provider,
        registered_domains=tuple(matcher.domains),
        kind=matcher.kind,
        path_prefix=tuple(matcher.path_prefix),
        query_requires=tuple(matcher.query_requires),
        query_forbids=tuple(matcher.query_forbids),
        path_regex=matcher.path_regex,
        handler_action_kind=matcher.handler_action_kind,
        params_hints=dict(matcher.params_hints),
        expansion=dict(matcher.expansion) if matcher.expansion is not None else None,
        priority=matcher.priority,
        source="plugin",
        origin_plugin_id=plugin_id,
    )


def plugin_matchers_from_manifest(manifest: "PluginManifest") -> list[UrlMatcher]:
    """Project a manifest's declared matchers into plugin-sourced ``UrlMatcher``s."""
    plugin_id = manifest.id
    return [
        _to_url_matcher(matcher, plugin_id=plugin_id)
        for matcher in manifest.runtime.matchers
    ]


def register_plugin_matchers(
    manifest: "PluginManifest", *, replace: bool = False
) -> list[str]:
    """Register a plugin's matchers into the shared registry.

    Returns the registered matcher ids. ``register_matcher`` re-applies the
    trust caps (priority ceiling, regex sandbox) as defense in depth even though
    the manifest validators already enforced them. Idempotent under
    ``replace=True`` (re-activation re-registers the same manifest).
    """
    registered: list[str] = []
    for url_matcher in plugin_matchers_from_manifest(manifest):
        register_matcher(url_matcher, replace=replace)
        registered.append(url_matcher.matcher_id)
    return registered


def _validate_plugin_matchers(manifest: "PluginManifest") -> None:
    for matcher in plugin_matchers_from_manifest(manifest):
        _validate_matcher(matcher)


def unregister_plugin_matchers(plugin_id: str) -> list[str]:
    """Remove every registry matcher owned by ``plugin_id``. Returns removed ids."""
    removed = [
        matcher.matcher_id
        for matcher in registered_matchers()
        if matcher.source == "plugin" and matcher.origin_plugin_id == plugin_id
    ]
    for matcher_id in removed:
        unregister_matcher(matcher_id)
    return removed
