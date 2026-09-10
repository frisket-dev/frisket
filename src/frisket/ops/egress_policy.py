"""One egress policy for media fetching.

Host classification for server-side fetches, plus the ``MediaEgressPolicy``
the media download paths (direct fetch, RSS enclosures, yt-dlp) consume.
Three tiers: public (allowed), private/loopback (governed by
``allow_private_hosts`` — "a media host on your own network"), and
link-local/instance-metadata (refused at every level: nothing legitimate is
served from them, so no caller can opt in).

Enforcement scope, stated exactly: on its own this is an app-level filter on
what dispatch accepts, not a network guarantee. A caller that only asks "is
this host safe?" and then hands the *name* to a client that re-resolves it at
connect time leaves a DNS-rebinding gap — the address vetted here need not be
the address dialed. Callers that must be authoritative therefore resolve once
through ``safe_pinned_addresses`` and pin the connection to a vetted literal;
``netguard.safe_request`` does this on every hop, so the API-call guard is not
rebinding-bypassable. The media paths keep the weaker entry-filter contract:
the direct/RSS fetchers re-check each redirect hop but do not pin, and the
yt-dlp path is checked on entry only — its subprocess follows redirects and
re-resolves DNS itself (see ``ytdlp.download_media``). For those paths the hard
egress boundary remains container-level network control owned by the operator.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable
from urllib.parse import urlparse

BLOCKED_HOSTS = {"metadata.google.internal", "metadata", "localhost"}
# Names that resolve to instance-metadata services. Blocked at every tier:
# nothing legitimate is served from them, so no caller has reason to opt in.
METADATA_HOSTS = {"metadata.google.internal", "metadata"}


class EgressRefused(ValueError):
    """A fetch target the active egress policy refuses."""


def _resolve_safe_addresses(
    host: str, *, allow_private: bool = False
) -> list[str] | None:
    """Every address ``host`` resolves to, or ``None`` if any is refused.

    Resolves once and vets *all* returned addresses under the active tier.
    Returned strings are the exact resolved literals a caller pins its socket
    to, so the address vetted here is the address dialed — this closes the
    resolve/connect gap an httpx client would otherwise reopen by re-resolving
    the hostname itself at connect time. ``None`` means refuse (name in the
    blocklist, unresolvable, or any resolved address blocked); a non-empty list
    means every answer passed.
    """
    blocked = METADATA_HOSTS if allow_private else BLOCKED_HOSTS
    if host.lower() in blocked:
        return None
    rejected = _ip_is_metadata if allow_private else _ip_is_blocked
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return None
    addresses: list[str] = []
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        # NAT64/DNS64 networks can synthesize a public-looking v6 that embeds
        # a private/loopback v4. Checking is_private on the v6 alone misses it,
        # so unwrap standardized and locally-discovered translation prefixes.
        if isinstance(ip, ipaddress.IPv6Address):
            embedded = _embedded_v4(ip)
            if embedded is not None and rejected(embedded):
                return None
        if rejected(ip):
            return None
        addresses.append(str(ip))
    return addresses or None


def _host_is_safe(host: str, *, allow_private: bool = False) -> bool:
    return _resolve_safe_addresses(host, allow_private=allow_private) is not None


_RFC6052_PREFIX_LENGTHS = (32, 40, 48, 56, 64, 96)
_IPV4ONLY_DISCOVERY_ADDRESSES = {
    ipaddress.IPv4Address("192.0.0.170"),
    ipaddress.IPv4Address("192.0.0.171"),
}
_STANDARD_TRANSLATION_PREFIXES = (
    ipaddress.IPv6Network("64:ff9b::/96"),
    ipaddress.IPv6Network("64:ff9b:1::/48"),
)


def _rfc6052_v4(
    ip: ipaddress.IPv6Address, prefix_length: int
) -> ipaddress.IPv4Address | None:
    """Extract RFC 6052's 32 IPv4 bits for one known translation prefix."""
    packed = ip.packed
    if prefix_length == 96:
        raw = packed[12:16]
    else:
        # For /32 through /64, byte 8 is RFC 6052's reserved ``u`` octet.
        if prefix_length not in _RFC6052_PREFIX_LENGTHS or packed[8] != 0:
            return None
        prefix_bytes = prefix_length // 8
        before_u = 8 - prefix_bytes
        raw = packed[prefix_bytes:8] + packed[9 : 9 + (4 - before_u)]
    return ipaddress.IPv4Address(raw) if len(raw) == 4 else None


@lru_cache(maxsize=1)
def _discovered_nat64_prefixes() -> tuple[ipaddress.IPv6Network, ...]:
    """Discover this network's RFC 6052 prefix via RFC 7050's reserved name."""
    try:
        infos = socket.getaddrinfo("ipv4only.arpa", None, socket.AF_INET6)
    except OSError:
        return ()
    prefixes: set[ipaddress.IPv6Network] = set()
    for *_, sockaddr in infos:
        try:
            candidate = ipaddress.IPv6Address(sockaddr[0])
        except ValueError:
            continue
        for prefix_length in _RFC6052_PREFIX_LENGTHS:
            if _rfc6052_v4(candidate, prefix_length) in _IPV4ONLY_DISCOVERY_ADDRESSES:
                prefixes.add(
                    ipaddress.IPv6Network((candidate, prefix_length), strict=False)
                )
    return tuple(
        sorted(
            prefixes,
            key=lambda network: (network.prefixlen, int(network.network_address)),
        )
    )


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    """The v4 a v6 wraps (v4-mapped, 6to4, or recognized NAT64) or None."""
    for cand in (ip.ipv4_mapped, getattr(ip, "sixtofour", None)):
        if cand is not None:
            return cand
    for prefix in _STANDARD_TRANSLATION_PREFIXES:
        if ip in prefix:
            return _rfc6052_v4(ip, prefix.prefixlen)
    for prefix in _discovered_nat64_prefixes():
        if ip in prefix:
            return _rfc6052_v4(ip, prefix.prefixlen)
    return None


def _ip_is_blocked(ip) -> bool:
    return (
        # ``is_private`` and ``is_global`` are deliberately not complements.
        # In particular Python classifies RFC 6598 shared/CGNAT space
        # (100.64.0.0/10) as neither.  Strict server-side fetches must reject
        # every non-global address, including IPv4-mapped v6 forms, rather than
        # treating that common internal/overlay range as public Internet.
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or str(ip) == "169.254.169.254"
    )


def _ip_is_metadata(ip) -> bool:
    """Link-local and instance-metadata addresses only.

    The narrow tier: these host nothing a caller could legitimately want, so
    they stay blocked even where reaching a LAN or loopback service is allowed.
    """
    return ip.is_link_local or str(ip) == "169.254.169.254"


def url_is_safe(url: str, *, allow_private: bool = False) -> bool:
    """Whether ``url`` may be fetched server-side.

    ``allow_private`` keeps loopback and RFC1918 targets reachable — for
    callers whose URLs are operator-supplied and may legitimately name a LAN
    service — while still refusing link-local and instance-metadata addresses.
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    return _host_is_safe(p.hostname, allow_private=allow_private)


def safe_pinned_addresses(url: str, *, allow_private: bool = False) -> list[str] | None:
    """The vetted IP literals ``url``'s host resolves to, for connection pinning.

    ``None`` means the URL is refused (non-HTTP scheme, no host, unresolvable,
    or any resolved address blocked under the active tier). A non-empty list
    means every resolved address passed; a caller may dial any one of them and
    reach a vetted host. Pinning to a returned literal — rather than re-issuing
    the request by hostname — is what makes the guard authoritative against DNS
    rebinding, since httpx re-resolves the name independently at connect time.
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    return _resolve_safe_addresses(p.hostname, allow_private=allow_private)


@dataclass(frozen=True)
class MediaEgressPolicy:
    """What one media fetch may reach; build via ``media_egress_policy``."""

    allow_private_hosts: bool

    def check_url(self, url: str) -> None:
        """Raise ``EgressRefused`` unless ``url`` is fetchable under this policy."""
        if not url_is_safe(url, allow_private=self.allow_private_hosts):
            raise EgressRefused(f"blocked URL ({self._refused_scope()}): {url}")

    def hop_validator(self) -> Callable[[str], None]:
        """Per-redirect-hop re-check for fetchers that follow redirects."""

        def _check_hop(url: str) -> None:
            if not url_is_safe(url, allow_private=self.allow_private_hosts):
                raise EgressRefused(
                    f"blocked redirect target ({self._refused_scope()}): {url}"
                )

        return _check_hop

    def _refused_scope(self) -> str:
        # The refusal message states only what this policy actually refuses.
        if self.allow_private_hosts:
            return "link-local/metadata"
        return "private/loopback/metadata"


STRICT_POLICY = MediaEgressPolicy(allow_private_hosts=False)

# Server-level default for whether media fetches may reach private/loopback
# hosts; a project may override it unless the value is deny-locked. Local
# no-org installs that leave this unset keep the project-level default (on).
MEDIA_PRIVATE_HOSTS_ENV = "FRISKET_MEDIA_PRIVATE_HOSTS"
_SERVER_MODES = ("allow", "deny", "deny-locked")


def server_private_hosts_mode() -> str:
    """``allow`` (default), ``deny``, or ``deny-locked`` from the server env.

    An unrecognized value raises instead of guessing: silently reading a typo
    as ``allow`` would void the operator's floor, and reading it as a lock
    would hide a toggle the operator never locked.
    """
    raw = (os.environ.get(MEDIA_PRIVATE_HOSTS_ENV) or "").strip().lower()
    if not raw:
        return "allow"
    if raw not in _SERVER_MODES:
        raise ValueError(
            f"{MEDIA_PRIVATE_HOSTS_ENV} must be one of {', '.join(_SERVER_MODES)};"
            f" got {raw!r}"
        )
    return raw


def private_hosts_locked() -> bool:
    """Whether the server forbids the project-level override outright."""
    return server_private_hosts_mode() == "deny-locked"


def server_default_policy() -> MediaEgressPolicy:
    """For call sites with no project in scope: the server default alone."""
    return MediaEgressPolicy(allow_private_hosts=server_private_hosts_mode() == "allow")


def media_egress_policy(project: Any) -> MediaEgressPolicy:
    """The effective policy for one project's media fetches.

    Server default first: ``deny-locked`` ignores the project setting
    entirely; otherwise an explicit project value wins and an unset one
    inherits the server default. Link-local/metadata stay refused at every
    level — the resolution here only decides private/loopback reach.
    """
    from frisket.project_settings import read_project_settings

    mode = server_private_hosts_mode()
    if mode == "deny-locked":
        return STRICT_POLICY
    stored = read_project_settings(project).media_allow_private_hosts
    if stored is None:
        return MediaEgressPolicy(allow_private_hosts=mode == "allow")
    return MediaEgressPolicy(allow_private_hosts=stored)
