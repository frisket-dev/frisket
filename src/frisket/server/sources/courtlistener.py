"""CourtListener docket source poller.

The v1 connector materializes docket metadata rows only. RECAP PDFs are linked
as media metadata under ``link_only`` and are never downloaded by this poller.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from frisket.server.sources.runtime import (
    SourcePollContext,
    SourcePollItem,
    SourcePollResult,
    register_source_poller,
)

COURTLISTENER_DOCKET_KIND = "courtlistener_docket"
COURTLISTENER_CURSOR_SCHEMA = "frisket.courtlistener_cursor.v1"
COURTLISTENER_SOURCE_SCHEMA = "frisket.source.courtlistener_docket.v1"
COURTLISTENER_PAGE_SIZE = 500
MAX_CURSOR_ITEM_HASH_BYTES = 200_000
COURTLISTENER_BASE_URL = "https://www.courtlistener.com"
_DOCKET_ID_RE = re.compile(r"^[1-9][0-9]*$")
_SENSITIVE_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|cookie|token|password|secret)"
    r"(\s*[:=]\s*(?:bearer\s+)?)?[^\s,;&]+"
)
_STRUCTURED_SECRET_RE = re.compile(
    r"(?i)((?:[\"']?(?:api[_-]?key|authorization|cookie|token|password|secret)"
    r"[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)"
    r"[^\"'\s,;&}]+)"
)
_URL_CREDENTIAL_RE = re.compile(r"(?i)(https?://)([^/\s:@]+):([^@\s/]+)@")
_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "docket_id",
        "docket_url",
        "url",
        "keywords",
        "include_parties",
        "include_documents",
        "recap_pdf_policy",
        "max_entries_per_poll",
    }
)
_DISALLOWED_CONFIG_KEYS = frozenset(
    {
        "api_key",
        "api_token",
        "auth",
        "authorization",
        "connection_id",
        "cookie",
        "cookies",
        "header",
        "headers",
        "password",
        "secret",
        "secret_ref",
        "secrets",
        "token",
    }
)


class CourtListenerProviderError(RuntimeError):
    """Raised for redacted CourtListener provider failures."""


@dataclass(frozen=True)
class CourtListenerDocketListing:
    docket: dict[str, Any]
    parties: list[dict[str, Any]] = field(default_factory=list)
    docket_entries: list[dict[str, Any]] = field(default_factory=list)
    entries_truncated: bool = False
    recap_documents: list[dict[str, Any]] = field(default_factory=list)
    provider_facts: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CourtListenerDocketConfig:
    docket_id: str
    docket_url: str
    keywords: tuple[str, ...]
    include_parties: bool
    include_documents: bool
    recap_pdf_policy: str
    max_entries_per_poll: int | None


CourtListenerDocketProvider = Callable[..., CourtListenerDocketListing]


class CourtListenerDocketPoller:
    kind = COURTLISTENER_DOCKET_KIND

    def __init__(
        self,
        provider: CourtListenerDocketProvider | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider or fetch_courtlistener_docket
        self._requires_env_token = provider is None
        self._now = now or (lambda: datetime.now(UTC))

    def validate_config(self, source: dict[str, Any]) -> str | None:
        _config, error = _resolve_config(source)
        return error

    def poll(self, ctx: SourcePollContext) -> SourcePollResult:
        config, error = _resolve_config(ctx.source)
        if error:
            raise ValueError(error)
        assert config is not None
        if (
            self._requires_env_token
            and not os.environ.get("COURTLISTENER_API_TOKEN", "").strip()
        ):
            raise CourtListenerProviderError(
                "CourtListener docket provider disabled: "
                "COURTLISTENER_API_TOKEN is not set"
            )

        try:
            listing = self._provider(
                docket_id=config.docket_id,
                docket_url=config.docket_url,
                include_parties=config.include_parties,
                include_documents=config.include_documents,
                max_entries=config.max_entries_per_poll,
            )
        except Exception as exc:  # noqa: BLE001 - source.poll stores failures
            raise CourtListenerProviderError(
                f"CourtListener docket provider failed: {_redact_provider_message(exc)}"
            ) from exc

        docket = listing.docket
        raw_entries = listing.docket_entries
        truncated = listing.entries_truncated or (
            config.max_entries_per_poll is not None
            and len(raw_entries) > config.max_entries_per_poll
        )
        entries = (
            raw_entries
            if config.max_entries_per_poll is None
            else raw_entries[: config.max_entries_per_poll]
        )
        parties = listing.parties if config.include_parties else []
        documents = (
            _documents_for_entries(entries, fallback=listing.recap_documents)
            if config.include_documents
            else []
        )

        warnings = [_redact_warning(warning) for warning in listing.warnings]
        if truncated:
            warnings.append(
                "courtlistener_docket entry cap reached; limited to "
                f"{config.max_entries_per_poll} entries"
            )

        items: list[SourcePollItem] = [
            _docket_snapshot_item(config=config, docket=docket)
        ]
        items.extend(
            _party_item(config=config, docket=docket, party=party) for party in parties
        )
        items.extend(
            _entry_item(config=config, docket=docket, entry=entry) for entry in entries
        )
        items.extend(
            _document_item(
                config=config,
                docket=docket,
                document=document,
                entry=entry,
            )
            for document, entry in documents
        )

        item_hashes = {item.stable_item_id(): item.stable_hash() for item in items}
        if _json_size(item_hashes) > MAX_CURSOR_ITEM_HASH_BYTES:
            warnings.append(
                "courtlistener_docket cursor hash set is large; lower "
                "max_entries_per_poll if source_runs storage becomes noisy"
            )
        previous_hashes = _cursor_item_hashes(ctx.cursor_before)
        for item_id in sorted(item_hashes):
            previous = previous_hashes.get(item_id)
            if previous is not None and previous != item_hashes[item_id]:
                warnings.append(
                    f"courtlistener_docket existing item changed: {item_id}"
                )

        entry_item_ids = [_entry_item_id(config.docket_id, entry) for entry in entries]
        document_item_ids = [
            _document_item_id(config.docket_id, document)
            for document, _entry in documents
        ]
        party_item_ids = [_party_item_id(config.docket_id, party) for party in parties]
        cursor_after = {
            "schema_version": COURTLISTENER_CURSOR_SCHEMA,
            "source_kind": COURTLISTENER_DOCKET_KIND,
            "docket_id": config.docket_id,
            "docket_url_hash": _text_hash(config.docket_url),
            "entry_count": len(entries),
            "party_count": len(parties),
            "document_count": len(documents),
            "entry_ids_hash": _hash_json(sorted(entry_item_ids)),
            "party_ids_hash": _hash_json(sorted(party_item_ids)),
            "document_ids_hash": _hash_json(sorted(document_item_ids)),
            "item_hashes": item_hashes,
            "last_poll_completed_at": _iso_timestamp(self._now()),
            "max_entries_per_poll": config.max_entries_per_poll,
            "truncated": truncated,
            "warning_count": len(warnings),
            "recap_pdf_policy": config.recap_pdf_policy,
        }
        provider_use = [
            {
                "provider": "courtlistener",
                "service": str(
                    listing.provider_facts.get("service") or "courtlistener_api"
                ),
                "source_kind": COURTLISTENER_DOCKET_KIND,
                "docket_id": config.docket_id,
                "docket_url_hash": _text_hash(config.docket_url),
                "entries_returned": len(raw_entries),
                "entries_materialized": len(entries),
                "parties_materialized": len(parties),
                "documents_materialized": len(documents),
                "items_materialized": len(items),
                "include_parties": config.include_parties,
                "include_documents": config.include_documents,
                "recap_pdf_policy": config.recap_pdf_policy,
                "max_entries_per_poll": config.max_entries_per_poll,
                "download": False,
                "external_api": True,
                "cost_actual": 0.0,
                **{
                    str(k): v
                    for k, v in listing.provider_facts.items()
                    if k != "service"
                },
            }
        ]
        summary = {
            "schema_version": COURTLISTENER_CURSOR_SCHEMA,
            "source_kind": COURTLISTENER_DOCKET_KIND,
            "docket_id": config.docket_id,
            "docket_url_hash": _text_hash(config.docket_url),
            "entry_count": len(entries),
            "party_count": len(parties),
            "document_count": len(documents),
            "items_materialized": len(items),
            "matched_keyword_rows": sum(
                1 for item in items if item.row.get("matched_keywords")
            ),
            "recap_pdf_policy": config.recap_pdf_policy,
            "max_entries_per_poll": config.max_entries_per_poll,
            "truncated": truncated,
            "warnings": len(warnings),
        }
        return SourcePollResult(
            items=items,
            cursor_after=cursor_after,
            warnings=warnings,
            provider_use=provider_use,
            cost={"cost_micro": 0},
            summary=summary,
        )


def fetch_courtlistener_docket(
    *,
    docket_id: str,
    docket_url: str,
    include_parties: bool,
    include_documents: bool,
    max_entries: int | None,
    timeout: float = 20.0,
) -> CourtListenerDocketListing:
    """Fetch a CourtListener snapshot through bounded public API pages."""
    import httpx

    token = os.environ.get("COURTLISTENER_API_TOKEN", "").strip()
    headers = {
        "Accept": "application/json",
        "User-Agent": "frisket-courtlistener-docket-source/1",
    }
    if token:
        headers["Authorization"] = f"Token {token}"
    timeout_config = httpx.Timeout(timeout)
    limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
    endpoints_called: list[str] = []
    with httpx.Client(timeout=timeout_config, limits=limits, headers=headers) as client:
        docket = _get_courtlistener_json(
            client,
            f"{COURTLISTENER_BASE_URL}/api/rest/v4/dockets/{docket_id}/",
            endpoints_called=endpoints_called,
        )
        party_results = (
            _get_courtlistener_results(
                client,
                f"{COURTLISTENER_BASE_URL}/api/rest/v4/parties/",
                params={"docket": docket_id, "page_size": COURTLISTENER_PAGE_SIZE},
                endpoints_called=endpoints_called,
            )
            if include_parties
            else _CourtListenerResults(items=[], truncated=False)
        )
        entry_params: dict[str, Any] = {
            "docket": docket_id,
            "order_by": "-date_filed",
            "page_size": min(
                max_entries or COURTLISTENER_PAGE_SIZE, COURTLISTENER_PAGE_SIZE
            ),
        }
        if not include_documents:
            entry_params["omit"] = "recap_documents"
        entry_results = _get_courtlistener_results(
            client,
            f"{COURTLISTENER_BASE_URL}/api/rest/v4/docket-entries/",
            params=entry_params,
            endpoints_called=endpoints_called,
            max_results=max_entries,
        )
    return CourtListenerDocketListing(
        docket=docket,
        parties=party_results.items,
        docket_entries=entry_results.items,
        entries_truncated=entry_results.truncated,
        recap_documents=[],
        provider_facts={
            "service": "courtlistener_api",
            "endpoints_called": endpoints_called,
            "token_present": bool(token),
            "docket_url_hash": _text_hash(docket_url),
            "entries_truncated": entry_results.truncated,
        },
    )


def register_courtlistener_docket_poller(
    provider: CourtListenerDocketProvider | None = None,
    *,
    now: Callable[[], datetime] | None = None,
    replace: bool = False,
) -> CourtListenerDocketPoller:
    poller = CourtListenerDocketPoller(provider=provider, now=now)
    register_source_poller(poller, replace=replace)
    return poller


def _resolve_config(
    source: dict[str, Any],
) -> tuple[CourtListenerDocketConfig | None, str | None]:
    raw_config = source.get("config") if isinstance(source.get("config"), dict) else {}
    assert isinstance(raw_config, dict)
    normalized_keys = {_normalize_key(key) for key in raw_config}
    disallowed = sorted(normalized_keys & _DISALLOWED_CONFIG_KEYS)
    if disallowed:
        return (
            None,
            "courtlistener_docket does not support tokens, auth headers, "
            f"or secrets in source config: {', '.join(disallowed)}",
        )
    unsupported = sorted(set(raw_config) - _ALLOWED_CONFIG_KEYS)
    if unsupported:
        return None, f"courtlistener_docket unsupported config field: {unsupported[0]}"
    schema_version = raw_config.get("schema_version")
    if schema_version is not None and schema_version != COURTLISTENER_SOURCE_SCHEMA:
        return None, (
            f"courtlistener_docket schema_version must be {COURTLISTENER_SOURCE_SCHEMA}"
        )

    docket_id, docket_url, id_error = _resolve_docket_ref(source, raw_config)
    if id_error:
        return None, id_error
    assert docket_id is not None
    assert docket_url is not None

    keywords, keyword_error = _keywords(raw_config.get("keywords", []))
    if keyword_error:
        return None, keyword_error
    include_parties, parties_error = _config_bool(
        raw_config,
        "include_parties",
        default=True,
    )
    if parties_error:
        return None, parties_error
    include_documents, documents_error = _config_bool(
        raw_config,
        "include_documents",
        default=True,
    )
    if documents_error:
        return None, documents_error
    recap_pdf_policy = raw_config.get("recap_pdf_policy", "link_only")
    if recap_pdf_policy != "link_only":
        return None, "courtlistener_docket recap_pdf_policy must be link_only"
    max_entries, entries_error = _config_positive_int(
        raw_config,
        "max_entries_per_poll",
        default=None,
        maximum=None,
    )
    if entries_error:
        return None, entries_error

    return (
        CourtListenerDocketConfig(
            docket_id=docket_id,
            docket_url=docket_url,
            keywords=tuple(keywords),
            include_parties=include_parties,
            include_documents=include_documents,
            recap_pdf_policy="link_only",
            max_entries_per_poll=max_entries,
        ),
        None,
    )


def _resolve_docket_ref(
    source: dict[str, Any],
    config: dict[str, Any],
) -> tuple[str | None, str | None, str | None]:
    docket_id_value = config.get("docket_id")
    url_value = config.get("docket_url") or config.get("url") or source.get("url")
    docket_id: str | None = None
    docket_url: str | None = None
    if docket_id_value is not None:
        docket_id, id_error = _docket_id_from_scalar(docket_id_value)
        if id_error:
            return None, None, id_error
    if url_value is not None:
        url_id, parsed_url, url_error = _docket_id_from_url(url_value)
        if url_error and docket_id is None:
            return None, None, url_error
        if url_error is None:
            if docket_id is not None and url_id != docket_id:
                return None, None, "courtlistener_docket docket_id and URL disagree"
            docket_id = url_id
            docket_url = parsed_url
    if docket_id is None:
        return (
            None,
            None,
            "courtlistener_docket sources require docket_id or CourtListener "
            "docket URL",
        )
    return docket_id, docket_url or _canonical_docket_url(docket_id), None


def _docket_id_from_scalar(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, bool):
        return None, "courtlistener_docket docket_id must be a positive integer"
    if isinstance(value, int):
        if value <= 0:
            return None, "courtlistener_docket docket_id must be a positive integer"
        return str(value), None
    if isinstance(value, str):
        text = value.strip()
        if _DOCKET_ID_RE.fullmatch(text):
            return text, None
    return None, "courtlistener_docket docket_id must be a positive integer"


def _docket_id_from_url(value: Any) -> tuple[str | None, str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, None, "courtlistener_docket docket URL must be a URL"
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        return None, None, "courtlistener_docket URL must be a CourtListener URL"
    host = parsed.netloc.lower().split("@")[-1].split(":", 1)[0]
    if host not in {"courtlistener.com", "www.courtlistener.com"}:
        return None, None, "courtlistener_docket URL must be a CourtListener URL"
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] != "docket" or not _DOCKET_ID_RE.fullmatch(parts[1]):
        return None, None, "courtlistener_docket URL must be a docket URL"
    if parsed.username or parsed.password:
        return None, None, "courtlistener_docket URL must not include credentials"
    for key in parse_qs(parsed.query):
        if _normalize_key(key) in _DISALLOWED_CONFIG_KEYS:
            return (
                None,
                None,
                "courtlistener_docket URL must not include secret query parameters",
            )
    return parts[1], _canonicalize_courtlistener_url(parts), None


def _keywords(value: Any) -> tuple[list[str], str | None]:
    if value is None:
        return [], None
    if not isinstance(value, list):
        return [], "courtlistener_docket keywords must be a list of strings"
    keywords: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            return [], "courtlistener_docket keywords must be a list of strings"
        keyword = raw.strip().lower()
        if not keyword:
            continue
        if keyword not in seen:
            keywords.append(keyword)
            seen.add(keyword)
    return keywords, None


def _config_bool(
    config: dict[str, Any],
    field_name: str,
    *,
    default: bool,
) -> tuple[bool, str | None]:
    value = config.get(field_name, default)
    if type(value) is not bool:
        return False, f"courtlistener_docket {field_name} must be a boolean"
    return bool(value), None


def _config_positive_int(
    config: dict[str, Any],
    field_name: str,
    *,
    default: int | None,
    maximum: int | None,
) -> tuple[int | None, str | None]:
    value = config.get(field_name, default)
    if value is None and default is None:
        return None, None
    if type(value) is not int or value <= 0:
        return None, f"courtlistener_docket {field_name} must be a positive integer"
    if maximum is not None and value > maximum:
        return None, f"courtlistener_docket {field_name} must be <= {maximum}"
    return int(value), None


def _docket_snapshot_item(
    *,
    config: CourtListenerDocketConfig,
    docket: dict[str, Any],
) -> SourcePollItem:
    source_item_id = f"courtlistener:docket:{config.docket_id}"
    row = {
        **_common_docket_row(config, docket),
        "item_type": "docket_snapshot",
        "matched_keywords": [],
    }
    return SourcePollItem(
        source_item_id=source_item_id,
        dedupe_key=source_item_id,
        item_hash=_normalized_item_hash(row),
        title=row.get("case_name"),
        url=row.get("source_url"),
        row=row,
        raw=docket,
    )


def _party_item(
    *,
    config: CourtListenerDocketConfig,
    docket: dict[str, Any],
    party: dict[str, Any],
) -> SourcePollItem:
    source_item_id = _party_item_id(config.docket_id, party)
    party_name = party.get("name")
    party_type = party.get("type")
    row = {
        **_common_docket_row(config, docket),
        "item_type": "party",
        "party_name": party_name,
        "party_type": party_type,
        "matched_keywords": [],
    }
    return SourcePollItem(
        source_item_id=source_item_id,
        dedupe_key=source_item_id,
        item_hash=_normalized_item_hash(row),
        title=party_name,
        url=row.get("source_url"),
        row=row,
        raw=party,
    )


def _entry_item(
    *,
    config: CourtListenerDocketConfig,
    docket: dict[str, Any],
    entry: dict[str, Any],
) -> SourcePollItem:
    source_item_id = _entry_item_id(config.docket_id, entry)
    description = entry.get("description")
    entry_date = entry.get("date_filed")
    row = {
        **_common_docket_row(config, docket),
        "item_type": "docket_entry",
        "entry_number": entry.get("entry_number"),
        "entry_date": entry_date,
        "description": description,
        "matched_keywords": _matched_keywords(config.keywords, description),
    }
    source_url = _absolute_courtlistener_url(
        entry.get("absolute_url"),
        default=_docket_source_url(config, docket),
    )
    return SourcePollItem(
        source_item_id=source_item_id,
        dedupe_key=source_item_id,
        item_hash=_normalized_item_hash(row),
        title=description,
        url=source_url,
        published_at=entry_date,
        row=row,
        raw=entry,
    )


def _document_item(
    *,
    config: CourtListenerDocketConfig,
    docket: dict[str, Any],
    document: dict[str, Any],
    entry: dict[str, Any] | None,
) -> SourcePollItem:
    source_item_id = _document_item_id(config.docket_id, document)
    document_url = _absolute_courtlistener_url(
        document.get("absolute_url"), default=None
    )
    document_description = document.get("description")
    document_id = _slug_key(str(document["id"]))
    media = []
    if document_url:
        media.append(
            {
                "kind": "recap_pdf",
                "provider": "courtlistener",
                "document_id": document_id,
                "document_url": document_url,
                "recap_pdf_policy": config.recap_pdf_policy,
                "downloaded": False,
            }
        )
    row = {
        **_common_docket_row(config, docket),
        "item_type": "recap_document",
        "entry_number": entry.get("entry_number") if entry is not None else None,
        "document_number": document.get("document_number"),
        "document_description": document_description,
        "document_url": document_url,
        "recap_available": bool(document_url),
        "matched_keywords": _matched_keywords(config.keywords, document_description),
    }
    return SourcePollItem(
        source_item_id=source_item_id,
        dedupe_key=source_item_id,
        item_hash=_normalized_item_hash(row, media=media),
        title=document_description,
        url=document_url,
        row=row,
        raw=document,
        media=media,
    )


def _common_docket_row(
    config: CourtListenerDocketConfig,
    docket: dict[str, Any],
) -> dict[str, Any]:
    return {
        "docket_id": _docket_id_value(config.docket_id),
        "case_name": docket.get("case_name"),
        "court_id": docket.get("court_id"),
        "court_name": docket.get("court_name"),
        "docket_number": docket.get("docket_number"),
        "date_filed": docket.get("date_filed"),
        "source_url": _docket_source_url(config, docket),
    }


def _docket_source_url(
    config: CourtListenerDocketConfig,
    docket: dict[str, Any],
) -> str:
    return _absolute_courtlistener_url(
        docket.get("absolute_url"),
        default=config.docket_url,
    )


def _documents_for_entries(
    entries: list[dict[str, Any]],
    *,
    fallback: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any] | None]]:
    nested = [
        (document, entry)
        for entry in entries
        for document in entry.get("recap_documents", [])
    ]
    if nested:
        return nested

    retained_entry_numbers = {
        str(entry_number)
        for entry in entries
        if (entry_number := entry.get("entry_number")) is not None
    }
    return [
        (document, None)
        for document in fallback
        if (document_number := document.get("document_number")) is None
        or str(document_number) in retained_entry_numbers
        or any(
            str(document_number).startswith(f"{entry_number}-")
            for entry_number in retained_entry_numbers
        )
    ]


def _party_item_id(docket_id: str, party: dict[str, Any]) -> str:
    return f"courtlistener:party:{docket_id}:{_party_stable_key(party)}"


def _entry_item_id(docket_id: str, entry: dict[str, Any]) -> str:
    return f"courtlistener:docket_entry:{_entry_stable_key(entry)}"


def _document_item_id(docket_id: str, document: dict[str, Any]) -> str:
    return (
        "courtlistener:recap_document:"
        f"{_slug_key(docket_id)}:{_document_stable_key(document)}"
    )


def _party_stable_key(party: dict[str, Any]) -> str:
    return _slug_key(str(party["id"]))


def _entry_stable_key(entry: dict[str, Any]) -> str:
    return _slug_key(str(entry["id"]))


def _document_stable_key(
    document: dict[str, Any],
) -> str:
    return _slug_key(str(document["id"]))


def _matched_keywords(keywords: tuple[str, ...], text: str | None) -> list[str]:
    if not keywords or not text:
        return []
    lowered = text.lower()
    return [keyword for keyword in keywords if keyword in lowered]


def _cursor_item_hashes(cursor_before: str | None) -> dict[str, str]:
    if not cursor_before:
        return {}
    try:
        cursor = json.loads(cursor_before)
    except (TypeError, ValueError):
        return {}
    if not isinstance(cursor, dict):
        return {}
    hashes = cursor.get("item_hashes")
    if not isinstance(hashes, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in hashes.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def _get_courtlistener_json(
    client: Any,
    url: str,
    *,
    endpoints_called: list[str],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    endpoint = _endpoint_label(url)
    endpoints_called.append(endpoint)
    try:
        response = client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - caller redacts
        raise CourtListenerProviderError(
            f"CourtListener API request failed for {endpoint}: "
            f"{_redact_provider_message(exc)}"
        ) from exc
    return payload


@dataclass(frozen=True)
class _CourtListenerResults:
    items: list[dict[str, Any]]
    truncated: bool


def _get_courtlistener_results(
    client: Any,
    url: str,
    *,
    params: dict[str, Any],
    endpoints_called: list[str],
    max_results: int | None = None,
) -> _CourtListenerResults:
    results: list[dict[str, Any]] = []
    truncated = False
    next_url: str | None = url
    next_params: dict[str, Any] | None = params
    seen_urls: set[str] = set()
    while next_url is not None:
        if next_url in seen_urls:
            raise CourtListenerProviderError("CourtListener API pagination loop")
        seen_urls.add(next_url)
        payload = _get_courtlistener_json(
            client,
            next_url,
            params=next_params,
            endpoints_called=endpoints_called,
        )
        page = payload["results"]
        if not isinstance(page, list):
            raise CourtListenerProviderError("CourtListener API results must be a list")
        remaining = None if max_results is None else max_results - len(results)
        raw_next = payload.get("next")
        if remaining is not None:
            results.extend(page[:remaining])
        else:
            results.extend(page)
        if max_results is not None and len(results) >= max_results:
            truncated = len(page) > remaining or raw_next is not None
            break
        if raw_next is None:
            break
        next_url = _safe_courtlistener_next_url(raw_next, endpoint_url=url)
        next_params = None
    return _CourtListenerResults(items=results, truncated=truncated)


def _safe_courtlistener_next_url(value: Any, *, endpoint_url: str) -> str:
    if not isinstance(value, str) or not value:
        raise CourtListenerProviderError("CourtListener API next link is invalid")
    parsed = urlparse(value)
    expected = urlparse(endpoint_url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise CourtListenerProviderError(
            "CourtListener API next link is invalid"
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected.hostname
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path != expected.path
    ):
        raise CourtListenerProviderError("CourtListener API next link is not trusted")
    return value


def _endpoint_label(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or "/"


def _absolute_courtlistener_url(
    value: str | None, *, default: str | None
) -> str | None:
    if not value:
        return default
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    if value.startswith("/"):
        return COURTLISTENER_BASE_URL + value
    return default


def _canonical_docket_url(docket_id: str) -> str:
    return f"{COURTLISTENER_BASE_URL}/docket/{docket_id}/"


def _canonicalize_courtlistener_url(parts: list[str]) -> str:
    return f"{COURTLISTENER_BASE_URL}/{'/'.join(parts)}/"


def _docket_id_value(docket_id: str) -> int | str:
    return int(docket_id) if _DOCKET_ID_RE.fullmatch(docket_id) else docket_id


def _slug_key(value: str) -> str:
    lowered = value.strip().lower()
    return re.sub(r"[^a-z0-9._:-]+", "-", lowered).strip("-")


def _normalize_key(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")


def _normalized_item_hash(
    row: dict[str, Any],
    *,
    media: list[dict[str, Any]] | None = None,
) -> str:
    payload = {
        "row": {key: value for key, value in row.items() if key != "source_raw"},
        "media": media or [],
    }
    return _hash_json(payload)


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _json_size(value: Any) -> int:
    return len(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str))


def _text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso_timestamp(value: datetime) -> str:
    dt = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _redact_provider_message(exc: BaseException) -> str:
    message = str(exc) or exc.__class__.__name__
    message = _SENSITIVE_RE.sub(r"\1=<redacted>", message)
    message = _STRUCTURED_SECRET_RE.sub(
        lambda m: _redact_secret_match(m.group(1)), message
    )
    return _URL_CREDENTIAL_RE.sub(r"\1<redacted>@", message)


def _redact_warning(warning: Any) -> str:
    message = _SENSITIVE_RE.sub(r"\1=<redacted>", str(warning))
    message = _STRUCTURED_SECRET_RE.sub(
        lambda m: _redact_secret_match(m.group(1)), message
    )
    return _URL_CREDENTIAL_RE.sub(r"\1<redacted>@", message)


def _redact_secret_match(value: str) -> str:
    for separator in (":", "="):
        if separator in value:
            key = value.split(separator, 1)[0]
            return f"{key}{separator}<redacted>"
    return "<redacted>"


register_courtlistener_docket_poller()
