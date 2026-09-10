from __future__ import annotations

import sys
from pathlib import Path

WORKER = Path(__file__).resolve().parents[1]
SIDECAR_SRC = Path(__file__).resolve().parents[3] / "src"
for path in (WORKER, SIDECAR_SRC):
    value = str(path)
    if value not in sys.path:
        sys.path.insert(0, value)
