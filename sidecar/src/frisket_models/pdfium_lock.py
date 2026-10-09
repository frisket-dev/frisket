"""One process-wide guard for libraries that call PDFium."""

from __future__ import annotations

import threading


# PDFium calls must not overlap across pypdfium2, Docling, or future adapters.
# RLock permits a guarded library path to call another guarded helper safely.
PDFIUM_LOCK = threading.RLock()
