"""The one media egress policy: host classification, the server floor, and
the per-path enforcement it feeds.

Three tiers: public is fetchable, private/loopback is governed by
``allow_private_hosts``, link-local/instance-metadata is refused under every
policy. The server env (``FRISKET_MEDIA_PRIVATE_HOSTS``) sets the default a
project may override — unless it says ``deny-locked``.
"""

from __future__ import annotations

import json

import pytest

from frisket.ops.egress_policy import (
    MEDIA_PRIVATE_HOSTS_ENV,
    STRICT_POLICY,
    EgressRefused,
    MediaEgressPolicy,
    media_egress_policy,
    server_private_hosts_mode,
    url_is_safe,
)
from frisket.project_settings import PROJECT_SETTINGS_META_KEY

ALLOW_PRIVATE = MediaEgressPolicy(allow_private_hosts=True)


class _FakeProject:
    def __init__(self, stored: bool | None = None) -> None:
        self.meta: dict[str, str] = {}
        if stored is not None:
            self.meta[PROJECT_SETTINGS_META_KEY] = json.dumps(
                {"media_allow_private_hosts": stored}
            )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        return self.meta.get(key, default)


# ---------------------------------------------------------------------------
# Host classification
# ---------------------------------------------------------------------------


def test_public_hosts_pass_both_tiers(monkeypatch):
    import socket

    from frisket.ops import egress_policy

    # public-host resolution is stubbed: no live DNS in the offline suite
    monkeypatch.setattr(
        egress_policy.socket,
        "getaddrinfo",
        lambda host, *a, **k: [
            (socket.AF_INET, None, None, "", ("93.184.216.34", 443))
        ],
    )
    for policy in (STRICT_POLICY, ALLOW_PRIVATE):
        policy.check_url("https://example.com/a.mp3")  # does not raise


def test_public_ipv6_suffix_is_not_guessed_to_be_a_private_v4(monkeypatch):
    """GitHub Pages' ordinary global IPv6 ends in ::153 (bytes 0.0.1.83)."""
    import socket

    from frisket.ops import egress_policy

    monkeypatch.setattr(egress_policy, "_discovered_nat64_prefixes", lambda: ())
    monkeypatch.setattr(
        egress_policy.socket,
        "getaddrinfo",
        lambda host, *a, **k: [
            (socket.AF_INET6, None, None, "", ("2606:50c0:8000::153", 443, 0, 0))
        ],
    )
    assert url_is_safe("https://frisket-dev.github.io/example.xml") is True


def test_network_specific_nat64_prefix_is_discovered(monkeypatch):
    import ipaddress
    import socket

    from frisket.ops import egress_policy

    # RFC 7050's ipv4only.arpa address 192.0.0.170 embedded in a local /96.
    monkeypatch.setattr(
        egress_policy.socket,
        "getaddrinfo",
        lambda host, *a, **k: [
            (socket.AF_INET6, None, None, "", ("2607:7700:0:e:0:2:c000:aa", 0, 0, 0))
        ],
    )
    egress_policy._discovered_nat64_prefixes.cache_clear()
    try:
        assert egress_policy._discovered_nat64_prefixes() == (
            ipaddress.IPv6Network("2607:7700:0:e:0:2::/96"),
        )
    finally:
        egress_policy._discovered_nat64_prefixes.cache_clear()


@pytest.mark.parametrize(
    "url",
    [
        "http://[::ffff:10.0.0.5]/a",
        "http://[2002:0a00:0005::]/a",
        "http://[64:ff9b::a00:5]/a",
    ],
)
def test_standardized_ipv4_in_ipv6_private_targets_remain_blocked(url: str):
    with pytest.raises(EgressRefused, match="private/loopback"):
        STRICT_POLICY.check_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a",
        "http://localhost:8000/a",
        "http://10.0.0.5/a",
        "http://192.168.1.1/a",
    ],
)
def test_private_and_loopback_are_governed_by_the_flag(url: str):
    with pytest.raises(EgressRefused, match="private/loopback"):
        STRICT_POLICY.check_url(url)
    ALLOW_PRIVATE.check_url(url)  # the flag's whole scope


@pytest.mark.parametrize(
    "url",
    [
        "http://100.64.0.1/a",
        "http://100.127.255.254/a",
        "http://[::ffff:100.64.0.1]/a",
    ],
)
def test_shared_cgnat_space_is_internal_under_the_strict_tier(url: str):
    """RFC 6598 is non-global even though ``ipaddress`` does not call it private."""

    with pytest.raises(EgressRefused, match="private/loopback"):
        STRICT_POLICY.check_url(url)
    # The media policy's explicit private-host opt-in keeps its existing broad
    # LAN/overlay reach.  Only the strict server-fetch tier changes here.
    ALLOW_PRIVATE.check_url(url)


def test_public_address_adjacent_to_cgnat_range_remains_allowed():
    # The RFC 6598 range ends at 100.127.255.255; do not widen the block into
    # the adjacent globally routable /9.
    STRICT_POLICY.check_url("http://100.128.0.1/a")


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.10.10/",
        "http://metadata.google.internal/computeMetadata/",
        "http://metadata/",
    ],
)
def test_link_local_and_metadata_are_refused_under_every_policy(url: str):
    for policy in (STRICT_POLICY, ALLOW_PRIVATE):
        with pytest.raises(EgressRefused, match="blocked URL"):
            policy.check_url(url)


def test_non_http_schemes_are_refused():
    assert url_is_safe("file:///etc/passwd") is False
    assert url_is_safe("ftp://example.com/a") is False
    with pytest.raises(EgressRefused):
        ALLOW_PRIVATE.check_url("file:///etc/passwd")


def test_hop_validator_applies_the_same_classification():
    check = ALLOW_PRIVATE.hop_validator()
    check("http://127.0.0.1:9/a")  # private allowed under this policy
    with pytest.raises(EgressRefused, match="blocked redirect target"):
        check("http://169.254.169.254/latest/meta-data/")


# ---------------------------------------------------------------------------
# Server floor + project override resolution
# ---------------------------------------------------------------------------


def test_unset_env_means_allow_and_project_default_on(monkeypatch):
    monkeypatch.delenv(MEDIA_PRIVATE_HOSTS_ENV, raising=False)
    assert server_private_hosts_mode() == "allow"
    assert media_egress_policy(_FakeProject()).allow_private_hosts is True


def test_a_typo_in_the_env_value_fails_loudly(monkeypatch):
    # Reading a typo as "allow" would void the operator's floor; reading it
    # as a lock would hide a toggle nobody locked.
    monkeypatch.setenv(MEDIA_PRIVATE_HOSTS_ENV, "deny_locked")
    with pytest.raises(ValueError, match=MEDIA_PRIVATE_HOSTS_ENV):
        server_private_hosts_mode()


@pytest.mark.parametrize(
    ("mode", "stored", "expected"),
    [
        ("allow", None, True),
        ("allow", False, False),
        ("deny", None, False),
        ("deny", True, True),  # deny is a default, not a lock
        ("deny-locked", None, False),
        ("deny-locked", True, False),  # the lock beats the stored override
    ],
)
def test_lock_precedence(monkeypatch, mode: str, stored: bool | None, expected: bool):
    monkeypatch.setenv(MEDIA_PRIVATE_HOSTS_ENV, mode)
    assert media_egress_policy(_FakeProject(stored)).allow_private_hosts is expected


def test_deny_locked_still_only_governs_private_reach(monkeypatch):
    # The lock is about RFC1918/loopback; it neither loosens nor is needed
    # for link-local/metadata, which no level can allow.
    monkeypatch.setenv(MEDIA_PRIVATE_HOSTS_ENV, "deny-locked")
    with pytest.raises(EgressRefused):
        media_egress_policy(_FakeProject(True)).check_url(
            "http://169.254.169.254/latest/meta-data/"
        )


# ---------------------------------------------------------------------------
# Per-path enforcement
# ---------------------------------------------------------------------------


def test_direct_download_refuses_a_redirect_to_a_refused_host():
    """A redirect-following direct fetch is re-checked at every hop: the
    entry URL passes (private allowed), the metadata redirect target must
    not be dialed."""
    import http.server

    from frisket.ops import enclosures
    from tests.deterministic_time import controlled_time

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — http.server API
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()

        def log_message(self, *args):  # noqa: A002
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    with controlled_time() as t:
        t.background(server.serve_forever)
        try:
            url = f"http://127.0.0.1:{server.server_port}/a.mp3"
            _data, _mime, _filename, err = enclosures.download_url(
                url, policy=ALLOW_PRIVATE
            )
            assert err is not None and "blocked redirect target" in err
            assert "169.254.169.254" in err
        finally:
            server.shutdown()


def test_ytdlp_entry_check_refuses_what_the_policy_refuses():
    """The yt-dlp path enforces the policy on the entry URL (its only
    enforcement point — the subprocess follows redirects itself)."""
    from frisket.ops.ytdlp import download_media

    with pytest.raises(EgressRefused, match="link-local/metadata"):
        download_media(
            "http://169.254.169.254/video",
            extractor=lambda *_a, **_k: {},
            policy=ALLOW_PRIVATE,
        )
    with pytest.raises(EgressRefused, match="private/loopback"):
        download_media(
            "http://127.0.0.1:8080/video",
            extractor=lambda *_a, **_k: {},
            policy=STRICT_POLICY,
        )
