"""One-use host binding for a prepared PDF-packet import."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator, Mapping

from frisket.actions.pdf_packet_import_types import PacketSplitManifest


class AdmittedPacketSplitReader:
    def __init__(
        self,
        *,
        ref: str,
        manifest: PacketSplitManifest,
        source: Path,
        children: Mapping[int, Path],
    ) -> None:
        self._ref = ref
        self._manifest = manifest
        self._source = source
        self._children = dict(children)
        self.facts: list[dict] = []

    def _require_ref(self, ref: str) -> None:
        if ref != self._ref:
            raise ValueError("PDF packet source was not admitted by this invocation")

    def manifest(self, ref: str) -> PacketSplitManifest:
        self._require_ref(ref)
        self.facts = [
            {
                "kind": "pdf_packet_split",
                "ref": ref,
                "page_count": self._manifest.page_count,
                "document_count": len(self._manifest.children),
            }
        ]
        return self._manifest

    @contextmanager
    def open_source(self, ref: str) -> Iterator[BinaryIO]:
        self._require_ref(ref)
        with self._source.open("rb") as source:
            yield source

    @contextmanager
    def open_child(self, ref: str, index: int) -> Iterator[BinaryIO]:
        self._require_ref(ref)
        path = self._children.get(index)
        if path is None:
            raise ValueError("PDF packet child was not admitted by this invocation")
        with path.open("rb") as source:
            yield source


__all__ = ["AdmittedPacketSplitReader"]
