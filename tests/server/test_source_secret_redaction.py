"""Source-poll failure text goes through frisket.redaction, never weaker.

Adversary: source_runs rows, receipts and the source health API surface
provider error messages and warnings to anyone who can read the project, and
the same text reaches server logs. The regexes below are the per-poller
redactors the canonical module replaced; they stay here as parity oracles:
any corpus input whose secret an oracle removed must lose it through the
canonical path too.
"""

from __future__ import annotations

import itertools
import re
from datetime import UTC, datetime

import pytest

from frisket.redaction import redact_text
from frisket.server.sources.api_list_dicts import ApiListDictsFeedChecker
from frisket.server.sources.courtlistener import (
    CourtListenerDocketListing,
    CourtListenerDocketFeedChecker,
)
from frisket.server.sources.runtime import SourcePollContext, provider_error_text
from frisket.server.sources.tiktok import TikTokProfileFeedChecker
from frisket.server.sources.youtube import (
    YOUTUBE_CHANNEL,
    YOUTUBE_PLAYLIST,
    YouTubeChannelListing,
    YouTubeCollectionFeedChecker,
)

SECRET = "S3cr3tV4lue9"
_KEY_WORDS = r"(api[_-]?key|authorization|cookie|token|password|secret)"
_ORACLE_SOURCE = re.compile(rf"(?i){_KEY_WORDS}(\s*[:=]\s*(?:bearer\s+)?)?[^\s,;&]+")
_ORACLE_MEDIA = re.compile(rf"(?i){_KEY_WORDS}(\s*[:=]\s*(?:bearer\s+)?)?[^\s,;]+")
_ORACLE_STRUCTURED = re.compile(
    r"(?i)((?:[\"']?(?:api[_-]?key|authorization|cookie|token|password|secret)"
    r"[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)"
    r"[^\"'\s,;&}]+)"
)
_ORACLE_URL_CREDENTIAL = re.compile(r"(?i)(https?://)([^/\s:@]+):([^@\s/]+)@")


def _oracle_courtlistener(text: str) -> str:
    def keep_key(value: str) -> str:
        for separator in (":", "="):
            if separator in value:
                return f"{value.split(separator, 1)[0]}{separator}<redacted>"
        return "<redacted>"

    text = _ORACLE_SOURCE.sub(r"\1=<redacted>", text)
    text = _ORACLE_STRUCTURED.sub(lambda match: keep_key(match.group(1)), text)
    return _ORACLE_URL_CREDENTIAL.sub(r"\1<redacted>@", text)


ORACLES = {
    "api_list_dicts": lambda text: _ORACLE_SOURCE.sub(r"\1=<redacted>", text),
    "youtube_tiktok": lambda text: _ORACLE_MEDIA.sub(r"\1=<redacted>", text),
    "courtlistener": _oracle_courtlistener,
}

_KEYS = (
    "api_key",
    "api-key",
    "apikey",
    "API_KEY",
    "apiKey",
    "x-api-key",
    "authorization",
    "Authorization",
    "cookie",
    "token",
    "access_token",
    "accessToken",
    "accesstoken",
    "x-auth-token",
    "mytoken",
    "password",
    "db_password",
    "secret",
    "client_secret",
    "clientSecret",
)
_SEPARATORS = ("=", ": ", " = ", ":")
_PREFIXES = ("", "Bearer ", "bearer ")


def _corpus() -> list[str]:
    shapes = [
        f"request failed {key}{separator}{prefix}{SECRET} retrying"
        for key, separator, prefix in itertools.product(_KEYS, _SEPARATORS, _PREFIXES)
    ]
    shapes += [f'body {{"{key}": "{SECRET}"}}' for key in _KEYS if "-" not in key] + [
        f"body {{'{key}': '{SECRET}'}}" for key in _KEYS if "-" not in key
    ]
    shapes += [
        f"GET https://api.example.org/v1?api_key={SECRET}&page=2 failed",
        f"GET https://api.example.org/v1?token={SECRET} failed",
        f"GET https://user:{SECRET}@api.example.org/v1 failed",
        f"Authorization: Bearer {SECRET}",
        f"secret={SECRET};next",
        f"password={SECRET}&next=1",
        f"key=sk-{SECRET}abcdef",
    ]
    return shapes


CORPUS = _corpus()


@pytest.mark.parametrize("oracle", sorted(ORACLES))
def test_canonical_source_redaction_is_at_least_as_strong_as_each_oracle(oracle):
    redact = ORACLES[oracle]
    covered = [text for text in CORPUS if SECRET not in redact(text)]
    assert len(covered) > len(CORPUS) // 2
    leaks = [
        text
        for text in covered
        if SECRET in provider_error_text(RuntimeError(text))
        or SECRET in redact_text(text)
    ]
    assert leaks == []


def test_provider_error_text_names_an_empty_exception():
    assert provider_error_text(TimeoutError()) == "TimeoutError"


def _poll(feed_checker, source):
    return feed_checker.poll(SourcePollContext(source=source, cursor_before=None))


def _failing(*_args, **_kwargs):
    raise RuntimeError(f"upstream said token={SECRET} for https://u:{SECRET}@h/x")


@pytest.mark.parametrize(
    ("feed_checker", "source"),
    [
        (
            ApiListDictsFeedChecker(http_get=_failing, now=lambda: datetime.now(UTC)),
            {"url": "https://api.example.org/items", "config": {"list_path": ""}},
        ),
        (
            CourtListenerDocketFeedChecker(provider=_failing),
            {"url": "https://www.courtlistener.com/docket/123/x/"},
        ),
        (
            YouTubeCollectionFeedChecker(YOUTUBE_PLAYLIST, provider=_failing),
            {"url": "https://www.youtube.com/playlist?list=PLabc123"},
        ),
        (
            TikTokProfileFeedChecker(provider=_failing),
            {"url": "https://www.tiktok.com/@example"},
        ),
    ],
    ids=["api_list_dicts", "courtlistener", "youtube", "tiktok"],
)
def test_each_feed_checker_redacts_provider_failures(feed_checker, source):
    with pytest.raises(Exception) as error:
        _poll(feed_checker, source)
    assert SECRET not in str(error.value)


def test_feed_checker_warnings_are_redacted():
    youtube = YouTubeCollectionFeedChecker(
        YOUTUBE_CHANNEL,
        provider=lambda *_a, **_k: YouTubeChannelListing(
            entries=[], warnings=[f"retry with accessToken={SECRET}"]
        ),
    )
    court = CourtListenerDocketFeedChecker(
        provider=lambda **_k: CourtListenerDocketListing(
            docket={"id": 123}, warnings=[f'{{"token": "{SECRET}"}}']
        )
    )
    for feed_checker, source in (
        (youtube, {"url": "https://www.youtube.com/@example"}),
        (court, {"url": "https://www.courtlistener.com/docket/123/x/"}),
    ):
        result = _poll(feed_checker, source)
        assert result.warnings
        assert all(SECRET not in warning for warning in result.warnings)
