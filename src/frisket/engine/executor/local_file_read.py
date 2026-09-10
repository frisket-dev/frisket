"""Host-observed local bytes for typed table producers."""

from __future__ import annotations

import hashlib
import io
import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping, TextIO, cast

from frisket.actions.types import TableError
from frisket.engine.executor.action_inventory import BoundLocalFile


class _ObservedRaw(io.RawIOBase):
    """Hash bounded binary reads without taking ownership of the input handle."""

    def __init__(self, stream: BinaryIO, path: str, expected: str | None, facts: list):
        self.stream, self.path, self.expected, self.facts = (
            stream,
            path,
            expected,
            facts,
        )
        self.digest = hashlib.sha256()
        self.byte_count = 0
        self.finished = False

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        try:
            chunk = self.stream.read(len(buffer))
        except (OSError, ValueError) as exc:
            raise TableError("invalid_file_source", "Source could not be read") from exc
        size = len(chunk)
        buffer[:size] = chunk
        if size:
            self.digest.update(chunk)
            self.byte_count += size
        elif not self.finished:
            observed = "sha256:" + self.digest.hexdigest()
            if self.expected is not None and observed != self.expected:
                raise TableError(
                    "invalid_file_source", "Source bytes changed after admission"
                )
            self.facts.append(
                {
                    "kind": "local_file_read",
                    "path": self.path,
                    "sha256": observed,
                    "byte_count": self.byte_count,
                }
            )
            self.finished = True
        return size


class _ReadOnlyBinary(io.RawIOBase):
    """Seekable reader whose closure does not close the borrowed source.

    This enforces the file-like API's read-only discipline, including BytesIO;
    it is not a security sandbox for code with access to Python internals.
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream

    def readable(self) -> bool:
        self._checkClosed()
        return True

    def seekable(self) -> bool:
        self._checkClosed()
        return self._stream.seekable()

    def read(self, size: int = -1) -> bytes:
        self._checkClosed()
        return self._stream.read(size)

    def write(self, buffer: Any) -> int:
        raise io.UnsupportedOperation("read-only source")

    def truncate(self, size: int | None = None) -> int:
        raise io.UnsupportedOperation("read-only source")

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self._checkClosed()
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        self._checkClosed()
        return self._stream.tell()


def _descriptor_state(stream: BinaryIO) -> tuple[int, int, int, int, int] | None:
    try:
        descriptor = stream.fileno()
    except (AttributeError, io.UnsupportedOperation):
        return None
    info = os.fstat(descriptor)
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


class AdmittedLocalFileReader:
    def __init__(self, sources: Mapping[str, BoundLocalFile] | None = None) -> None:
        self.facts: list[dict[str, Any]] = []
        self.sources = sources
        self.resources = ExitStack()

    def close(self) -> None:
        self.resources.close()

    @contextmanager
    def _source(self, path: str) -> Iterator[tuple[BinaryIO, str, str | None]]:
        if not isinstance(path, str) or not path:
            raise TableError(
                "invalid_file_source", "Source must be a readable local file"
            )
        with ExitStack() as opened:
            self.resources.callback(opened.close)
            try:
                if self.sources is not None:
                    admitted = self.sources.get(path)
                    if admitted is None:
                        raise ValueError("Source path was not admitted")
                    stream, actual_path, expected = (
                        admitted.stream,
                        path,
                        admitted.sha256,
                    )
                    stream.seek(0)
                else:
                    source = Path(path).resolve()
                    if not source.is_file():
                        raise OSError("source is not a regular file")
                    stream = opened.enter_context(source.open("rb"))
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise OSError("source is not a regular file")
                    actual_path, expected = str(source), None
            except (OSError, ValueError, RuntimeError) as exc:
                if isinstance(exc, TableError):
                    raise
                raise TableError(
                    "invalid_file_source", "Source must be a readable local file"
                ) from exc
            yield stream, actual_path, expected

    @contextmanager
    def _open(self, path: str) -> Iterator[io.BufferedReader]:
        with self._source(path) as (stream, actual_path, expected):
            observed = _ObservedRaw(stream, actual_path, expected, self.facts)
            with io.BufferedReader(observed) as buffered:
                self.resources.callback(buffered.close)
                yield buffered
                if not observed.finished:
                    raise TableError(
                        "invalid_file_source", "Admitted source was not read completely"
                    )

    @contextmanager
    def open_binary(self, path: str) -> Iterator[BinaryIO]:
        with self._source(path) as (stream, actual_path, expected):
            try:
                if not stream.seekable():
                    raise ValueError("Source is not seekable")
                before = _descriptor_state(stream)
            except (OSError, ValueError) as exc:
                raise TableError(
                    "invalid_file_source", "Source must be a seekable local file"
                ) from exc
            with _ReadOnlyBinary(stream) as binary:
                self.resources.callback(binary.close)
                yield cast(BinaryIO, binary)
            # Only normal consumption reaches this pass: parser failures,
            # cancellation and GeneratorExit must never drain the source.
            try:
                stream.seek(0)
                digest = hashlib.sha256()
                byte_count = 0
                while chunk := stream.read(64 * 1024):
                    digest.update(chunk)
                    byte_count += len(chunk)
                observed = "sha256:" + digest.hexdigest()
                if before != _descriptor_state(stream):
                    raise TableError(
                        "invalid_file_source", "Source changed during parsing"
                    )
                if expected is not None and observed != expected:
                    raise TableError(
                        "invalid_file_source", "Source bytes changed after admission"
                    )
            except (OSError, ValueError) as exc:
                if isinstance(exc, TableError):
                    raise
                raise TableError(
                    "invalid_file_source", "Source could not be verified"
                ) from exc
        # Owned handles must also close successfully before facts are published.
        self.facts.append(
            {
                "kind": "local_file_read",
                "path": actual_path,
                "sha256": observed,
                "byte_count": byte_count,
            }
        )

    def read_bytes(self, path: str) -> bytes:
        with self._open(path) as stream:
            return stream.read()

    @contextmanager
    def open_text(
        self, path: str, *, encoding: str = "utf-8", newline: str | None = ""
    ) -> Iterator[TextIO]:
        with self._open(path) as binary:
            with io.TextIOWrapper(binary, encoding=encoding, newline=newline) as text:
                yield text
