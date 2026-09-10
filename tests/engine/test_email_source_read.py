from __future__ import annotations

import hashlib
import io
import json
from contextlib import ExitStack

import pytest

from frisket.actions.types import EmailInput, EmailSourceRef, TableError
from frisket.engine.executor.email_source_read import AdmittedEmailSourceReader


def _ref(source_ref="opaque:one", logical_path="Inbox/one.eml", format="eml"):
    return EmailSourceRef(
        source_ref=source_ref, logical_path=logical_path, format=format
    )


def _input(stream, logical_path="Inbox/one.eml", format="eml"):
    return EmailInput(logical_path=logical_path, format=format, stream=stream)


@pytest.mark.parametrize("admitted", [None, {}])
def test_no_ingress_never_opens_opaque_path(tmp_path, admitted):
    path = tmp_path / "existing.eml"
    path.write_bytes(b"Subject: private\n\nbody")
    reader = AdmittedEmailSourceReader(admitted)
    with pytest.raises(TableError, match="trusted ingress"):
        with reader.open([_ref(source_ref=str(path))]):
            pytest.fail("an opaque ref must never be used as a path")
    assert reader.facts == []


@pytest.mark.parametrize("case", ["unknown", "duplicate", "omitted", "path", "format"])
def test_entire_exact_source_set_is_checked_before_exposing_streams(case):
    class Unread(io.BytesIO):
        def read(self, *_args):
            pytest.fail("source bytes read during descriptor admission")

    one, two = Unread(b"one"), Unread(b"two")
    mapping = {
        "opaque:one": _input(one),
        "opaque:two": _input(two, "Inbox/two.eml"),
    }
    sources = [_ref(), _ref("opaque:two", "Inbox/two.eml")]
    if case == "unknown":
        sources[1] = _ref("unknown", "Inbox/two.eml")
    elif case == "duplicate":
        sources.append(sources[0])
    elif case == "omitted":
        sources.pop()
    elif case == "path":
        sources[1] = _ref("opaque:two", "changed.eml")
    else:
        sources[1] = _ref("opaque:two", "Inbox/two.eml", "mbox")
    reader = AdmittedEmailSourceReader(mapping)
    with pytest.raises(TableError, match="trusted ingress"):
        with reader.open(sources):
            pytest.fail("invalid input set was exposed")
    assert reader.facts == []
    assert not one.closed and not two.closed


def test_borrowed_readonly_streams_preserve_order_seek_and_owner():
    one, two = io.BytesIO(b"one\nsecond\n"), io.BytesIO(b"two")
    mapping = {"opaque:one": _input(one), "opaque:two": _input(two, "Inbox/two.eml")}
    reader = AdmittedEmailSourceReader(mapping)
    with reader.open([_ref("opaque:two", "Inbox/two.eml"), _ref()]) as inputs:
        assert [item.logical_path for item in inputs] == [
            "Inbox/two.eml",
            "Inbox/one.eml",
        ]
        wrapped = inputs[1].stream
        assert wrapped.readline(4) == b"one\n"
        wrapped.seek(0)
        assert wrapped.tell() == 0
        assert wrapped.read(3) == b"one"
        with pytest.raises(io.UnsupportedOperation):
            wrapped.write(b"changed")
        with pytest.raises(io.UnsupportedOperation):
            wrapped.truncate(0)
        assert not hasattr(wrapped, "getbuffer")
        wrapped.close()
        assert not one.closed
    assert all(item.stream.closed for item in inputs)
    assert not one.closed and not two.closed
    expected_descriptors = [
        {"source_ref": "opaque:two", "logical_path": "Inbox/two.eml", "format": "eml"},
        {"source_ref": "opaque:one", "logical_path": "Inbox/one.eml", "format": "eml"},
    ]
    expected_digest = hashlib.sha256(
        json.dumps(expected_descriptors, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert reader.facts == [
        {
            "kind": "email_source_admission",
            "source_count": 2,
            "descriptors_sha256": expected_digest,
        }
    ]
    reader.close()


@pytest.mark.parametrize("change", ["source_ref", "logical_path", "format", "order"])
def test_admission_hash_binds_actual_ordered_descriptors_not_just_count(change):
    def admission(sources):
        mapping = {
            source.source_ref: _input(
                io.BytesIO(b"unchanged"), source.logical_path, source.format
            )
            for source in reversed(sources)
        }
        reader = AdmittedEmailSourceReader(mapping)
        try:
            with reader.open(sources):
                pass
            return reader.facts[0]
        finally:
            reader.close()
            for source in mapping.values():
                source.stream.close()

    sources = [_ref(), _ref("opaque:two", "Inbox/two.eml")]
    before = admission(sources)
    if change == "order":
        sources.reverse()
    else:
        value = {
            "source_ref": "opaque:new",
            "logical_path": "Inbox/renamed.eml",
            "format": "mbox",
        }[change]
        sources[0] = sources[0].model_copy(update={change: value})
    after = admission(sources)
    assert before["source_count"] == after["source_count"] == 2
    assert before["descriptors_sha256"] != after["descriptors_sha256"]


@pytest.mark.parametrize("failure", [RuntimeError, GeneratorExit])
def test_consumer_failure_closes_wrappers_without_drain_or_owner_close(failure):
    class Tracking(io.BytesIO):
        reads = 0

        def read(self, size=-1):
            self.reads += 1
            return super().read(size)

    source = Tracking(b"email body")
    reader = AdmittedEmailSourceReader({"opaque:one": _input(source)})
    with pytest.raises(failure):
        with reader.open([_ref()]) as inputs:
            assert inputs[0].stream.read(1) == b"e"
            raise failure("consumer stopped")
    assert inputs[0].stream.closed
    assert not source.closed
    assert source.reads == 1
    reader.close()


def test_host_close_closes_active_wrapper_but_not_source():
    source = io.BytesIO(b"mail")
    reader = AdmittedEmailSourceReader({"opaque:one": _input(source)})
    with ExitStack() as consumer:
        inputs = consumer.enter_context(reader.open([_ref()]))
        reader.close()
        assert inputs[0].stream.closed
        assert not source.closed
        with pytest.raises(TableError, match="trusted ingress"):
            consumer.enter_context(reader.open([_ref()]))
    reader.close()


def test_mbox_line_reads_delegate_bounded_request():
    class Lines(io.BytesIO):
        def read(self, *_args):
            pytest.fail("readline fell back to per-byte reads")

        def readline(self, size=-1):
            assert size == 64 * 1024
            return super().readline(size)

    source = Lines(b"From sender\nSubject: test\n")
    reader = AdmittedEmailSourceReader({"opaque:one": _input(source, format="mbox")})
    with reader.open([_ref(format="mbox")]) as inputs:
        assert inputs[0].stream.readline(64 * 1024) == b"From sender\n"
    assert not source.closed
    reader.close()


def test_closed_stream_refuses_without_admission_facts():
    source = io.BytesIO(b"closed")
    source.close()
    reader = AdmittedEmailSourceReader({"opaque:one": _input(source)})
    with pytest.raises(TableError, match="trusted ingress"):
        with reader.open([_ref()]):
            pytest.fail("closed source admitted")
    assert reader.facts == []
