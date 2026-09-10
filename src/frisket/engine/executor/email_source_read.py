"""Closed-set admission of opaque, request-pinned email inputs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from typing import Any, BinaryIO, cast

from frisket.actions.types import EmailInput, EmailSourceRef, TableError
from frisket.engine.executor.local_file_read import _ReadOnlyBinary


class _ReadOnlyEmailBinary(_ReadOnlyBinary):
    def readline(self, size: int = -1) -> bytes:
        # MBOX framing requests bounded lines. RawIOBase's fallback would
        # issue a separate read(1) for every byte; delegate only this read API.
        self._checkClosed()
        return self._stream.readline(size)


class AdmittedEmailSourceReader:
    def __init__(self, sources: Mapping[str, EmailInput] | None = None) -> None:
        self._sources = dict(sources) if sources is not None else None
        self._resources = ExitStack()
        self._closed = False
        self.facts: list[dict[str, Any]] = []

    @staticmethod
    def _refused() -> TableError:
        return TableError(
            "email_sources_unavailable",
            "Email sources require matching request-scoped trusted ingress",
        )

    @contextmanager
    def open(
        self, sources: Sequence[EmailSourceRef]
    ) -> Iterator[tuple[EmailInput, ...]]:
        if self._closed or self._sources is None:
            raise self._refused()
        requested = tuple(sources)
        if not requested or any(
            not isinstance(source, EmailSourceRef) for source in requested
        ):
            raise self._refused()
        refs = [source.source_ref for source in requested]
        if len(set(refs)) != len(refs) or set(refs) != set(self._sources):
            raise self._refused()
        admitted = []
        # Canonical JSON array in actual requested order, encoded one descriptor
        # at a time rather than retaining another corpus-sized metadata list.
        descriptor_digest = hashlib.sha256(b"[")
        encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"))
        for source in requested:
            actual = self._sources[source.source_ref]
            if (
                not isinstance(actual, EmailInput)
                or actual.logical_path != source.logical_path
                or actual.format != source.format
            ):
                raise self._refused()
            try:
                if not actual.stream.readable() or not actual.stream.seekable():
                    raise self._refused()
            except (AttributeError, OSError, ValueError) as exc:
                raise self._refused() from exc
            if admitted:
                descriptor_digest.update(b",")
            for chunk in encoder.iterencode(
                {
                    "source_ref": source.source_ref,
                    "logical_path": source.logical_path,
                    "format": source.format,
                }
            ):
                descriptor_digest.update(chunk.encode("utf-8"))
            admitted.append(actual)
        descriptor_digest.update(b"]")

        with ExitStack() as opened:
            self._resources.callback(opened.close)
            inputs = tuple(
                EmailInput(
                    logical_path=source.logical_path,
                    format=source.format,
                    stream=cast(
                        BinaryIO,
                        opened.enter_context(_ReadOnlyEmailBinary(source.stream)),
                    ),
                )
                for source in admitted
            )
            # This reader hashed admitted descriptors, not source bytes.
            # Keep corpus-sized descriptor lists out of durable receipt facts.
            self.facts.append(
                {
                    "kind": "email_source_admission",
                    "source_count": len(inputs),
                    "descriptors_sha256": descriptor_digest.hexdigest(),
                }
            )
            yield inputs

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._resources.close()
