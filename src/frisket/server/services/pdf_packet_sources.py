"""Trusted source descriptors shared by packet admission and review."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping


MAX_INTERACTIVE_PDF_PAGES = 2000


@dataclass(frozen=True)
class AdmittedPacketSource:
    """Server-admitted source; the opener owns each borrowed path lifetime."""

    key: str
    sha256: str
    filename: str
    size: int
    page_count: int
    page_start: int
    page_end: int
    open_source: Callable[[], ContextManager[Path]]
    source_url: str | None = None
    source_sheet_id: int | None = None
    source_row_id: int | None = None
    source_column_id: int | None = None
    frozen_value: Any = None
    prepared_ref_id: int | None = None
    prepared_pages: tuple[int, ...] = ()
    preview_inventory_path: Path | None = None
    mime: str = "application/pdf"
    provider: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
