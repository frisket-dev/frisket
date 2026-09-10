"""Cycle-safe credential and error redaction.

This module is deliberately a standard-library-only leaf so logging, queue,
worker, provider, trace, store, and server boundaries can all depend on it
without creating an import cycle. It never discovers or retains credentials;
callers may pass only values they already own for the duration of one call.

The pattern grammar below is FROZEN: no new pattern
families. Known-credential-value replacement at write boundaries
(``secret_values=``) is the load-bearing mechanism; the patterns are
best-effort backup for values the caller could not name. Cycle-safety,
depth/size budgets, and idempotent safe markers defend against ordinary
bugs; this module does not defend against hostile objects — an attacker
who can hand it a hostile object already executes in this process.
"""

from __future__ import annotations

import re
import traceback
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import TracebackType
from typing import Any
from urllib.parse import unquote_plus, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
CYCLE = "[CYCLE]"
TRUNCATED = "[TRUNCATED]"

DEFAULT_ERROR_TEXT_MAX_CHARS = 1_000
DEFAULT_LOG_STRING_MAX_CHARS = 4_096
DEFAULT_TRACE_STRING_MAX_CHARS = 4_096
MAX_REDACTION_INPUT_CHARS = 65_536
MAX_SAFE_FRAMES = 12

_TRACEBACK_REDACTED = "[TRACEBACK REDACTED]"
_MAX_REDACTION_ITEMS = 256
_MAX_SECRET_VALUES = 64
_MAX_SECRET_VALUE_CHARS = MAX_REDACTION_INPUT_CHARS
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_STORED_ERROR_RE = re.compile(r"^([a-z][a-z0-9_]{0,63}):\s*(.*)$", re.DOTALL)
_CONTROL_OR_WHITESPACE_RE = re.compile(r"[\s\x00-\x1f\x7f]+")
_SAFE_MARKER_RE = re.compile(
    r"(?i)(\[(?:redacted(?:-key|-jwt)?|cycle|truncated|traceback redacted)\])"
)
_FRAME_LINE_RE = re.compile(r'^\s*File\s+"[^"]*",\s+line\s+\d+(?:,\s+in\s+.*)?$')
_EXCEPTION_GROUP_HEADER_RE = re.compile(
    r"^\s*\+\s+Exception Group Traceback \(most recent call last\):$"
)
_EXCEPTION_GROUP_LINE_RE = re.compile(r"^\s*[|+].*$")
_EXCEPTION_GROUP_END_RE = re.compile(r"^\s*\+-+\s*$")
_CHAIN_LINE_RE = re.compile(
    r"^(?:During handling of the above exception, another exception occurred:|"
    r"The above exception was the direct cause of the following exception:)$"
)

_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "password",
        "passwd",
        "secret",
        "secret_key",
        "signing_secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "access_key",
        "private_key",
        "client_secret",
        "database_url",
        "dsn",
        "webhook_url",
        "auth_session",
        "auth_session_id",
        # Backward-security invariants: keep even though nothing else references them.
        "stripe_key",
        "resend_key",
    }
)
_SENSITIVE_SUFFIXES = (
    "_token",
    "_cookie",
    "_api_key",
    "_apikey",
    "_password",
    "_passwd",
    "_secret",
    "_secret_key",
    "_access_key",
    "_private_key",
    "_client_secret",
    "_database_url",
    "_dsn",
)
_SENSITIVE_URL_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "client_secret",
        "password",
        "secret",
        "signature",
        "sig",
        "token",
        "x_amz_signature",
        "x_goog_signature",
    }
)

_JSON_ASSIGNMENT_RES = (
    re.compile(
        r"(?P<prefix>[\"\'](?P<key>[A-Za-z_][A-Za-z0-9_-]{0,80})[\"\']"
        r'\s*:\s*(?P<quote>"))(?P<value>(?:\\[^\r\n]|[^"\\\r\n])*)'
        r'(?P<suffix>")'
    ),
    re.compile(
        r"(?P<prefix>[\"'](?P<key>[A-Za-z_][A-Za-z0-9_-]{0,80})[\"']"
        r"\s*:\s*(?P<quote>'))(?P<value>(?:\\[^\r\n]|[^'\\\r\n])*)"
        r"(?P<suffix>')"
    ),
)
_QUOTED_ASSIGNMENT_RES = (
    re.compile(
        r"(?i)(?P<prefix>\b(?P<key>[A-Za-z_][A-Za-z0-9_-]{0,80})\b"
        r'\s*[:=]\s*(?:(?:Bearer|Basic)\s+)?)(?P<quote>")'
        r'(?P<value>(?:\\[^\r\n]|[^"\\\r\n])*)(?P<suffix>")'
    ),
    re.compile(
        r"(?i)(?P<prefix>\b(?P<key>[A-Za-z_][A-Za-z0-9_-]{0,80})\b"
        r"\s*[:=]\s*(?:(?:Bearer|Basic)\s+)?)(?P<quote>')"
        r"(?P<value>(?:\\[^\r\n]|[^'\\\r\n])*)(?P<suffix>')"
    ),
)
_UNCLOSED_JSON_ASSIGNMENT_RES = (
    re.compile(
        r'(?im)(?P<prefix>"(?P<key>[A-Za-z_][A-Za-z0-9_-]{0,80})"\s*:\s*'
        r'(?P<quote>"))(?P<value>(?:\\[^\r\n]|[^"\\\r\n])*)$'
    ),
    re.compile(
        r"(?im)(?P<prefix>'(?P<key>[A-Za-z_][A-Za-z0-9_-]{0,80})'\s*:\s*"
        r"(?P<quote>'))(?P<value>(?:\\[^\r\n]|[^'\\\r\n])*)$"
    ),
)
_UNCLOSED_ASSIGNMENT_RES = (
    re.compile(
        r"(?im)(?P<prefix>\b(?P<key>[A-Za-z_][A-Za-z0-9_-]{0,80})\b"
        r'\s*[:=]\s*(?:(?:Bearer|Basic)\s+)?(?P<quote>"))'
        r'(?P<value>(?:\\[^\r\n]|[^"\\\r\n])*)$'
    ),
    re.compile(
        r"(?im)(?P<prefix>\b(?P<key>[A-Za-z_][A-Za-z0-9_-]{0,80})\b"
        r"\s*[:=]\s*(?:(?:Bearer|Basic)\s+)?(?P<quote>'))"
        r"(?P<value>(?:\\[^\r\n]|[^'\\\r\n])*)$"
    ),
)
_ASSIGNMENT_RE = re.compile(
    r"(?i)(?P<prefix>\b(?P<key>[A-Za-z_][A-Za-z0-9_-]{0,80})\b"
    r"\s*[:=]\s*(?:(?:Bearer|Basic)\s+)?)"
    r"(?P<quote>[\"']?)(?P<value>[^\s,;}\"']+)(?P<suffix>[\"']?)"
)
_HEADER_RE = re.compile(
    r"(?im)(?P<prefix>\b(?:Authorization|Proxy-Authorization|Cookie|Set-Cookie)"
    r"\s*:\s*)(?P<value>[^\r\n]*)"
)
_BEARER_RE = re.compile(
    r"(?im)^(?P<leading>[ \t]*)(?P<scheme>Bearer|Basic)[ \t]+"
    r"(?P<value>\S+)"
)
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?P<pem_label>(?: [A-Z0-9]+)? PRIVATE KEY)-----.*?"
    r"(?:-----END(?P=pem_label)-----|\Z)",
    re.DOTALL,
)
_CREDENTIAL_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9_-])frisket_pat_[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9_-])gh[pousr]_[A-Za-z0-9]{8,}"),
    re.compile(r"(?<![A-Za-z0-9_-])github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"(?<![A-Za-z0-9_-])xox[baprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
)
_JWT_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_.-])"
)
# ``postgres(?:ql)?`` — the previous ``postgresql?`` optionalized only the
# final letter, so the canonical Heroku/SQLAlchemy ``postgres://`` scheme
# never matched and its userinfo password shipped raw. Same excluded-variant
# class: ``mongodb+srv://``. The scheme list itself stays frozen.
_URL_RE = re.compile(
    r"(?i)\b(?:https?|postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis)://"
    r"[^\s<>\"']+"
)


@dataclass(frozen=True, slots=True)
class SafeFrame:
    module: str | None
    path: str
    function: str
    line: int


@dataclass(frozen=True, slots=True)
class SafeError:
    code: str
    detail: str
    exception_type: str | None = None
    frames: tuple[SafeFrame, ...] = ()

    @property
    def text(self) -> str:
        # No ``<code>: `` envelope can fit in fewer than three characters.
        # ``safe_error`` uses an empty code only for those pathological limits
        # so redaction remains non-raising and honors the caller's hard bound.
        if not self.code:
            return self.detail
        return f"{self.code}: {self.detail}"


def canonical_error_code(
    value: object,
    *,
    fallback: str = "internal_error",
) -> str:
    """Return a bounded lower-snake-case code without raising."""
    safe_fallback = (
        fallback
        if type(fallback) is str and _ERROR_CODE_RE.fullmatch(fallback)
        else "internal_error"
    )
    if type(value) is not str:
        return safe_fallback
    return value if _ERROR_CODE_RE.fullmatch(value) else safe_fallback


def _normalize_key(value: str) -> str:
    return value.strip().lower().replace("-", "_")


def _is_sensitive_key(value: str) -> bool:
    normalized = _normalize_key(value)
    return normalized in _SENSITIVE_KEYS or any(
        normalized.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES
    )


def _is_safe_marker(value: str) -> bool:
    return _SAFE_MARKER_RE.fullmatch(value.strip()) is not None


def _safe_int(value: object, *, default: int) -> int:
    value_type = type(value)
    if value_type is int:
        return value
    if value_type is not str and value_type is not float:
        return default
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed


def _bounded_int(value: object, *, default: int, lower: int, upper: int) -> int:
    return min(max(_safe_int(value, default=default), lower), upper)


def _text_limit(value: object, *, default: int) -> int:
    parsed = _safe_int(value, default=default)
    return min(max(parsed, 0), MAX_REDACTION_INPUT_CHARS)


def _truncate(value: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    if max_chars == 1:
        return "…"
    return f"{value[: max_chars - 1]}…"


def _unprintable_marker(value: object) -> str:
    return f"[UNPRINTABLE {type(value).__name__}]"


def _materialize_secret_values(
    values: Iterable[str | None],
) -> tuple[str, ...]:
    materialized: list[str] = []
    seen: set[str] = set()
    attempts = 0
    for raw in values:
        attempts += 1
        if raw is not None:
            value = str(raw)[:_MAX_SECRET_VALUE_CHARS]
            if len(value) >= 4 and value not in seen:
                seen.add(value)
                materialized.append(value)
        if attempts >= _MAX_SECRET_VALUES or len(materialized) >= _MAX_SECRET_VALUES:
            break
    materialized.sort(key=lambda item: (-len(item), item))
    return tuple(materialized)


def _replace_known_secrets(value: str, secrets: tuple[str, ...]) -> str:
    if not secrets:
        return value
    parts = _SAFE_MARKER_RE.split(value)
    for index in range(0, len(parts), 2):
        part = parts[index]
        for secret in secrets:
            if len(secret) >= 8:
                part = part.replace(secret, REDACTED)
            else:
                part = re.sub(
                    rf"(?<![A-Za-z0-9_]){re.escape(secret)}(?![A-Za-z0-9_])",
                    REDACTED,
                    part,
                )
        parts[index] = part
    return "".join(parts)


def _redact_traceback_strings(value: str) -> str:
    """Remove Python traceback/stack serialization without reading source."""
    lines = value.splitlines(keepends=True)
    if not lines:
        return value
    output: list[str] = []
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.rstrip("\r\n")
        stripped = line.strip()
        is_exception_group = _EXCEPTION_GROUP_HEADER_RE.fullmatch(line) is not None
        if is_exception_group:
            start = index
            index += 1
            saw_end = False
            while index < len(lines):
                candidate = lines[index].rstrip("\r\n")
                if _EXCEPTION_GROUP_LINE_RE.fullmatch(candidate):
                    index += 1
                    if _EXCEPTION_GROUP_END_RE.fullmatch(candidate):
                        saw_end = True
                        break
                    continue
                if not candidate.strip():
                    break
                # A recognized but malformed group is safer to consume through
                # the next blank separator/end than to guess that an opaque
                # child continuation is ordinary prose.
                while index < len(lines) and lines[index].strip():
                    index += 1
                break
            if not saw_end:
                while index < len(lines) and lines[index].strip():
                    index += 1
            consumed = "".join(lines[start:index])
            newline = "\n" if consumed.endswith(("\n", "\r")) else ""
            output.append(f"{_TRACEBACK_REDACTED}{newline}")
            continue

        is_traceback = stripped == "Traceback (most recent call last):"
        is_stack = stripped == "Stack (most recent call last):"
        is_frame = _FRAME_LINE_RE.fullmatch(line) is not None
        is_chain = _CHAIN_LINE_RE.fullmatch(stripped) is not None
        if not (is_traceback or is_stack or is_frame or is_chain):
            output.append(raw_line)
            index += 1
            continue

        start = index
        expects_terminal = is_traceback or is_frame or is_chain
        if is_traceback or is_stack or is_chain:
            index += 1
        if is_chain:
            while index < len(lines) and not lines[index].strip():
                index += 1
            if index < len(lines) and lines[index].strip() == (
                "Traceback (most recent call last):"
            ):
                index += 1
        saw_frame = is_frame
        if is_frame:
            index += 1

        while index < len(lines):
            candidate_raw = lines[index]
            candidate = candidate_raw.rstrip("\r\n")
            candidate_stripped = candidate.strip()
            if _FRAME_LINE_RE.fullmatch(candidate):
                saw_frame = True
                index += 1
                continue
            if saw_frame and candidate != candidate.lstrip():
                # Source/caret/continuation lines are forbidden string data.
                index += 1
                continue
            if not candidate_stripped:
                if expects_terminal and saw_frame:
                    index += 1
                    continue
                break
            if expects_terminal and not saw_frame:
                # A recognized traceback header is enough to fail closed.
                # Even malformed/truncated serializations can carry an opaque
                # terminal exception and continuation lines without a frame.
                while index < len(lines) and lines[index].strip():
                    index += 1
                break
            if expects_terminal and saw_frame:
                # The first unindented line begins the exception rendering.
                # Multiline exception messages continue on subsequent
                # unindented lines, so fail closed through a blank separator
                # or end-of-input rather than preserving opaque continuations.
                index += 1
                while index < len(lines) and lines[index].strip():
                    index += 1
                lookahead = index
                while lookahead < len(lines) and not lines[lookahead].strip():
                    lookahead += 1
                if lookahead < len(lines) and _CHAIN_LINE_RE.fullmatch(
                    lines[lookahead].strip()
                ):
                    index = lookahead + 1
                    while index < len(lines) and not lines[index].strip():
                        index += 1
                    if index < len(lines) and lines[index].strip() == (
                        "Traceback (most recent call last):"
                    ):
                        index += 1
                        saw_frame = False
                        expects_terminal = True
                        continue
                break
            break

        if index == start:
            index += 1
        consumed = "".join(lines[start:index])
        newline = "\n" if consumed.endswith(("\n", "\r")) else ""
        output.append(f"{_TRACEBACK_REDACTED}{newline}")
    return "".join(output)


def _replace_json_assignment(match: re.Match[str]) -> str:
    if not _is_sensitive_key(match.group("key")):
        return match.group(0)
    value = match.group("value")
    if _is_safe_marker(value):
        return match.group(0)
    return f"{match.group('prefix')}{REDACTED}{match.group('suffix')}"


def _replace_assignment(match: re.Match[str]) -> str:
    if not _is_sensitive_key(match.group("key")):
        return match.group(0)
    value = match.group("value")
    if _is_safe_marker(value):
        return match.group(0)
    quote = match.group("quote")
    suffix = match.group("suffix") if quote else ""
    return f"{match.group('prefix')}{quote}{REDACTED}{suffix}"


def _replace_quoted_assignment(match: re.Match[str]) -> str:
    if not _is_sensitive_key(match.group("key")):
        return match.group(0)
    value = match.group("value")
    if _is_safe_marker(value):
        return match.group(0)
    return (
        f"{match.group('prefix')}{match.group('quote')}"
        f"{REDACTED}{match.group('suffix')}"
    )


def _replace_unclosed_assignment(match: re.Match[str]) -> str:
    if not _is_sensitive_key(match.group("key")):
        return match.group(0)
    quote = match.group("quote")
    return f"{match.group('prefix')}{REDACTED}{quote}"


def _replace_header(match: re.Match[str]) -> str:
    if _is_safe_marker(match.group("value")):
        return match.group(0)
    return f"{match.group('prefix')}{REDACTED}"


def _replace_bearer(match: re.Match[str]) -> str:
    if _is_safe_marker(match.group("value")):
        return match.group(0)
    return f"{match.group('leading')}{match.group('scheme')} {REDACTED}"


def _redact_raw_parameters(value: str) -> str:
    if not value:
        return value
    parts: list[str] = []
    for item in value.split("&"):
        key, separator, parameter_value = item.partition("=")
        normalized = _normalize_key(unquote_plus(key))
        if separator and normalized in _SENSITIVE_URL_KEYS:
            if _is_safe_marker(unquote_plus(parameter_value)):
                parts.append(item)
            else:
                parts.append(f"{key}={REDACTED}")
        else:
            parts.append(item)
    return "&".join(parts)


def _redact_malformed_url(value: str) -> str:
    """Fail closed for a recognized URL that ``urlsplit`` rejects."""
    scheme_end = value.find("://")
    authority_start = scheme_end + 3 if scheme_end >= 0 else 0
    at = value.rfind("@")
    if at >= authority_start:
        value = f"{value[:authority_start]}{REDACTED}{value[at:]}"

    without_fragment, fragment_separator, fragment = value.partition("#")
    without_query, query_separator, query = without_fragment.partition("?")
    if query_separator:
        without_query = f"{without_query}?{_redact_raw_parameters(query)}"
    if fragment_separator:
        without_query = f"{without_query}#{_redact_raw_parameters(fragment)}"
    return without_query


def _redact_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    trailing = ""
    while raw and raw[-1] in ".,;)":
        trailing = raw[-1] + trailing
        raw = raw[:-1]
    try:
        parts = urlsplit(raw)
    except ValueError:
        return _redact_malformed_url(raw) + trailing

    netloc = parts.netloc
    if "@" in netloc:
        _userinfo, host = netloc.rsplit("@", 1)
        netloc = f"{REDACTED}@{host}"

    path = parts.path
    try:
        hostname = (parts.hostname or "").lower()
    except ValueError:
        hostname = ""
    if hostname == "hooks.slack.com" or hostname.endswith(".hooks.slack.com"):
        path = re.sub(
            r"(?i)^/services/[^/]+/[^/]+/[^/]+",
            f"/services/{REDACTED}",
            path,
        )
    if hostname in {"discord.com", "discordapp.com"} or hostname.endswith(
        (".discord.com", ".discordapp.com")
    ):
        path = re.sub(
            r"(?i)^/api/webhooks/([^/]+)/[^/]+",
            rf"/api/webhooks/\1/{REDACTED}",
            path,
        )
    if hostname == "api.telegram.org":
        path = re.sub(r"(?i)^/bot[^/]+", f"/bot{REDACTED}", path)

    query = _redact_raw_parameters(parts.query)
    fragment = _redact_raw_parameters(parts.fragment)
    return urlunsplit((parts.scheme, netloc, path, query, fragment)) + trailing


def _redact_text_with_secrets(
    text: str,
    *,
    secrets: tuple[str, ...],
    max_chars: int,
    one_line: bool,
) -> str:
    bounded_max = _text_limit(max_chars, default=DEFAULT_ERROR_TEXT_MAX_CHARS)
    input_was_truncated = len(text) > MAX_REDACTION_INPUT_CHARS
    value = text[:MAX_REDACTION_INPUT_CHARS]
    value = _redact_traceback_strings(value)
    value = _replace_known_secrets(value, secrets)
    value = _PEM_PRIVATE_KEY_RE.sub(REDACTED, value)
    for pattern in _JSON_ASSIGNMENT_RES:
        value = pattern.sub(_replace_json_assignment, value)
    for pattern in _QUOTED_ASSIGNMENT_RES:
        value = pattern.sub(_replace_quoted_assignment, value)
    for pattern in _UNCLOSED_JSON_ASSIGNMENT_RES:
        value = pattern.sub(_replace_unclosed_assignment, value)
    for pattern in _UNCLOSED_ASSIGNMENT_RES:
        value = pattern.sub(_replace_unclosed_assignment, value)
    value = _HEADER_RE.sub(_replace_header, value)
    value = _ASSIGNMENT_RE.sub(_replace_assignment, value)
    for pattern in _CREDENTIAL_PATTERNS:
        value = pattern.sub(REDACTED, value)
    value = _JWT_CANDIDATE_RE.sub(REDACTED, value)
    value = _URL_RE.sub(_redact_url, value)
    value = _BEARER_RE.sub(_replace_bearer, value)
    if one_line:
        value = _CONTROL_OR_WHITESPACE_RE.sub(" ", value).strip()
    if input_was_truncated:
        value = f"{value}…"
    return _truncate(value, bounded_max)


def redact_text(
    text: str,
    *,
    secret_values: Iterable[str | None] = (),
    max_chars: int = DEFAULT_ERROR_TEXT_MAX_CHARS,
    one_line: bool = True,
) -> str:
    secrets = _materialize_secret_values(secret_values)
    if type(text) is not str:
        text = _safe_unknown_string(text)
    return _redact_text_with_secrets(
        text,
        secrets=secrets,
        max_chars=max_chars,
        one_line=one_line,
    )


def _safe_unknown_string(value: object) -> str:
    try:
        return str(value)
    except Exception:  # a buggy __str__ must not break error reporting
        return _unprintable_marker(value)


def redact_value(
    value: Any,
    *,
    secret_values: Iterable[str | None] = (),
    max_string_chars: int = DEFAULT_LOG_STRING_MAX_CHARS,
    max_depth: int = 12,
) -> Any:
    secrets = _materialize_secret_values(secret_values)
    string_limit = _bounded_int(
        max_string_chars,
        default=DEFAULT_LOG_STRING_MAX_CHARS,
        lower=0,
        upper=MAX_REDACTION_INPUT_CHARS,
    )
    depth_limit = _bounded_int(max_depth, default=12, lower=0, upper=64)
    seen: set[int] = set()
    remaining_items = _MAX_REDACTION_ITEMS

    def take_item() -> bool:
        nonlocal remaining_items
        if remaining_items <= 0:
            return False
        remaining_items -= 1
        return True

    def visit(item: Any, depth: int) -> Any:
        if depth > depth_limit:
            return TRUNCATED
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, str):
            return _redact_text_with_secrets(
                item,
                secrets=secrets,
                max_chars=string_limit,
                one_line=False,
            )
        if isinstance(item, (int, float)):
            return item
        if isinstance(item, (bytes, bytearray, memoryview)):
            return f"[BYTES {len(item)}]"
        if isinstance(item, complex):
            return _redact_text_with_secrets(
                str(item),
                secrets=secrets,
                max_chars=string_limit,
                one_line=False,
            )
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen:
                return CYCLE
            seen.add(identity)
            output: dict[str, Any] = {}
            for raw_key, child in item.items():
                if not take_item():
                    output[TRUNCATED] = TRUNCATED
                    break
                key = _safe_unknown_string(raw_key)
                safe_key = _redact_text_with_secrets(
                    key,
                    secrets=secrets,
                    max_chars=string_limit,
                    one_line=True,
                )
                output[safe_key] = (
                    REDACTED if _is_sensitive_key(key) else visit(child, depth + 1)
                )
            return output
        if isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in seen:
                return CYCLE
            seen.add(identity)
            children: list[Any] = []
            for child in item:
                if not take_item():
                    children.append(TRUNCATED)
                    break
                children.append(visit(child, depth + 1))
            return children
        if isinstance(item, (set, frozenset)):
            identity = id(item)
            if identity in seen:
                return CYCLE
            seen.add(identity)
            children = []
            for child in item:
                if not take_item():
                    children.append(TRUNCATED)
                    break
                children.append(visit(child, depth + 1))
            return sorted(
                children, key=lambda child: (type(child).__name__, str(child))
            )
        return _redact_text_with_secrets(
            _safe_unknown_string(item),
            secrets=secrets,
            max_chars=string_limit,
            one_line=False,
        )

    return visit(value, 0)


def _safe_frame_path(filename: str) -> tuple[str | None, str]:
    normalized = filename.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    lowered = [part.lower() for part in parts]
    safe_parts: list[str]
    module: str | None
    try:
        src_index = next(
            index
            for index in range(len(lowered) - 1)
            if lowered[index] == "src" and lowered[index + 1] == "frisket"
        )
    except StopIteration:
        src_index = -1
    if src_index >= 0:
        safe_parts = parts[src_index + 1 :]
        module_parts = safe_parts[:]
        module = ".".join(module_parts).removesuffix(".py").replace("/", ".")
    elif "tests" in lowered:
        tests_index = lowered.index("tests")
        safe_parts = parts[tests_index:]
        module = ".".join(safe_parts).removesuffix(".py").replace("/", ".")
    else:
        safe_parts = parts[-1:] or ["<unknown>"]
        module = None
    return module, "/".join(safe_parts)


def safe_stack_frames(
    tb: TracebackType | None,
    *,
    max_frames: int = MAX_SAFE_FRAMES,
) -> tuple[SafeFrame, ...]:
    limit = _bounded_int(
        max_frames, default=MAX_SAFE_FRAMES, lower=0, upper=MAX_SAFE_FRAMES
    )
    if tb is None or limit == 0:
        return ()
    try:
        summaries = traceback.extract_tb(tb, limit=-limit)
    except Exception:  # noqa: BLE001 - diagnostics cannot mask the original error
        return ()
    frames: list[SafeFrame] = []
    for summary in summaries:
        module, path = _safe_frame_path(summary.filename)
        safe_module = (
            redact_text(module, max_chars=512, one_line=True) if module else None
        )
        safe_path = redact_text(path, max_chars=512, one_line=True)
        function = redact_text(summary.name, max_chars=256, one_line=True)
        line = summary.lineno if type(summary.lineno) is int else 1
        frames.append(
            SafeFrame(
                module=safe_module,
                path=safe_path or "<unknown>",
                function=function or "<unknown>",
                line=max(line, 1),
            )
        )
    return tuple(frames)


def safe_error(
    code: str,
    value: BaseException | str,
    *,
    secret_values: Iterable[str | None] = (),
    max_chars: int = DEFAULT_ERROR_TEXT_MAX_CHARS,
    fallback_detail: str = "operation failed",
    include_frames: bool = False,
    max_frames: int = MAX_SAFE_FRAMES,
) -> SafeError:
    safe_code = canonical_error_code(code)
    total_limit = _text_limit(max_chars, default=DEFAULT_ERROR_TEXT_MAX_CHARS)
    secrets = _materialize_secret_values(secret_values)
    safe_fallback_detail = _safe_unknown_string(fallback_detail)
    exception_type: str | None = None
    frames: tuple[SafeFrame, ...] = ()
    if isinstance(value, BaseException):
        exception_type = _redact_text_with_secrets(
            type(value).__name__, secrets=secrets, max_chars=256, one_line=True
        )
        try:
            raw_detail = str(value)
        except Exception:  # a buggy __str__ must not mask the original failure
            raw_detail = safe_fallback_detail
        if include_frames:
            frames = safe_stack_frames(value.__traceback__, max_frames=max_frames)
    else:
        raw_detail = _safe_unknown_string(value)
    if total_limit < 3:
        detail = _redact_text_with_secrets(
            raw_detail or safe_fallback_detail,
            secrets=secrets,
            max_chars=total_limit,
            one_line=True,
        )
        return SafeError(
            code="",
            detail=detail,
            exception_type=exception_type or None,
            frames=frames,
        )
    if len(safe_code) + 2 > total_limit:
        safe_code = "e"
    detail_limit = max(total_limit - len(safe_code) - 2, 0)
    detail = _redact_text_with_secrets(
        raw_detail,
        secrets=secrets,
        max_chars=detail_limit,
        one_line=True,
    )
    if not detail:
        detail = _redact_text_with_secrets(
            safe_fallback_detail,
            secrets=secrets,
            max_chars=detail_limit,
            one_line=True,
        )
    return SafeError(
        code=safe_code,
        detail=detail,
        exception_type=exception_type or None,
        frames=frames,
    )


def redact_stored_error(
    value: object | None,
    *,
    fallback_code: str,
    max_chars: int = DEFAULT_ERROR_TEXT_MAX_CHARS,
) -> str | None:
    if value is None:
        return None
    raw = _safe_unknown_string(value)
    match = _STORED_ERROR_RE.fullmatch(raw)
    if match and redact_text(match.group(1)) == match.group(1):
        code = match.group(1)
        detail = match.group(2)
    else:
        code = canonical_error_code(fallback_code)
        detail = raw
    return safe_error(code, detail, max_chars=max_chars).text


__all__ = [
    "CYCLE",
    "DEFAULT_ERROR_TEXT_MAX_CHARS",
    "DEFAULT_LOG_STRING_MAX_CHARS",
    "DEFAULT_TRACE_STRING_MAX_CHARS",
    "MAX_REDACTION_INPUT_CHARS",
    "MAX_SAFE_FRAMES",
    "REDACTED",
    "TRUNCATED",
    "SafeError",
    "SafeFrame",
    "canonical_error_code",
    "redact_stored_error",
    "redact_text",
    "redact_value",
    "safe_error",
    "safe_stack_frames",
]
