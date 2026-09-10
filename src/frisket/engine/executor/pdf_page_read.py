"""Invocation-owned PDF rendering, bounded by host demand rather than Params."""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType

from frisket.actions.types import PdfPage, StagedFile, TableError
from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
from frisket.engine.sandbox import fence
from frisket.engine.sandbox.media_sync import run_media_sync
from frisket.engine.sandbox.shim import SandboxPolicy, run_sandboxed


class AdmittedPdfPageRenderer:
    def __init__(
        self,
        blobs: AdmittedImportBlobStager,
        *,
        page_limit: int | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        if page_limit is not None and (type(page_limit) is not int or page_limit < 0):
            raise ValueError("PDF page limit must be a nonnegative integer")
        self._blobs = blobs
        self._page_limit = page_limit
        self._cancelled = cancelled
        self._closed = False

    def _is_cancelled(self) -> bool:
        return self._closed or (self._cancelled is not None and self._cancelled())

    @property
    def facts(self) -> tuple:
        # The admitted source read and staged document/page roles own provenance.
        return ()

    def _check_cancelled(self) -> None:
        if self._is_cancelled():
            raise TableError("action_cancelled", "PDF rendering was cancelled")

    def close(self) -> None:
        self._closed = True

    def render(self, document: StagedFile, *, dpi: int) -> Mapping[int, StagedFile]:
        self._check_cancelled()
        source = self._blobs.describe(document)
        if source.role != "document":
            raise ValueError("PDF rendering requires an admitted PDF document")
        if type(dpi) is not int or not 50 <= dpi <= 600:
            raise ValueError("PDF dpi must be between 50 and 600")
        executable = shutil.which("pdftoppm")
        if executable is None or self._page_limit == 0:
            return MappingProxyType({})
        with tempfile.TemporaryDirectory(prefix="frisket-pdf-pages-") as temporary:
            directory = Path(temporary)
            # Diagnostics were previously discarded; do not buffer arbitrary
            # document-triggered Poppler messages in the host process.
            command = [executable, "-q", "-png", "-r", str(dpi)]
            if self._page_limit is not None:
                command.extend(["-f", "1", "-l", str(self._page_limit)])
            command.extend([str(source.path), str(directory / "page")])
            try:
                # The sandbox alone owns timeout, cancellation and process-tree
                # reaping; source and scratch stay alive until it settles.
                result = run_media_sync(
                    lambda should_cancel: run_sandboxed(
                        command,
                        policy=SandboxPolicy(
                            cpu_seconds=30,
                            wall_seconds=30,
                            memory_mb=2048,
                            env_passthrough=["PATH"],
                            confine=fence.Confinement(
                                op="PDF page rasterization",
                                read=(str(source.path), "/etc/fonts"),
                                write=(str(directory),),
                                exec_binary=executable,
                            ),
                        ),
                        scratch_dir=directory,
                        should_cancel=should_cancel,
                    ),
                    cancelled=self._is_cancelled,
                )
            except OSError:
                return MappingProxyType({})
            self._check_cancelled()
            if result.cancelled:
                raise TableError("action_cancelled", "PDF rendering was cancelled")
            if not result.ok:
                return MappingProxyType({})

            images = {}
            stem = source.filename.rsplit(".", 1)[0] or "document"
            pages = (
                (int(match[1]), path)
                for path in directory.glob("page-*.png")
                if (match := re.fullmatch(r"page-(\d+)\.png", path.name)) is not None
            )
            for page, path in sorted(pages):
                if page < 1 or (
                    self._page_limit is not None and page > self._page_limit
                ):
                    continue
                self._check_cancelled()
                # The child can create symlinks and FIFOs inside its scratch
                # fence. Admit only regular files, without following links or
                # blocking on a substituted FIFO after the sandbox has exited.
                try:
                    metadata = path.lstat()
                    if not stat.S_ISREG(metadata.st_mode):
                        continue
                    descriptor = os.open(
                        path,
                        os.O_RDONLY
                        | getattr(os, "O_NOFOLLOW", 0)
                        | getattr(os, "O_NONBLOCK", 0)
                        | getattr(os, "O_CLOEXEC", 0),
                    )
                except OSError:
                    continue
                with os.fdopen(descriptor, "rb") as stream:
                    opened = os.fstat(stream.fileno())
                    if not stat.S_ISREG(opened.st_mode) or (
                        opened.st_dev,
                        opened.st_ino,
                    ) != (metadata.st_dev, metadata.st_ino):
                        continue
                    images[page] = self._blobs.stage(
                        stream,
                        filename=f"{stem}-p{page:04d}.png",
                        mime="image/png",
                        role=PdfPage(document=document, page=page),
                    )
            self._check_cancelled()
            return MappingProxyType(images)
