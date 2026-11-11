"""Archive paths and stored files for portable dataset exports."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from zipfile import ZIP_STORED, ZipFile

from frisket.engine.store.blob_backend import BlobStoreError, validate_blob_digest
from frisket.server.downloads import download_filename
from frisket.server.exports.rowset import ExportError


class ExportAttachments:
    """Register only files encountered by the export's normal value reader."""

    def __init__(self) -> None:
        self.paths: dict[str, str] = {}

    def path(self, digest: str, filename: str | None) -> str:
        if digest not in self.paths:
            basename = PurePosixPath(str(filename or "file").replace("\\", "/")).name
            safe_name = download_filename(basename, "")
            self.paths[digest] = f"attachments/{digest}/{safe_name}"
        return self.paths[digest]

    def write(self, project: Any, archive: ZipFile) -> None:
        for digest, name in self.paths.items():
            try:
                validate_blob_digest(digest)
                with project.blob_store.materialize(digest) as path:
                    archive.write(path, name, compress_type=ZIP_STORED)
            except (BlobStoreError, ValueError) as exc:
                raise ExportError(
                    "attachment_unavailable",
                    f"The attachment ‘{PurePosixPath(name).name}’ could not be read. "
                    "Restore the file or export without attachments.",
                ) from exc
