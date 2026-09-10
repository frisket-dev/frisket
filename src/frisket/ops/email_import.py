"""Streaming, deterministic parsing for staged EML and MBOX sources."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.policy import default
from html.parser import HTMLParser
from pathlib import Path
from typing import BinaryIO, Literal, TypeVar


_MAX_WARNINGS = 20
_MESSAGE_HEADERS = frozenset({"date", "from", "to", "cc", "subject", "message-id"})


@dataclass(frozen=True, slots=True)
class EmailSource:
    """One staged email source and its stable user-facing path."""

    path: Path | None
    logical_path: str
    format: Literal["eml", "mbox"]
    # Trusted ingress may pin an already-verified inode for the duration of an
    # import. The parser borrows this seekable stream and never closes it.
    stream: BinaryIO | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class EmailAttachment:
    """A decoded attachment staged outside of parser memory."""

    path: Path
    mime: str
    filename: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ParsedEmail:
    date: str
    from_: str
    to: list[str]
    cc: list[str]
    subject: str
    body: str
    html: str
    attachments: list[EmailAttachment]
    source_file: str

    @property
    def row(self) -> dict[str, object]:
        """Return the normalized public row shape used by import adapters."""

        return {
            "date": self.date,
            "from": self.from_,
            "to": self.to,
            "cc": self.cc,
            "subject": self.subject,
            "body": self.body,
            "html": self.html,
            "attachments": self.attachments,
            "source_file": self.source_file,
        }


@dataclass(frozen=True, slots=True)
class EmailParseResult:
    messages: list[ParsedEmail]
    warnings: list[str]
    _temporary_directory: tempfile.TemporaryDirectory[str] | None = field(
        default=None, repr=False, compare=False
    )

    def close(self) -> None:
        """Remove attachment scratch space owned by this eager result."""

        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()

    def __enter__(self) -> EmailParseResult:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class EmailParseWarnings:
    """Retain useful examples while keeping diagnostics bounded."""

    def __init__(self) -> None:
        self._shown: list[str] = []
        self.total = 0

    def add(self, warning: str) -> None:
        self.total += 1
        if len(self._shown) < _MAX_WARNINGS - 1:
            self._shown.append(warning)

    def values(self) -> list[str]:
        if self.total <= len(self._shown):
            return list(self._shown)
        return [
            *self._shown,
            f"{self.total} invalid email messages total; "
            f"{self.total - len(self._shown)} additional warnings omitted",
        ]


class _TextExtractor(HTMLParser):
    """Extract visible text without dereferencing any HTML resource."""

    _BLOCKS = frozenset(
        {
            "address",
            "article",
            "aside",
            "blockquote",
            "br",
            "div",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "hr",
            "li",
            "main",
            "nav",
            "p",
            "pre",
            "section",
            "table",
            "tr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style"}:
            self._ignored_depth += 1
        elif tag in self._BLOCKS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._ignored_depth:
            self._ignored_depth -= 1
        elif tag in self._BLOCKS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self._chunks.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self._chunks).splitlines()]
        return "\n".join(line for line in lines if line)


class _MboxStream:
    """One-pass mbox reader that never builds a mailbox-wide table of contents."""

    _READ_CHUNK_BYTES = 64 * 1024

    def __init__(self, source: str | Path | BinaryIO) -> None:
        if hasattr(source, "read"):
            self._stream = source
            self._owns_stream = False
            self._stream.seek(0)
        else:
            self._stream = Path(source).open("rb")
            self._owns_stream = True

    def __iter__(self) -> Iterator[bytes]:
        payload = bytearray()
        saw_separator = False
        at_line_start = True
        discarding_separator = False
        pending_greater_than = 0
        while chunk := self._stream.readline(self._READ_CHUNK_BYTES):
            ends_line = chunk.endswith(b"\n")
            if discarding_separator:
                if ends_line:
                    discarding_separator = False
                    at_line_start = True
                continue

            if at_line_start and chunk.startswith(b"From "):
                if saw_separator:
                    yield bytes(payload)
                    payload.clear()
                saw_separator = True
                discarding_separator = not ends_line
                at_line_start = ends_line
                continue

            if at_line_start and chunk.startswith(b">"):
                leading = len(chunk) - len(chunk.lstrip(b">"))
                pending_greater_than += leading
                if leading == len(chunk):
                    # The leading quote run crosses the bounded read chunk.
                    at_line_start = False
                    continue
                remainder = chunk[leading:]
                if saw_separator:
                    quote_count = pending_greater_than
                    if remainder.startswith(b"From "):
                        quote_count -= 1
                    payload.extend(b">" * quote_count)
                    payload.extend(remainder)
                pending_greater_than = 0
                at_line_start = ends_line
                continue

            if pending_greater_than:
                leading = len(chunk) - len(chunk.lstrip(b">"))
                pending_greater_than += leading
                if leading == len(chunk):
                    continue
                remainder = chunk[leading:]
                if saw_separator:
                    quote_count = pending_greater_than
                    if remainder.startswith(b"From "):
                        quote_count -= 1
                    payload.extend(b">" * quote_count)
                    payload.extend(remainder)
                pending_greater_than = 0
                at_line_start = ends_line
                continue

            if saw_separator:
                payload.extend(chunk)
            # Preambles and separator-free mislabeled files are discarded in
            # bounded chunks rather than accumulated as a candidate message.
            at_line_start = ends_line
        if pending_greater_than and saw_separator:
            payload.extend(b">" * pending_greater_than)
        if saw_separator:
            yield bytes(payload)

    def close(self) -> None:
        if self._owns_stream:
            self._stream.close()


def _open_mbox(
    source: str | Path | BinaryIO, *_args: object, **_kwargs: object
) -> _MboxStream:
    return _MboxStream(source)


def _read_eml(source: EmailSource) -> bytes:
    if source.stream is not None:
        source.stream.seek(0)
        return source.stream.read()
    if source.path is None:
        raise OSError("email source is unavailable")
    return source.path.read_bytes()


def iter_email_messages(
    sources: Iterable[EmailSource],
    *,
    open_mbox: Callable[..., Iterable[Message | bytes]] = _open_mbox,
    attachment_dir: Path | None = None,
    warnings: EmailParseWarnings | None = None,
) -> Iterator[ParsedEmail]:
    """Yield normalized messages lazily, with no corpus-wide limits.

    EML sources have stable logical-path ordering. MBOX files are framed and
    consumed sequentially, and each decoded attachment is staged to disk before
    the next message is requested.
    """

    if attachment_dir is None:
        raise ValueError("attachment_dir is required when streaming email messages")
    attachment_dir.mkdir(parents=True, exist_ok=True)
    return _iter_email_messages(
        sources,
        open_mbox=open_mbox,
        attachment_dir=attachment_dir,
        warnings=warnings if warnings is not None else EmailParseWarnings(),
    )


def _iter_email_messages(
    sources: Iterable[EmailSource],
    *,
    open_mbox: Callable[..., Iterable[Message | bytes]],
    attachment_dir: Path,
    warnings: EmailParseWarnings,
) -> Iterator[ParsedEmail]:
    collector = warnings

    for source in sorted(sources, key=lambda item: item.logical_path):
        if source.format == "eml":
            try:
                payload = _read_eml(source)
                yield _parse_message(payload, source.logical_path, attachment_dir)
            except (LookupError, OSError, UnicodeError, ValueError) as exc:
                collector.add(f"{source.logical_path}: {exc}")
            continue
        if source.format != "mbox":
            collector.add(
                f"{source.logical_path}: unsupported email format {source.format!r}"
            )
            continue

        try:
            if source.stream is not None:
                box = _MboxStream(source.stream)
            elif source.path is not None:
                box = open_mbox(source.path, create=False)
            else:
                raise OSError("email source is unavailable")
        except (LookupError, OSError, ValueError) as exc:
            collector.add(f"{source.logical_path}: {exc}")
            continue
        found = False
        try:
            for index, message in enumerate(box, start=1):
                found = True
                location = f"{source.logical_path}#{index}"
                try:
                    yield _parse_message(message, location, attachment_dir)
                except (LookupError, UnicodeError, ValueError) as exc:
                    collector.add(f"{location}: {exc}")
        finally:
            close = getattr(box, "close", None)
            if close is not None:
                close()
        if not found:
            collector.add(f"{source.logical_path}: mailbox contains no messages")


_BatchItem = TypeVar("_BatchItem")


def iter_email_batches(
    messages: Iterable[_BatchItem], *, batch_size: int
) -> Iterator[list[_BatchItem]]:
    """Yield bounded, ordered batches without eagerly consuming the input."""

    if batch_size <= 0:
        raise ValueError("batch_size must be greater than zero")
    batch: list[_BatchItem] = []
    for message in messages:
        batch.append(message)
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def parse_email_sources(
    sources: Iterable[EmailSource], *, attachment_dir: Path | None = None
) -> EmailParseResult:
    """Compatibility wrapper for callers that intentionally materialize results."""

    warnings = EmailParseWarnings()
    owned_scratch: tempfile.TemporaryDirectory[str] | None = None
    if attachment_dir is None:
        owned_scratch = tempfile.TemporaryDirectory(prefix="frisket-email-attachments-")
        attachment_dir = Path(owned_scratch.name)
    try:
        messages = list(
            iter_email_messages(
                sources,
                attachment_dir=attachment_dir,
                warnings=warnings,
            )
        )
        if not messages:
            raise ValueError("no valid email messages were found")
        return EmailParseResult(
            messages=messages,
            warnings=warnings.values(),
            _temporary_directory=owned_scratch,
        )
    except BaseException:
        if owned_scratch is not None:
            owned_scratch.cleanup()
        raise


def _parse_message(
    message: Message | bytes, source_file: str, attachment_dir: Path
) -> ParsedEmail:
    if isinstance(message, bytes):
        if b"\x00" in message:
            raise ValueError("email message contains invalid NUL bytes")
        message = BytesParser(policy=default).parsebytes(message)
    if not any(name.lower() in _MESSAGE_HEADERS for name in message.keys()):
        raise ValueError("email message has no recognizable headers")

    text_parts = {"plain": "", "html": ""}
    attachments: list[EmailAttachment] = []
    try:
        pending: list[Message] = [message]
        while pending:
            part = pending.pop()
            disposition = part.get_content_disposition()
            filename = part.get_filename()
            content_type = part.get_content_type()
            if content_type == "message/rfc822":
                attachments.append(
                    _stage_attachment(
                        part,
                        attachment_dir,
                        fallback_filename="forwarded.eml",
                    )
                )
                continue
            if disposition == "attachment" or filename is not None:
                attachments.append(
                    _stage_attachment(
                        part,
                        attachment_dir,
                        fallback_filename="attachment",
                    )
                )
                continue
            if part.is_multipart():
                children = part.get_payload()
                if isinstance(children, list):
                    pending.extend(
                        reversed(
                            [child for child in children if isinstance(child, Message)]
                        )
                    )
                continue
            if content_type == "text/plain" and not text_parts["plain"]:
                text_parts["plain"] = _text_part(part)
            elif content_type == "text/html" and not text_parts["html"]:
                text_parts["html"] = _text_part(part)

        plain = text_parts["plain"]
        html = text_parts["html"]
        return ParsedEmail(
            date=_header(message, "Date"),
            from_=_header(message, "From"),
            to=_addresses(message, "To"),
            cc=_addresses(message, "Cc"),
            subject=_header(message, "Subject"),
            body=plain if plain else _html_to_text(html),
            html=html,
            attachments=attachments,
            source_file=source_file,
        )
    except (LookupError, OSError, UnicodeError, ValueError):
        for attachment in attachments:
            attachment.path.unlink(missing_ok=True)
        raise


def _stage_attachment(
    part: Message,
    attachment_dir: Path,
    *,
    fallback_filename: str,
) -> EmailAttachment:
    payload = _attachment_bytes(part)
    digest = hashlib.sha256(payload).hexdigest()
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=attachment_dir,
            prefix=f"{digest}-",
            delete=False,
        ) as stream:
            stream.write(payload)
            path = Path(stream.name)
        return EmailAttachment(
            path=path,
            mime=part.get_content_type() or "application/octet-stream",
            filename=_decoded_filename(part) or fallback_filename,
            sha256=digest,
            size=len(payload),
        )
    except (LookupError, OSError, UnicodeError, ValueError):
        if path is not None:
            path.unlink(missing_ok=True)
        raise


def _decoded_filename(part: Message) -> str:
    filename = part.get_filename()
    if not filename:
        return ""
    return _decoded_header(filename)


def _attachment_bytes(part: Message) -> bytes:
    if part.get_content_type() == "message/rfc822":
        payload = part.get_payload()
        if isinstance(payload, list):
            messages = [item for item in payload if isinstance(item, Message)]
            if messages:
                return b"".join(item.as_bytes(policy=default) for item in messages)
        if isinstance(payload, Message):
            return payload.as_bytes(policy=default)
    data = part.get_payload(decode=True)
    if data is not None:
        return data
    content = part.get_payload()
    return content.encode("utf-8") if isinstance(content, str) else b""


def _text_part(part: Message) -> str:
    try:
        content = part.get_content()
    except LookupError:
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")
    except UnicodeError:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    value = content if isinstance(content, str) else str(content)
    return value.rstrip("\r\n")


def _header(message: Message, name: str) -> str:
    for raw_name, raw_value in message.raw_items():
        if raw_name.lower() == name.lower():
            # Keep the supplied Date text stable instead of letting the header
            # registry correct its weekday while still decoding encoded words.
            if name.lower() == "date":
                return _decoded_header(raw_value)
            parsed = message[name]
            if parsed is not None:
                try:
                    return str(parsed)
                except (LookupError, UnicodeError, ValueError):
                    pass
            return _decoded_header(raw_value)
    return ""


def _decoded_header(value: str) -> str:
    """Decode encoded words without allowing an unknown charset to reject mail."""

    try:
        return str(make_header(decode_header(value)))
    except (LookupError, UnicodeError, ValueError):
        decoded: list[str] = []
        for chunk, charset in decode_header(value):
            if isinstance(chunk, str):
                decoded.append(chunk)
                continue
            try:
                decoded.append(chunk.decode(charset or "ascii", errors="replace"))
            except LookupError:
                decoded.append(chunk.decode("utf-8", errors="replace"))
        return "".join(decoded)


def _addresses(message: Message, name: str) -> list[str]:
    result: list[str] = []
    for header in message.get_all(name, []):
        addresses = getattr(header, "addresses", None)
        if addresses is None:
            value = str(header).strip()
            if value:
                result.append(value)
            continue
        result.extend(str(address) for address in addresses)
    return result


def _html_to_text(value: str) -> str:
    if not value:
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text()
