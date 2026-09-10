"""UrlMatcher dataclass and the normalized-parts matching engine.

The engine parses a URL once and matches predicates against the PARSED
components (host, decoded path, query dict), never the raw string.
Plugin-supplied ``path_regex`` runs under the sandbox described below.

REGEX-SANDBOX RULE: combine bounded input with a wall-clock timeout. The
``regex`` module is already a project
dependency (pyproject: ``regex>=2026.5.9``) and exposes a native wall-clock
``timeout=`` on ``match``. So a plugin ``path_regex`` is: (1) length-capped and
complexity-capped at registration; (2) force-anchored ``\\A(?:...)\\Z``;
(3) applied ONLY to the already-domain-matched path+query, hard-capped at
``MAX_MATCH_INPUT`` chars; (4) matched under ``REGEX_TIMEOUT_SECONDS``. Belt
(bounded input makes catastrophic backtracking non-fatal) AND suspenders
(wall-clock timeout). No RE2 binding needed; revisit only if plugin regex
adoption outgrows this. First-party matchers may use richer patterns because
their regexes are code-reviewed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping
from urllib.parse import parse_qs, unquote, urlencode

import regex

from frisket.features.url_classification.psl import registered_domain
from frisket.ops.http_urls import parse_http_url

# First-party matchers reserve priority >= this ceiling; a plugin matcher
# declaring priority >= ceiling is rejected at registration.
PLUGIN_PRIORITY_CEILING = 1000

# Regex sandbox caps.
MAX_REGEX_PATTERN_LENGTH = 512
MAX_MATCH_INPUT = 2048
REGEX_TIMEOUT_SECONDS = 0.1
# Reject obviously nested-quantifier patterns from untrusted plugins at
# registration time (cheap structural heuristic, complements the runtime caps).
MAX_REGEX_QUANTIFIERS = 16

# Tracking / share params stripped during normalization so they cannot affect
# routing.
_TRACKING_QUERY_KEYS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
        "igshid",
        "si",
        "feature",
    }
)


class MatcherRegistrationError(ValueError):
    """Raised when a matcher fails registration validation (§1.4, §1.5)."""


@dataclass(frozen=True)
class UrlMatcher:
    matcher_id: str
    provider: str
    registered_domains: tuple[str, ...]
    kind: Literal["media", "collection", "scraper", "page"]
    path_prefix: tuple[str, ...] = ()
    query_requires: tuple[str, ...] = ()
    query_forbids: tuple[str, ...] = ()
    path_regex: str | None = None
    handler_action_kind: str | None = None
    params_hints: Mapping[str, Any] = field(default_factory=dict)
    capabilities: tuple[str, ...] = ()
    expansion: Mapping[str, Any] | None = None
    priority: int = 0
    source: Literal["first_party", "plugin"] = "first_party"
    origin_plugin_id: str | None = None

    @property
    def is_universal(self) -> bool:
        """A matcher with no declared domains matches any http/https host.

        Reserved for the first-party ``page`` fallback; plugin matchers must
        declare domains (enforced in the plugin-matchers lane).
        """
        return not self.registered_domains

    def specificity(self) -> int:
        """Number of refinement-predicate CATEGORIES this matcher declares.

        More categories = more specific (§1.5). Counted by category, not atom,
        so a channel matcher listing four path prefixes does not out-specify a
        watch matcher using path + query.
        """
        return (
            (1 if self.path_prefix else 0)
            + (1 if self.query_requires else 0)
            + (1 if self.query_forbids else 0)
            + (1 if self.path_regex else 0)
        )


@dataclass(frozen=True)
class NormalizedUrl:
    scheme: str
    host: str
    registered_domain: str
    path: str
    path_segments: tuple[str, ...]
    query: dict[str, list[str]]
    normalized: str


def normalize_url(url: str) -> NormalizedUrl | None:
    """Parse and normalize a URL into matchable parts, or None if unmatchable.

    Rejects non-http(s) schemes; lowercases and strips credentials/port from
    the host; decodes path segments; drops tracking query params.
    """
    parts = parse_http_url(url)
    if parts is None:
        return None
    parsed, host = parts

    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    segments = tuple(unquote(seg) for seg in path.split("/") if seg)

    raw_query = parse_qs(parsed.query, keep_blank_values=True)
    query = {k: v for k, v in raw_query.items() if k not in _TRACKING_QUERY_KEYS}

    normalized = f"{parsed.scheme.lower()}://{host}{path}"
    if query:
        normalized += "?" + urlencode(sorted(query.items()), doseq=True)

    return NormalizedUrl(
        scheme=parsed.scheme.lower(),
        host=host,
        registered_domain=registered_domain(host),
        path=path,
        path_segments=segments,
        query=query,
        normalized=normalized,
    )


def host_matches_domain(host: str, domain: str) -> bool:
    """Label-boundary registered-domain suffix match.

    ``music.youtube.com`` matches ``youtube.com``; ``notyoutube.com`` does not.
    """
    host = host.lower()
    domain = domain.lower()
    return host == domain or host.endswith("." + domain)


def validate_regex_pattern(pattern: str, *, trusted: bool) -> None:
    """Validate a ``path_regex`` at registration time (§1.4).

    Untrusted (plugin) patterns face length + complexity caps; trusted
    (first-party, code-reviewed) patterns only need to compile.
    """
    if not trusted:
        if len(pattern) > MAX_REGEX_PATTERN_LENGTH:
            raise MatcherRegistrationError(
                f"path_regex exceeds {MAX_REGEX_PATTERN_LENGTH}-char cap "
                f"({len(pattern)} chars)"
            )
        quantifiers = sum(pattern.count(ch) for ch in "*+")
        if quantifiers > MAX_REGEX_QUANTIFIERS:
            raise MatcherRegistrationError(
                f"path_regex has too many quantifiers ({quantifiers} > "
                f"{MAX_REGEX_QUANTIFIERS})"
            )
    try:
        _compile_anchored(pattern)
    except regex.error as exc:  # noqa: F841 - re-raised as registration error
        raise MatcherRegistrationError(f"path_regex does not compile: {exc}") from exc


def _compile_anchored(pattern: str) -> "regex.Pattern[str]":
    return regex.compile(r"\A(?:" + pattern + r")\Z")


def _regex_matches(pattern: str, target: str) -> bool:
    """Anchored, bounded, timeout-guarded regex match (§1.4)."""
    compiled = _compile_anchored(pattern)
    bounded = target[:MAX_MATCH_INPUT]
    try:
        return compiled.match(bounded, timeout=REGEX_TIMEOUT_SECONDS) is not None
    except TimeoutError:
        # A pathological pattern that blows the wall-clock guard simply does
        # not match; it never hangs the host.
        return False


def matcher_matches(matcher: UrlMatcher, norm: NormalizedUrl) -> bool:
    """Does ``matcher``'s domain + refinement predicates accept ``norm``?

    ALL declared predicates must pass. Predicates run on normalized parts, not
    the raw URL string.
    """
    if not matcher.is_universal:
        if not any(
            host_matches_domain(norm.host, d) for d in matcher.registered_domains
        ):
            return False

    if matcher.path_prefix and not any(
        norm.path.startswith(prefix) for prefix in matcher.path_prefix
    ):
        return False

    if any(key not in norm.query for key in matcher.query_requires):
        return False

    if any(key in norm.query for key in matcher.query_forbids):
        return False

    if matcher.path_regex is not None:
        target = norm.path
        if norm.query:
            target += "?" + urlencode(sorted(norm.query.items()), doseq=True)
        if not _regex_matches(matcher.path_regex, target):
            return False

    return True
