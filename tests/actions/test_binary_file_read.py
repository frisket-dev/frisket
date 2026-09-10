from __future__ import annotations

import asyncio
import hashlib
import io
import os
from pathlib import Path

import pytest

from frisket.actions.types import TableError
from frisket.engine.executor.action_inventory import BoundLocalFile
from frisket.engine.executor.local_file_read import AdmittedLocalFileReader


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _reader(stream, raw: bytes) -> AdmittedLocalFileReader:
    return AdmittedLocalFileReader(
        {"archive.xlsx": BoundLocalFile(stream=stream, sha256=_digest(raw))}
    )


class BoundedReads(io.BytesIO):
    def __init__(self, raw: bytes):
        super().__init__(raw)
        self.read_sizes = []

    def read(self, size=-1):
        assert 0 <= size <= 64 * 1024, "unbounded host hash read"
        self.read_sizes.append(size)
        return super().read(size)


def test_binary_random_access_is_read_only_and_hashes_entire_borrowed_source():
    raw = b"archive-content" * 20_000
    borrowed = BoundedReads(raw)
    reader = _reader(borrowed, raw)
    with reader.open_binary("archive.xlsx") as source:
        assert source.readable() and source.seekable()
        assert not source.writable()
        assert source.read(3) == raw[:3]
        assert source.seek(-7, os.SEEK_END) == len(raw) - 7
        assert source.read(7) == raw[-7:]
        assert source.seek(2) == source.tell() == 2
        assert source.read(5) == raw[2:7]
        for mutator in (lambda: source.write(b"oops"), lambda: source.truncate(0)):
            with pytest.raises(io.UnsupportedOperation):
                mutator()
        assert not hasattr(source, "getbuffer")
        assert reader.facts == []
        # Closing the file-like facade never transfers source ownership.
        source.close()
    reader.close()
    assert source.closed
    assert not borrowed.closed
    assert borrowed.getvalue() == raw
    assert len(borrowed.read_sizes) > 5
    assert reader.facts == [
        {
            "kind": "local_file_read",
            "path": "archive.xlsx",
            "sha256": _digest(raw),
            "byte_count": len(raw),
        }
    ]


@pytest.mark.parametrize("failure", [ValueError, asyncio.CancelledError, GeneratorExit])
def test_binary_failed_or_abandoned_parse_never_hashes_or_closes_borrowed(failure):
    raw = b"archive"
    borrowed = BoundedReads(raw)
    reader = _reader(borrowed, raw)
    with pytest.raises(failure):
        with reader.open_binary("archive.xlsx") as source:
            assert source.read(1) == b"a"
            raise failure()
    reader.close()
    assert source.closed
    assert borrowed.read_sizes == [1]
    assert not borrowed.closed
    assert reader.facts == []


def test_binary_bound_bytesio_mutation_refused_without_descriptor():
    raw = b"archive"
    borrowed = io.BytesIO(raw)
    reader = _reader(borrowed, raw)
    with pytest.raises(TableError, match="changed after admission"):
        with reader.open_binary("archive.xlsx") as source:
            assert source.read(1) == b"a"
            borrowed.seek(0)
            borrowed.write(b"changed")
    reader.close()
    assert not borrowed.closed
    assert reader.facts == []


@pytest.mark.parametrize("during_hash", [False, True])
def test_binary_descriptor_mutation_during_parse_or_hash_is_refused(
    tmp_path, during_hash
):
    path = tmp_path / "archive.xlsx"
    raw = b"archive"
    path.write_bytes(raw)

    def mutate():
        # Keep the bytes and size unchanged: descriptor metadata must be checked
        # even when an admitted digest would still match.
        before = path.stat()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))

    class MutatingReader(io.BufferedReader):
        def read(self, size=-1):
            chunk = super().read(size)
            if during_hash:
                mutate()
            return chunk

    with MutatingReader(path.open("rb")) as borrowed:
        reader = _reader(borrowed, raw)
        with pytest.raises(TableError, match="changed during parsing"):
            with reader.open_binary("archive.xlsx") as source:
                source.seek(3)
                if not during_hash:
                    mutate()
        reader.close()
        assert not borrowed.closed
    assert reader.facts == []


@pytest.mark.parametrize("operation", ["seek", "read"])
def test_binary_late_hash_failure_does_not_record_facts(operation):
    class FailingSource(io.BytesIO):
        fail = False

        def seek(self, offset, whence=0):
            if self.fail and operation == "seek":
                raise OSError("late seek failure")
            return super().seek(offset, whence)

        def read(self, size=-1):
            if self.fail and operation == "read":
                raise OSError("late read failure")
            return super().read(size)

    raw = b"archive"
    borrowed = FailingSource(raw)
    reader = _reader(borrowed, raw)
    with pytest.raises(TableError, match="could not be verified"):
        with reader.open_binary("archive.xlsx") as source:
            assert source.read(1) == b"a"
            borrowed.fail = True
    reader.close()
    assert not borrowed.closed
    assert reader.facts == []


@pytest.mark.parametrize("fail_close", [False, True])
def test_binary_owned_source_closes_before_recording_facts(
    tmp_path, monkeypatch, fail_close
):
    path = tmp_path / "archive.xlsx"
    raw = b"archive"
    path.write_bytes(raw)
    original_open = Path.open
    opened = []

    class OwnedSource:
        def __enter__(self):
            stream = original_open(path, "rb")
            opened.append(stream)
            return stream

        def __exit__(self, *args):
            opened[-1].close()
            if fail_close:
                raise OSError("late close failure")

    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: OwnedSource())
    reader = AdmittedLocalFileReader()

    def consume():
        with reader.open_binary(str(path)) as source:
            assert source.read(1) == b"a"
            assert reader.facts == []

    if fail_close:
        with pytest.raises(OSError, match="late close failure"):
            consume()
        assert reader.facts == []
    else:
        consume()
        assert reader.facts[0]["sha256"] == _digest(raw)
        assert reader.facts[0]["byte_count"] == len(raw)
    reader.close()
    assert len(opened) == 1
    assert opened[0].closed


def test_binary_closed_admission_map_never_falls_back_to_existing_path(tmp_path):
    path = tmp_path / "archive.xlsx"
    path.write_bytes(b"archive")
    reader = AdmittedLocalFileReader({})
    with pytest.raises(TableError, match="readable local file"):
        with reader.open_binary(str(path)):
            pytest.fail("An unadmitted path was opened")
    reader.close()
    assert reader.facts == []


def test_binary_direct_source_must_be_regular_file(tmp_path):
    reader = AdmittedLocalFileReader()
    with pytest.raises(TableError, match="readable local file"):
        with reader.open_binary(str(tmp_path)):
            pytest.fail("A directory was opened")
    reader.close()
    assert reader.facts == []
