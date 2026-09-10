"""Public bulk import request and limit types."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Literal
from starlette.datastructures import UploadFile
from frisket.server.route_errors import RouteError

Kind = Literal["csv", "xlsx", "files", "email"]
EmailFormat = Literal["eml", "mbox"]


@dataclass(frozen=True)
class BulkImportLimits:
    max_upload_files: int | None = None
    max_upload_bytes: int | None = None
    max_request_bytes: int | None = None
    max_archive_members: int | None = None
    max_expanded_bytes: int | None = None


@dataclass(frozen=True)
class BulkUpload:
    filename: str
    logical_path: str
    mime: str
    file: UploadFile


class ImportBulkRouteError(RouteError):
    pass
