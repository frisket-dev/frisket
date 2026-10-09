"""Atomic table-host commit for confirmed PDF packet ranges."""

from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pypdf import PdfReader, PdfWriter

from frisket.actions.pdf_packet_import_types import (
    PacketOcrPage,
    PacketSplitChild,
    PacketSplitManifest,
)
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor.pdf_packet_split_read import AdmittedPacketSplitReader
from frisket.engine.executor.ocr_read import ocr_token_granularity
from frisket.server.workspace import Workspace


class PdfPacketCommitter:
    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def commit(
        self,
        project_id: str,
        *,
        source_path: Path,
        source_filename: str,
        source_page_count: int,
        ranges: Sequence[tuple[int, int]],
        names: Sequence[str],
        ocr_pages: Mapping[int, dict[str, Any]] | None,
        ocr_engine: str | None,
        sheet_name: str,
        idempotency_key: str,
        request_context: Any,
        progress: Callable[[int, int], None],
        cancelled: Callable[[], bool],
        seal_cancellation: Callable[[], bool],
    ) -> dict[str, Any]:
        if len(ranges) != len(names):
            raise ValueError("packet ranges and names must have the same length")
        scratch = Path(tempfile.mkdtemp(prefix="frisket-packet-commit-"))
        try:
            reader = PdfReader(source_path, strict=False)
            children = []
            child_paths = {}
            for index, ((start, end), filename) in enumerate(
                zip(ranges, names, strict=True), 1
            ):
                if cancelled():
                    raise RuntimeError("packet commit was cancelled")
                writer = PdfWriter()
                for page in range(start, end + 1):
                    writer.add_page(reader.pages[page - 1])
                path = scratch / f"{index}.pdf"
                with path.open("wb") as sink:
                    writer.write(sink)
                child_paths[index] = path
                prepared = ()
                if ocr_pages is not None:
                    if ocr_engine is None:
                        raise ValueError("retained OCR requires one engine")
                    prepared = tuple(
                        PacketOcrPage(
                            page=source_page - start + 1,
                            text=str(ocr_pages[source_page].get("text") or ""),
                            blocks=tuple(ocr_pages[source_page].get("blocks") or ()),
                            engine=ocr_engine,
                            token_granularity=ocr_token_granularity(ocr_engine),
                        )
                        for source_page in range(start, end + 1)
                    )
                children.append(
                    PacketSplitChild(
                        index=index,
                        filename=filename,
                        page_start=start,
                        page_end=end,
                        ocr_pages=prepared,
                    )
                )
                progress(index, len(ranges))
            ref = uuid.uuid4().hex
            packet_reader = AdmittedPacketSplitReader(
                ref=ref,
                manifest=PacketSplitManifest(
                    filename=source_filename,
                    page_count=source_page_count,
                    children=tuple(children),
                ),
                source=source_path,
                children=child_paths,
            )
            factory = self._workspace.executor_deps_factory
            deps = (
                factory(project_id, request_context)
                if factory is not None
                else ExecutorDeps()
            )
            deps = replace(
                deps,
                packet_split_reader=packet_reader,
                cancelled=cancelled,
                seal_cancellation=seal_cancellation,
            )
            project = self._workspace.get(project_id)
            result = run_action_spec(
                project,
                {
                    "action_id": "import.packet_split",
                    "scope": {"kind": "project"},
                    "sheet_name": sheet_name,
                    "params": {
                        "source_ref": ref,
                        "keep_ocr_text": ocr_pages is not None,
                    },
                    "output_names": {},
                    "idempotency_key": idempotency_key,
                },
                project_id=project_id,
                router=self._workspace.router_for(project),
                deps=deps,
            )
            if result.status != "completed":
                detail = (
                    f"{result.errors[0].message}: {result.errors[0].details}"
                    if result.errors
                    else "packet import failed"
                )
                raise RuntimeError(detail)
            sheet_output = next(
                (
                    output
                    for output in result.outputs
                    if output.ref.get("kind") == "materialized_sheet"
                    and output.sheet_id is not None
                ),
                None,
            )
            if sheet_output is None:
                raise RuntimeError("packet import did not return a sheet")
            return {
                "sheet_id": int(sheet_output.sheet_id),
                "document_count": len(ranges),
                "receipt_id": result.receipt_id,
            }
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


__all__ = ["PdfPacketCommitter"]
