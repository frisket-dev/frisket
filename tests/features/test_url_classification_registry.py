"""url-classification-registry-core-v1: registry engine, precedence, and the
ReDoS / regex-sandbox trust posture.

Deterministic and offline. Covers the normalized-parts
matching engine, first-party > plugin trust tier + specificity + priority
precedence, registration conflict handling, and the plugin-regex sandbox
(anchored patterns on bounded parts under a wall-clock timeout), including an
adversarial-input timing sanity test.
"""

from __future__ import annotations

import time

import pytest

from frisket.features.url_classification import (
    UrlMatcher,
    classify_url,
    register_matcher,
    registered_matchers,
    unregister_matcher,
)
from frisket.features.url_classification.matchers import (
    MatcherRegistrationError,
    PLUGIN_PRIORITY_CEILING,
    normalize_url,
)


@pytest.fixture
def cleanup_matchers():
    added: list[str] = []
    yield added
    for matcher_id in added:
        unregister_matcher(matcher_id)


# --- normalization --------------------------------------------------------


def test_normalize_strips_credentials_port_and_lowercases_host() -> None:
    norm = normalize_url("https://User:Pass@YouTube.COM:443/Watch?v=abc")
    assert norm is not None
    assert norm.host == "youtube.com"
    assert norm.registered_domain == "youtube.com"
    # path case is preserved; host case is not
    assert norm.path == "/Watch"


def test_normalize_rejects_non_http_scheme() -> None:
    assert normalize_url("ftp://youtube.com/watch?v=abc") is None
    assert normalize_url("mailto:a@b.com") is None


def test_normalize_drops_tracking_query_params() -> None:
    norm = normalize_url("https://youtu.be/abc123?si=track&utm_source=x&v=keep")
    assert norm is not None
    assert "si" not in norm.query
    assert "utm_source" not in norm.query
    assert "v" in norm.query


def test_registered_domain_collapses_subdomains_but_not_public_suffixes() -> None:
    from frisket.features.url_classification.psl import registered_domain

    assert registered_domain("music.youtube.com") == "youtube.com"
    assert registered_domain("www.m.youtube.com") == "youtube.com"
    # a public-suffix-hosted site is NOT collapsed to the suffix
    assert registered_domain("foo.github.io") == "foo.github.io"


# --- precedence -----------------------------------------------------------


def test_first_party_outranks_plugin_on_same_domain(cleanup_matchers) -> None:
    plugin = UrlMatcher(
        matcher_id="plugin.hijack.youtube",
        provider="evil",
        registered_domains=("youtube.com",),
        kind="scraper",
        path_prefix=("/watch",),
        query_requires=("v",),
        handler_action_kind="evil.scrape",
        priority=PLUGIN_PRIORITY_CEILING - 1,
        source="plugin",
        origin_plugin_id="evil.plugin",
    )
    register_matcher(plugin)
    cleanup_matchers.append(plugin.matcher_id)
    result = classify_url("https://www.youtube.com/watch?v=abc123")
    assert result is not None
    # first-party youtube video wins; the plugin cannot hijack youtube.com
    assert result.source == "first_party"
    assert result.provider == "youtube"


def test_more_specific_matcher_wins_within_same_tier(cleanup_matchers) -> None:
    # a domain-only page-like matcher vs a path-refined one on the same host
    broad = UrlMatcher(
        matcher_id="plugin.example.broad",
        provider="example",
        registered_domains=("exampleplugin.test",),
        kind="scraper",
        handler_action_kind="example.scrape",
        priority=10,
        source="plugin",
        origin_plugin_id="example.plugin",
    )
    specific = UrlMatcher(
        matcher_id="plugin.example.specific",
        provider="example",
        registered_domains=("exampleplugin.test",),
        kind="collection",
        path_prefix=("/user/",),
        handler_action_kind="derive.collection_expand",
        priority=10,
        source="plugin",
        origin_plugin_id="example.plugin",
    )
    register_matcher(broad)
    register_matcher(specific)
    cleanup_matchers.extend([broad.matcher_id, specific.matcher_id])
    result = classify_url(
        "https://exampleplugin.test/user/bob",
        enabled_plugin_ids={"example.plugin"},
    )
    assert result is not None
    assert result.matcher_id == "plugin.example.specific"


def test_priority_breaks_specificity_ties(cleanup_matchers) -> None:
    low = UrlMatcher(
        matcher_id="plugin.tie.low",
        provider="tie",
        registered_domains=("tieplugin.test",),
        kind="scraper",
        path_prefix=("/x",),
        handler_action_kind="tie.a",
        priority=5,
        source="plugin",
        origin_plugin_id="tie.plugin",
    )
    high = UrlMatcher(
        matcher_id="plugin.tie.high",
        provider="tie",
        registered_domains=("tieplugin.test",),
        kind="scraper",
        path_prefix=("/x",),
        handler_action_kind="tie.b",
        priority=50,
        source="plugin",
        origin_plugin_id="tie.plugin",
    )
    register_matcher(low)
    register_matcher(high)
    cleanup_matchers.extend([low.matcher_id, high.matcher_id])
    result = classify_url("https://tieplugin.test/x", enabled_plugin_ids={"tie.plugin"})
    assert result is not None
    assert result.matcher_id == "plugin.tie.high"


# --- registration validation ---------------------------------------------


def test_duplicate_matcher_id_is_rejected(cleanup_matchers) -> None:
    m = UrlMatcher(
        matcher_id="plugin.dup.one",
        provider="dup",
        registered_domains=("dupplugin.test",),
        kind="scraper",
        handler_action_kind="dup.scrape",
        source="plugin",
        origin_plugin_id="dup.plugin",
    )
    register_matcher(m)
    cleanup_matchers.append(m.matcher_id)
    # a DIFFERENT matcher reusing the same id is a conflict; re-registering
    # the identical object is idempotent, so use a distinct one here.
    conflicting = UrlMatcher(
        matcher_id="plugin.dup.one",
        provider="other",
        registered_domains=("otherplugin.test",),
        kind="scraper",
        handler_action_kind="other.scrape",
        source="plugin",
        origin_plugin_id="other.plugin",
    )
    with pytest.raises(MatcherRegistrationError):
        register_matcher(conflicting)


def test_plugin_cannot_claim_first_party_priority_ceiling() -> None:
    m = UrlMatcher(
        matcher_id="plugin.toohigh",
        provider="x",
        registered_domains=("toohigh.test",),
        kind="scraper",
        handler_action_kind="x.scrape",
        priority=PLUGIN_PRIORITY_CEILING,
        source="plugin",
        origin_plugin_id="x.plugin",
    )
    with pytest.raises(MatcherRegistrationError):
        register_matcher(m)


def test_first_party_matchers_are_registered_at_import() -> None:
    ids = {m.matcher_id for m in registered_matchers()}
    assert "firstparty.youtube.video.watch" in ids
    assert "firstparty.page" in ids


# --- regex sandbox / ReDoS posture ----------------------------------------


def test_plugin_regex_over_size_cap_is_rejected() -> None:
    huge = "a" * 5000
    m = UrlMatcher(
        matcher_id="plugin.hugeregex",
        provider="x",
        registered_domains=("hugeregex.test",),
        kind="scraper",
        path_regex=huge,
        handler_action_kind="x.scrape",
        source="plugin",
        origin_plugin_id="x.plugin",
    )
    with pytest.raises(MatcherRegistrationError):
        register_matcher(m)


def test_pathological_regex_stays_bounded_on_adversarial_input(
    cleanup_matchers,
) -> None:
    # A classic catastrophic-backtracking pattern against a long non-matching
    # input. The guard (anchor + bounded input + wall-clock timeout) must keep
    # evaluation fast and simply not match, rather than hang.
    m = UrlMatcher(
        matcher_id="plugin.redos",
        provider="x",
        registered_domains=("redos.test",),
        kind="scraper",
        path_regex=r"/(a+)+b",
        handler_action_kind="x.scrape",
        source="plugin",
        origin_plugin_id="x.plugin",
    )
    register_matcher(m)
    cleanup_matchers.append(m.matcher_id)
    adversarial = "https://redos.test/" + ("a" * 4000) + "!"
    start = time.perf_counter()
    result = classify_url(adversarial)
    elapsed = time.perf_counter() - start
    # the ReDoS matcher must NOT match; falls through to the page fallback
    assert result is not None
    assert result.matcher_id == "firstparty.page"
    assert elapsed < 1.0, f"classification took {elapsed:.3f}s (ReDoS not bounded)"


def test_plugin_regex_is_evaluated_anchored(cleanup_matchers) -> None:
    # /abc must match the whole path+query, not a substring of it
    m = UrlMatcher(
        matcher_id="plugin.anchored",
        provider="x",
        registered_domains=("anchored.test",),
        kind="scraper",
        path_regex=r"/abc",
        handler_action_kind="x.scrape",
        source="plugin",
        origin_plugin_id="x.plugin",
    )
    register_matcher(m)
    cleanup_matchers.append(m.matcher_id)
    # exact anchored match -> plugin matcher
    hit = classify_url("https://anchored.test/abc", enabled_plugin_ids={"x.plugin"})
    assert hit is not None and hit.matcher_id == "plugin.anchored"
    # a longer path is NOT a match because the pattern is anchored end-to-end
    miss = classify_url("https://anchored.test/abcdef", enabled_plugin_ids={"x.plugin"})
    assert miss is not None and miss.matcher_id == "firstparty.page"
