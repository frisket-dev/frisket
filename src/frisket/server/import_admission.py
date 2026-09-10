"""Optional import-concurrency admission port shared with hosted composition.

Cloud supplies the port after it has resolved a request to a trusted storage
organization.  Solo callers leave it unset and therefore remain unlimited.
"""

from __future__ import annotations

from typing import Protocol

from fastapi.responses import JSONResponse
from starlette.types import Scope


class ImportAdmissionPermit(Protocol):
    """One acquired import slot.  Implementations make release idempotent."""

    def release(self) -> None: ...


class ImportAdmission(Protocol):
    def try_acquire(self) -> ImportAdmissionPermit | None: ...


IMPORT_ACTION_KINDS = frozenset(
    {
        "import.append_csv",
        "import.append_rows",
        "import.append_xlsx",
        "import.csv",
        "import.email",
        "import.files",
        "import.geojson",
        "import.kml",
        "import.ndjson",
        "import.pdf",
        "import.rows",
        "import.runtime",
        "import.update_csv",
        "import.update_rows",
        "import.update_xlsx",
        "import.urls",
        "import.xlsx",
        "frisket.ftm.ftm_import",
    }
)


def is_import_action(kind: object) -> bool:
    return isinstance(kind, str) and kind in IMPORT_ACTION_KINDS


def is_native_import_request(scope: Scope) -> bool:
    """Recognize every POST under a project's native ``/import/**`` prefix."""

    if scope["type"] != "http" or scope.get("method") != "POST":
        return False
    parts = scope.get("path", "").split("/")
    return (
        len(parts) >= 6
        and parts[1:3] == ["api", "projects"]
        and bool(parts[3])
        and parts[4] == "import"
    )


def import_admission_refusal() -> JSONResponse:
    return JSONResponse(
        {"detail": "too many imports are already running"},
        status_code=429,
        headers={"Retry-After": "1"},
    )
