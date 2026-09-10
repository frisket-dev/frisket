from __future__ import annotations

import mailbox
from collections.abc import Mapping
from email.message import EmailMessage
from email.mime.message import MIMEMessage
from email.parser import BytesParser
from email.policy import default
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest


def _email_import() -> Any:
    return import_module("frisket.ops.email_import")


def _message_bytes(
    subject: str,
    *,
    plain: str | None = None,
    html: str | None = None,
    attachment: tuple[bytes, str, str] | None = None,
    forwarded: EmailMessage | None = None,
) -> bytes:
    message = EmailMessage()
    message["Date"] = "Tue, 02 Sep 2026 12:34:56 +0000"
    message["From"] = "Alice Reporter <alice@example.test>"
    message["To"] = "Bob Editor <bob@example.test>, desk@example.test"
    message["Cc"] = "Archive <archive@example.test>"
    message["Subject"] = subject
    if plain is not None:
        message.set_content(plain)
        if html is not None:
            message.add_alternative(html, subtype="html")
    elif html is not None:
        message.set_content(html, subtype="html")
    if attachment is not None:
        payload, mime, filename = attachment
        maintype, subtype = mime.split("/", 1)
        message.add_attachment(
            payload,
            maintype=maintype,
            subtype=subtype,
            filename=filename,
        )
    if forwarded is not None:
        message.add_attachment(forwarded, filename="forwarded.eml")
    return message.as_bytes()


def _write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _write_mbox(path: Path, messages: list[bytes]) -> Path:
    box = mailbox.mbox(path, create=True)
    try:
        for payload in messages:
            parsed = BytesParser(policy=default).parsebytes(payload)
            box.add(mailbox.mboxMessage(parsed))
        box.flush()
    finally:
        box.close()
    return path


def _message_value(message: Any, field: str) -> Any:
    """Read only the normalized public values, independent of record style."""
    if isinstance(message, Mapping):
        return message[field]
    row = getattr(message, "row", None)
    if isinstance(row, Mapping) and field in row:
        return row[field]
    return getattr(message, "from_" if field == "from" else field)


def _attachment_value(attachment: Any, field: str) -> Any:
    if isinstance(attachment, Mapping):
        return attachment[field]
    return getattr(attachment, field)


def _source(module: Any, path: Path, logical_path: str, format: str) -> Any:
    return module.EmailSource(
        path=path,
        logical_path=logical_path,
        format=format,
    )


def test_eml_sources_are_path_sorted_and_preserve_inert_body_and_html(
    tmp_path: Path,
) -> None:
    module = _email_import()
    last = _write(
        tmp_path / "last.eml",
        _message_bytes("Last", plain="plain last"),
    )
    first = _write(
        tmp_path / "first.eml",
        _message_bytes(
            "First",
            html=(
                "<p>Hello <strong>world</strong>.</p>"
                '<img src="https://tracker.invalid/pixel">'
            ),
        ),
    )
    middle = _write(
        tmp_path / "middle.eml",
        _message_bytes(
            "Middle",
            plain="multipart plain",
            html="<p>multipart <em>html</em></p>",
        ),
    )

    result = module.parse_email_sources(
        [
            _source(module, last, "z/last.eml", "eml"),
            _source(module, middle, "m/middle.eml", "eml"),
            _source(module, first, "a/first.eml", "eml"),
        ]
    )

    assert [_message_value(item, "source_file") for item in result.messages] == [
        "a/first.eml",
        "m/middle.eml",
        "z/last.eml",
    ]
    assert [_message_value(item, "subject") for item in result.messages] == [
        "First",
        "Middle",
        "Last",
    ]
    html_only = result.messages[0]
    assert " ".join(_message_value(html_only, "body").split()) == "Hello world."
    assert _message_value(html_only, "html") == (
        '<p>Hello <strong>world</strong>.</p><img src="https://tracker.invalid/pixel">'
    )
    assert "https://tracker.invalid" not in _message_value(html_only, "body")

    multipart = result.messages[1]
    assert _message_value(multipart, "body").strip() == "multipart plain"
    assert _message_value(multipart, "html").strip() == (
        "<p>multipart <em>html</em></p>"
    )
    assert _message_value(result.messages[2], "body").strip() == "plain last"
    assert _message_value(result.messages[2], "html") == ""
    assert result.warnings == []


def test_encoded_headers_charset_addresses_and_mbox_locations(tmp_path: Path) -> None:
    module = _email_import()
    encoded = (
        b"Date: Tue, 02 Sep 2026 12:34:56 +0000\r\n"
        b"From: =?iso-8859-1?q?Jos=E9?= <jose@example.test>\r\n"
        b"To: Uno <one@example.test>, two@example.test\r\n"
        b"Cc: =?utf-8?q?R=C3=A9daction?= <desk@example.test>\r\n"
        b"Subject: =?iso-8859-1?q?Ol=E1_mundo?=\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: text/plain; charset=iso-8859-1\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"caf\xe9\r\n"
    )
    path = _write_mbox(
        tmp_path / "mailbox.mbox",
        [encoded, _message_bytes("Second", plain="second body")],
    )

    result = module.parse_email_sources(
        [_source(module, path, "exports/mailbox.mbox", "mbox")]
    )

    assert [_message_value(item, "source_file") for item in result.messages] == [
        "exports/mailbox.mbox#1",
        "exports/mailbox.mbox#2",
    ]
    first = result.messages[0]
    assert _message_value(first, "date") == "Tue, 02 Sep 2026 12:34:56 +0000"
    assert _message_value(first, "from") == "José <jose@example.test>"
    assert _message_value(first, "to") == [
        "Uno <one@example.test>",
        "two@example.test",
    ]
    assert _message_value(first, "cc") == ["Rédaction <desk@example.test>"]
    assert _message_value(first, "subject") == "Olá mundo"
    assert _message_value(first, "body").strip() == "café"


def test_parser_stages_attachment_as_a_path_descriptor(tmp_path: Path) -> None:
    module = _email_import()
    payload = b"%PDF-1.4\ninvoice bytes\n"
    forwarded = EmailMessage()
    forwarded["Subject"] = "Forwarded evidence"
    forwarded.set_content("Forwarded body")
    path = _write(
        tmp_path / "attached.eml",
        _message_bytes(
            "Invoice",
            plain="See attached.",
            attachment=(payload, "application/pdf", "invoice.pdf"),
            forwarded=forwarded,
        ),
    )

    result = module.parse_email_sources(
        [_source(module, path, "inbox/attached.eml", "eml")]
    )

    parsed = result.messages[0]
    assert _message_value(parsed, "body").strip() == "See attached."
    attachments = _message_value(parsed, "attachments")
    assert len(attachments) == 2
    attachment = attachments[0]
    staged_path = Path(_attachment_value(attachment, "path"))
    assert staged_path.is_file()
    assert staged_path.read_bytes() == payload
    assert _attachment_value(attachment, "mime") == "application/pdf"
    assert _attachment_value(attachment, "filename") == "invoice.pdf"

    forwarded_attachment = attachments[1]
    assert _attachment_value(forwarded_attachment, "mime") == "message/rfc822"
    assert _attachment_value(forwarded_attachment, "filename") == "forwarded.eml"
    forwarded_path = Path(_attachment_value(forwarded_attachment, "path"))
    assert forwarded_path.is_file()
    reparsed = BytesParser(policy=default).parsebytes(forwarded_path.read_bytes())
    assert reparsed["Subject"] == "Forwarded evidence"
    assert reparsed.get_content().strip() == "Forwarded body"


def test_malformed_siblings_are_skipped_with_bounded_named_warnings(
    tmp_path: Path,
) -> None:
    module = _email_import()
    sources = []
    for index in range(40):
        path = _write(tmp_path / f"bad-{index:03d}.eml", b"\x00\xffnot an email")
        sources.append(_source(module, path, path.name, "eml"))
    good = _write(
        tmp_path / "good.eml",
        _message_bytes("Kept", plain="valid sibling"),
    )
    sources.append(_source(module, good, good.name, "eml"))

    result = module.parse_email_sources(list(reversed(sources)))

    assert [_message_value(item, "subject") for item in result.messages] == ["Kept"]
    assert 1 <= len(result.warnings) <= 20
    summary = " ".join(str(warning) for warning in result.warnings)
    assert "bad-000.eml" in summary
    assert "40" in summary or len(result.warnings) < 40


def test_all_invalid_sources_fail_instead_of_publishing_an_empty_import(
    tmp_path: Path,
) -> None:
    module = _email_import()
    bad = _write(tmp_path / "only-bad.eml", b"\x00\xffnot an email")

    with pytest.raises(ValueError, match=r"(?i)(valid|email|message)"):
        module.parse_email_sources([_source(module, bad, "only-bad.eml", "eml")])


def test_nested_rfc822_without_filename_is_staged_and_never_becomes_body(
    tmp_path: Path,
) -> None:
    module = _email_import()
    forwarded = EmailMessage()
    forwarded["From"] = "source@example.test"
    forwarded["Subject"] = "Forwarded evidence"
    forwarded.set_content("inner body must not leak")
    outer = EmailMessage()
    outer["From"] = "reporter@example.test"
    outer["Subject"] = "Outer message"
    outer.set_content("outer body")
    outer.make_mixed()
    outer.attach(MIMEMessage(forwarded))
    source = _write(tmp_path / "forwarded.eml", outer.as_bytes())

    with module.parse_email_sources(
        [_source(module, source, "forwarded.eml", "eml")]
    ) as result:
        parsed = result.messages[0]
        assert _message_value(parsed, "body").strip() == "outer body"
        assert "inner body" not in _message_value(parsed, "body")
        attachment = _message_value(parsed, "attachments")[0]
        staged_path = Path(_attachment_value(attachment, "path"))
        assert _attachment_value(attachment, "mime") == "message/rfc822"
        assert _attachment_value(attachment, "filename") == "forwarded.eml"
        assert staged_path.is_file()

    assert not staged_path.exists()


def test_native_mbox_framing_preserves_indices_and_unescapes_one_quote(
    tmp_path: Path,
) -> None:
    module = _email_import()
    path = _write(
        tmp_path / "edges.mbox",
        b"ignored preamble\n"
        b"From first separator\n"
        b"From second separator\n"
        b"From: sender@example.test\n"
        b"Subject: Kept\n\n"
        b">>From quoted body\n",
    )
    warnings = module.EmailParseWarnings()

    messages = list(
        module.iter_email_messages(
            [_source(module, path, "edges.mbox", "mbox")],
            attachment_dir=tmp_path / "staged",
            warnings=warnings,
        )
    )

    assert [_message_value(message, "source_file") for message in messages] == [
        "edges.mbox#2"
    ]
    assert _message_value(messages[0], "body").strip() == ">From quoted body"
    assert len(warnings.values()) == 1


def test_separator_free_mbox_and_unknown_charset_are_handled_safely(
    tmp_path: Path,
) -> None:
    module = _email_import()
    mislabeled = _write(tmp_path / "mislabeled.mbox", b"x" * (3 * 1024 * 1024))
    warnings = module.EmailParseWarnings()
    assert (
        list(
            module.iter_email_messages(
                [_source(module, mislabeled, "mislabeled.mbox", "mbox")],
                attachment_dir=tmp_path / "mislabeled-staged",
                warnings=warnings,
            )
        )
        == []
    )
    assert warnings.values() == ["mislabeled.mbox: mailbox contains no messages"]

    unknown = _write(
        tmp_path / "unknown.eml",
        b"From: sender@example.test\r\n"
        b"Subject: Unknown charset\r\n"
        b"Content-Type: text/plain; charset=x-not-real\r\n\r\n"
        b"caf\xc3\xa9\r\n",
    )
    message = next(
        module.iter_email_messages(
            [_source(module, unknown, "unknown.eml", "eml")],
            attachment_dir=tmp_path / "unknown-staged",
        )
    )
    assert _message_value(message, "body").strip() == "café"

    nul = _write(
        tmp_path / "nul.eml",
        b"From: sender@example.test\r\nSubject: NUL\r\n\r\ninvalid\x00body",
    )
    warnings = module.EmailParseWarnings()
    assert (
        list(
            module.iter_email_messages(
                [_source(module, nul, "nul.eml", "eml")],
                attachment_dir=tmp_path / "nul-staged",
                warnings=warnings,
            )
        )
        == []
    )
    assert "NUL" in warnings.values()[0]


def test_unknown_encoded_header_and_filename_charsets_remain_importable(
    tmp_path: Path,
) -> None:
    module = _email_import()
    source = _write(
        tmp_path / "unknown-encoded-charset.eml",
        b"From: sender@example.test\r\n"
        b"Subject: =?x-not-real?b?Y2Fmw6k=?=\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=boundary\r\n\r\n"
        b"--boundary\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"body\r\n"
        b"--boundary\r\n"
        b"Content-Type: application/pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n"
        b"Content-Disposition: attachment; "
        b'filename="=?x-not-real?b?Y2Fmw6kucGRm?="\r\n\r\n'
        b"cGRm\r\n"
        b"--boundary--\r\n",
    )

    with module.parse_email_sources(
        [_source(module, source, source.name, "eml")]
    ) as result:
        assert len(result.messages) == 1
        parsed = result.messages[0]
        assert _message_value(parsed, "subject") == "caf\u00e9"
        attachment = _message_value(parsed, "attachments")[0]
        assert _attachment_value(attachment, "filename") == "caf\u00e9.pdf"
        assert Path(_attachment_value(attachment, "path")).read_bytes() == b"pdf"


def test_failed_late_attachment_normalization_removes_message_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _email_import()
    source = _write(
        tmp_path / "late-normalization-failure.eml",
        _message_bytes(
            "Two attachments",
            plain="body",
            attachment=(b"first", "application/octet-stream", "first.bin"),
        ),
    )
    parsed = BytesParser(policy=default).parsebytes(source.read_bytes())
    parsed.add_attachment(
        b"second",
        maintype="application",
        subtype="octet-stream",
        filename="second.bin",
    )
    source.write_bytes(parsed.as_bytes())
    original = module._decoded_filename
    calls = 0

    def fail_after_staging(part: Any) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("late filename normalization failed")
        return original(part)

    monkeypatch.setattr(module, "_decoded_filename", fail_after_staging)
    scratch = tmp_path / "scratch"
    warnings = module.EmailParseWarnings()

    assert (
        list(
            module.iter_email_messages(
                [_source(module, source, source.name, "eml")],
                attachment_dir=scratch,
                warnings=warnings,
            )
        )
        == []
    )
    assert list(scratch.iterdir()) == []
    assert warnings.values() == [
        "late-normalization-failure.eml: late filename normalization failed"
    ]


def test_streaming_requires_scratch_before_sources_are_opened(tmp_path: Path) -> None:
    module = _email_import()
    source = _write(
        tmp_path / "valid.eml",
        _message_bytes("Valid", plain="must not be parsed"),
    )
    warnings = module.EmailParseWarnings()

    with pytest.raises(ValueError, match="attachment_dir"):
        module.iter_email_messages(
            [_source(module, source, "valid.eml", "eml")],
            warnings=warnings,
        )

    assert warnings.values() == []
