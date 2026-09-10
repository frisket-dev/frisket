from __future__ import annotations

import io
from contextlib import contextmanager
from dataclasses import replace
from email.message import EmailMessage

import pytest

from frisket.actions import import_email as producer
from frisket.actions.types import EmailInput, StagedFile, TableError


class _Sources:
    def __init__(self, entries):
        self.inputs = tuple(
            EmailInput(logical_path=path, format=format, stream=io.BytesIO(data))
            for path, format, data in entries
        )
        self.params = producer.EmailParams(
            sources=[
                {
                    "source_ref": str(index),
                    "logical_path": item.logical_path,
                    "format": item.format,
                }
                for index, item in enumerate(self.inputs)
            ]
        )
        self.entered = False
        self.exited = False

    @contextmanager
    def open(self, refs):
        assert refs == self.params.sources
        self.entered = True
        try:
            yield self.inputs
        finally:
            self.exited = True


class _Blobs:
    def __init__(self):
        self.files = {}

    def stage(self, stream, *, filename, mime, role=None):
        assert role is None
        data = b"".join(iter(lambda: stream.read(64 * 1024), b""))
        handle = StagedFile(size=len(data))
        self.files[handle] = (filename, mime, data)
        return handle


def _message(subject="A message", *, attachments=False):
    message = EmailMessage()
    message["Date"] = "Wed, 02 Sep 2026 12:34:56 +0000"
    message["From"] = "Reporter <reporter@example.test>"
    message["To"] = "Editor <editor@example.test>, desk@example.test"
    message["Cc"] = "Archive <archive@example.test>"
    message["Subject"] = subject
    message.set_content("<p>Visible text</p><script>hidden</script>", subtype="html")
    if attachments:
        message.add_attachment(
            b"same bytes",
            maintype="text",
            subtype="plain",
            filename="café.txt",
            disposition="inline",
        )
        message.add_attachment(
            b"same bytes", maintype="text", subtype="plain", filename="second.txt"
        )
        forwarded = EmailMessage()
        forwarded["Subject"] = "Forwarded, not a second row"
        forwarded.set_content("Forwarded body")
        message.add_attachment(forwarded, filename="forwarded.eml")
    return message.as_bytes()


def test_email_static_row_preserves_headers_html_and_ordered_opaque_attachments():
    sources = _Sources([("cur/message.eml", "eml", _message(attachments=True))])
    blobs = _Blobs()
    table = producer.import_email(sources.params, sources, blobs)
    assert not sources.entered
    (row,) = list(table.rows)
    output = row.output
    assert output.from_ == "Reporter <reporter@example.test>"
    assert output.to == ["Editor <editor@example.test>", "desk@example.test"]
    assert output.cc == ["Archive <archive@example.test>"]
    assert output.date == "Wed, 02 Sep 2026 12:34:56 +0000"
    assert output.body == "Visible text"
    assert "<script>hidden</script>" in output.html
    assert output.source_file == "cur/message.eml"
    assert producer.EmailRow.model_fields["from_"].alias == "from"
    for key in ("body", "html"):
        assert producer.EmailRow.model_fields[key].json_schema_extra == {
            "format": "plain_text"
        }
    assert len(output.attachments) == 3
    first, second, forwarded = output.attachments
    assert first is not second
    assert blobs.files[first] == ("café.txt", "text/plain", b"same bytes")
    assert blobs.files[second] == ("second.txt", "text/plain", b"same bytes")
    assert blobs.files[forwarded][:2] == ("forwarded.eml", "message/rfc822")
    assert b"Forwarded body" in blobs.files[forwarded][2]
    assert list(table.warnings) == []
    assert sources.exited
    assert all(not item.stream.closed for item in sources.inputs)


def test_email_sorts_sources_and_reports_bounded_warnings_after_late_bad_siblings():
    sources = _Sources(
        [
            ("b.eml", "eml", _message("Second")),
            ("a.eml", "eml", _message("First")),
            *((f"z{index:02}.eml", "eml", b"not an email") for index in range(25)),
        ]
    )
    table = producer.import_email(sources.params, sources, _Blobs())
    assert [row.output.subject for row in table.rows] == ["First", "Second"]
    warnings = list(table.warnings)
    assert len(warnings) == 20
    assert warnings[0].startswith("z00.eml:")
    assert warnings[-1] == (
        "25 invalid email messages total; 6 additional warnings omitted"
    )
    assert sources.exited


@pytest.mark.parametrize("format,data", [("eml", b"invalid"), ("mbox", b"")])
def test_email_all_invalid_refuses_and_exits_sources(format, data):
    sources = _Sources([("bad", format, data)])
    table = producer.import_email(sources.params, sources, _Blobs())
    with pytest.raises(TableError) as caught:
        list(table.rows)
    assert caught.value.code == "email_parse_failed"
    assert sources.exited
    assert not sources.inputs[0].stream.closed


def test_email_mbox_locations_and_early_close_close_parser_not_borrowed_stream(
    monkeypatch,
):
    payload = b"".join(
        b"From sender@example.test Tue Sep 2 12:00:00 2026\n"
        + _message(subject)
        + b"\n"
        for subject in ("First", "Second")
    )
    sources = _Sources([("mail/archive.mbox", "mbox", payload)])
    closed = []
    close = producer.email_import._MboxStream.close

    def record_close(box):
        closed.append(box)
        close(box)

    monkeypatch.setattr(producer.email_import._MboxStream, "close", record_close)
    table = producer.import_email(sources.params, sources, _Blobs())
    rows = iter(table.rows)
    assert next(rows).output.source_file == "mail/archive.mbox#1"
    rows.close()
    assert len(closed) == 1
    assert sources.exited
    assert not sources.inputs[0].stream.closed
    # The same borrowed source is rewindable for a subsequent invocation.
    table = producer.import_email(sources.params, sources, _Blobs())
    assert [row.output.source_file for row in table.rows] == [
        "mail/archive.mbox#1",
        "mail/archive.mbox#2",
    ]


@pytest.mark.parametrize("stop", ["exhaust", "close", "stage_failure"])
def test_email_scratch_removed_before_advance_and_on_close_or_stage_failure(
    monkeypatch,
    stop,
):
    sources = _Sources(
        [
            ("a.eml", "eml", _message(attachments=True)),
            ("b.eml", "eml", _message(attachments=True)),
        ]
    )
    scratch = []
    parser_closed = []
    parse = producer.email_import.iter_email_messages

    def inspect_parser(*args, **kwargs):
        directory = kwargs["attachment_dir"]
        scratch.append(directory)
        parsed = parse(*args, **kwargs)
        try:
            for message in parsed:
                # Descriptor digests and sizes are not staging authority.
                yield replace(
                    message,
                    attachments=[
                        replace(item, sha256="not-authority", size=999999)
                        for item in message.attachments
                    ],
                )
                assert not list(directory.iterdir())
        finally:
            parsed.close()
            parser_closed.append(True)

    monkeypatch.setattr(producer.email_import, "iter_email_messages", inspect_parser)
    blobs = _Blobs()
    if stop == "stage_failure":
        stage = blobs.stage

        def fail_second(*args, **kwargs):
            if blobs.files:
                raise RuntimeError("attachment staging failed")
            return stage(*args, **kwargs)

        monkeypatch.setattr(blobs, "stage", fail_second)
    table = producer.import_email(sources.params, sources, blobs)
    rows = iter(table.rows)
    if stop == "stage_failure":
        with pytest.raises(RuntimeError, match="attachment staging failed"):
            next(rows)
    elif stop == "close":
        next(rows)
        assert not list(scratch[0].iterdir())
        rows.close()
    else:
        assert len(list(rows)) == 2
    assert parser_closed == [True]
    assert sources.exited
    assert not scratch[0].exists()
    assert all(not item.stream.closed for item in sources.inputs)
