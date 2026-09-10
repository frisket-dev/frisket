"""url-classification-plugin-matchers-v1: plugin-contributed URL matchers.

Deterministic and offline. Covers the PRE-REQ DEBT
PSL vendoring (the minimal core snapshot upgraded to the full vendored Public
Suffix List because this surface accepts UNTRUSTED plugin domains), the ``matchers``
manifest surface + install-time validators (trust caps: priority ceiling below
the first-party reserve, anchored/bounded plugin regex, registered-domain
requirement, handler binding), and host-side registration of plugin matchers
into the shared classification registry.
"""

from __future__ import annotations

import pytest

from frisket.contracts.plugin import PluginManifest, PluginManifestLoadError
from frisket.features.url_classification import (
    classify_url,
    registered_matchers,
    unregister_matcher,
)
from frisket.features.url_classification.matchers import (
    MAX_REGEX_PATTERN_LENGTH,
    PLUGIN_PRIORITY_CEILING,
    MatcherRegistrationError,
    UrlMatcher,
)
from frisket.features.url_classification.plugin_matchers import (
    plugin_matchers_from_manifest,
    register_plugin_matchers,
    unregister_plugin_matchers,
)
from frisket.features.url_classification.psl import (
    registered_domain,
)


# --- PSL vendoring (PRE-REQ DEBT) -----------------------------------------


def test_full_psl_resolves_icann_suffix_absent_from_core_minimal() -> None:
    # gov.br is an ICANN suffix the minimal core snapshot did NOT enumerate
    # (it carried only com.br). The full vendored list must collapse correctly.
    assert registered_domain("dados.gov.br") == "dados.gov.br"
    assert registered_domain("portal.dados.gov.br") == "dados.gov.br"


def test_full_psl_honours_wildcard_and_exception_rules() -> None:
    # *.ck is a wildcard public suffix: a.b.ck's registered domain is a.b.ck.
    assert registered_domain("a.b.ck") == "a.b.ck"
    assert registered_domain("deep.a.b.ck") == "a.b.ck"
    # !www.ck is an exception: www.ck is itself registrable.
    assert registered_domain("www.ck") == "www.ck"


def test_full_psl_covers_private_section_suffix() -> None:
    # githubusercontent.com lives in the PRIVATE section; collapsing it to
    # github's registrable domain would defeat the registered-domain guard.
    assert registered_domain("raw.githubusercontent.com") == "raw.githubusercontent.com"


# --- manifest matcher surface ---------------------------------------------


def _x_manifest_dict(**overrides: object) -> dict:
    base = {
        "schema_version": "frisket.plugin.v1",
        "id": "frisket.x",
        "version": "0.1.0",
        "contributes": {
            "actions": ["x.scrape_post"],
            "matchers": ["x.post", "x.profile"],
        },
        "requires": {"capabilities": ["plugin:trusted_local_backend"]},
        # No ``runtime.actions`` binding: a matcher's handler_action_kind is
        # validated against ``contributes.actions``, never against a runtime
        # binding, so this suite's subject needs the contribution only.
        "runtime": {
            "matchers": [
                {
                    "matcher_id": "x.post",
                    "provider": "x",
                    "domains": ["x.com", "twitter.com"],
                    "kind": "scraper",
                    "path_regex": r"/[A-Za-z0-9_]{1,15}/status/\d+",
                    "handler_action_kind": "x.scrape_post",
                    "params_hints": {"include_replies": False},
                },
                {
                    "matcher_id": "x.profile",
                    "provider": "x",
                    "domains": ["x.com", "twitter.com"],
                    "kind": "collection",
                    "path_regex": r"/(?!home|explore|search)[A-Za-z0-9_]{1,15}",
                    "handler_action_kind": "derive.collection_expand",
                    "expansion": {
                        "unit": "post",
                        "enumerator": "x.list_profile_posts",
                        "default_cap": 100,
                        "hard_cap": 1000,
                    },
                },
            ],
        },
    }
    base.update(overrides)
    return base


def test_manifest_with_matchers_validates() -> None:
    manifest = PluginManifest.model_validate(_x_manifest_dict())
    ids = [m.matcher_id for m in manifest.runtime.matchers]
    assert ids == ["x.post", "x.profile"]
    assert manifest.contributes.matchers == ["x.post", "x.profile"]


def test_legacy_core_matcher_handler_kind_fails_closed() -> None:
    """The pre-rename handler kind is retired vocabulary: a manifest naming
    it is refused at validation (it is neither a contributed action nor a
    permitted core kind), never silently rewritten."""
    data = _x_manifest_dict()
    data["runtime"]["matchers"][0]["handler_action_kind"] = "media.youtube_download"

    with pytest.raises(ValueError, match="media.youtube_download"):
        PluginManifest.model_validate(data)


def test_runtime_matcher_id_must_be_declared_in_contributes() -> None:
    data = _x_manifest_dict()
    data["contributes"]["matchers"] = ["x.post"]  # x.profile omitted
    with pytest.raises(ValueError):
        PluginManifest.model_validate(data)


def test_matcher_domains_are_required() -> None:
    data = _x_manifest_dict()
    data["runtime"]["matchers"][0]["domains"] = []
    with pytest.raises(ValueError):
        PluginManifest.model_validate(data)


def test_plugin_matcher_cannot_claim_first_party_priority() -> None:
    data = _x_manifest_dict()
    data["runtime"]["matchers"][0]["priority"] = PLUGIN_PRIORITY_CEILING
    with pytest.raises(ValueError):
        PluginManifest.model_validate(data)


def test_plugin_matcher_cannot_shadow_first_party_matcher_id() -> None:
    data = _x_manifest_dict()
    data["contributes"]["matchers"] = ["firstparty.x.post", "x.profile"]
    data["runtime"]["matchers"][0]["matcher_id"] = "firstparty.x.post"
    with pytest.raises(ValueError):
        PluginManifest.model_validate(data)


def test_matcher_handler_action_must_be_contributed_or_core() -> None:
    data = _x_manifest_dict()
    data["runtime"]["matchers"][0]["handler_action_kind"] = "x.not_contributed"
    with pytest.raises(ValueError):
        PluginManifest.model_validate(data)


def test_oversized_plugin_regex_fails_manifest_load() -> None:
    data = _x_manifest_dict()
    data["runtime"]["matchers"][0]["path_regex"] = "a" * (MAX_REGEX_PATTERN_LENGTH + 1)
    with pytest.raises(ValueError):
        PluginManifest.model_validate(data)


def test_collection_matcher_requires_expansion() -> None:
    data = _x_manifest_dict()
    del data["runtime"]["matchers"][1]["expansion"]
    with pytest.raises(ValueError):
        PluginManifest.model_validate(data)


def test_load_error_code_for_bad_matcher() -> None:
    import json
    from pathlib import Path
    import tempfile

    from frisket.contracts.plugin import load_plugin_manifest_file

    data = _x_manifest_dict()
    data["runtime"]["matchers"][0]["domains"] = []
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "plugin.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(PluginManifestLoadError) as excinfo:
            load_plugin_manifest_file(path)
    assert excinfo.value.code == "invalid_plugin_manifest"


# --- host-side registration into the shared registry ----------------------


@pytest.fixture
def cleanup_x_matchers():
    yield
    unregister_plugin_matchers("frisket.x")


def test_plugin_matchers_from_manifest_are_plugin_sourced() -> None:
    manifest = PluginManifest.model_validate(_x_manifest_dict())
    matchers = plugin_matchers_from_manifest(manifest)
    assert {m.matcher_id for m in matchers} == {"x.post", "x.profile"}
    assert all(m.source == "plugin" for m in matchers)
    assert all(m.origin_plugin_id == "frisket.x" for m in matchers)
    # priority stays strictly below the first-party reserved ceiling
    assert all(m.priority < PLUGIN_PRIORITY_CEILING for m in matchers)


def test_registered_plugin_matcher_classifies_its_domain(cleanup_x_matchers) -> None:
    manifest = PluginManifest.model_validate(_x_manifest_dict())
    registered = register_plugin_matchers(manifest)
    assert set(registered) == {"x.post", "x.profile"}

    post = classify_url(
        "https://x.com/nasa/status/123", enabled_plugin_ids={"frisket.x"}
    )
    assert post is not None
    assert post.kind == "scraper"
    assert post.provider == "x"
    assert post.source == "plugin"
    assert post.origin_plugin_id == "frisket.x"
    assert post.handler.action_kind == "x.scrape_post"

    profile = classify_url("https://twitter.com/nasa", enabled_plugin_ids={"frisket.x"})
    assert profile is not None
    assert profile.kind == "collection"
    assert profile.handler.action_kind == "derive.collection_expand"
    assert profile.expansion is not None
    assert profile.expansion.enumerator == "x.list_profile_posts"


def test_first_party_still_wins_over_plugin_on_shared_domain(
    cleanup_x_matchers,
) -> None:
    # A malicious plugin that lists youtube.com cannot hijack the first-party
    # youtube routing through trust-tier precedence.
    data = _x_manifest_dict()
    data["runtime"]["matchers"].append(
        {
            "matcher_id": "x.hijack_youtube",
            "provider": "x",
            "domains": ["youtube.com"],
            "kind": "scraper",
            "path_prefix": ["/watch"],
            "handler_action_kind": "x.scrape_post",
        }
    )
    data["contributes"]["matchers"] = ["x.post", "x.profile", "x.hijack_youtube"]
    manifest = PluginManifest.model_validate(data)
    register_plugin_matchers(manifest)
    result = classify_url("https://www.youtube.com/watch?v=abc123")
    assert result is not None
    assert result.source == "first_party"
    assert result.provider == "youtube"


def test_register_is_idempotent_and_unregister_removes(cleanup_x_matchers) -> None:
    manifest = PluginManifest.model_validate(_x_manifest_dict())
    register_plugin_matchers(manifest)
    register_plugin_matchers(manifest, replace=True)  # idempotent re-register
    before = {m.matcher_id for m in registered_matchers()}
    assert {"x.post", "x.profile"} <= before
    unregister_plugin_matchers("frisket.x")
    after = {m.matcher_id for m in registered_matchers()}
    assert "x.post" not in after
    assert "x.profile" not in after
    # a fresh classify no longer routes x.com to the plugin
    assert classify_url("https://x.com/nasa/status/123").matcher_id == "firstparty.page"


def test_direct_registration_rejects_priority_ceiling() -> None:
    # Defense in depth: the registry itself refuses a plugin matcher at/above
    # the ceiling even if a manifest validator were bypassed.
    bad = UrlMatcher(
        matcher_id="plugin.raw.ceiling",
        provider="x",
        registered_domains=("rawceiling.test",),
        kind="scraper",
        handler_action_kind="x.scrape",
        priority=PLUGIN_PRIORITY_CEILING,
        source="plugin",
        origin_plugin_id="x.plugin",
    )
    with pytest.raises(MatcherRegistrationError):
        from frisket.features.url_classification import register_matcher

        register_matcher(bad)
    unregister_matcher("plugin.raw.ceiling")
