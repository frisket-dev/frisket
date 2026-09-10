from __future__ import annotations

import sys
from pathlib import Path

_SIDECAR_SRC = Path(__file__).resolve().parent / "sidecar" / "src"
if _SIDECAR_SRC.is_dir() and str(_SIDECAR_SRC) not in sys.path:
    sys.path.insert(0, str(_SIDECAR_SRC))
