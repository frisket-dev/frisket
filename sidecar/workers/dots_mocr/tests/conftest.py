from __future__ import annotations

import sys
from pathlib import Path

worker_dir = Path(__file__).resolve().parents[1]
sidecar_src = worker_dir.parents[1] / "src"
sys.path[:0] = [str(worker_dir), str(sidecar_src)]
