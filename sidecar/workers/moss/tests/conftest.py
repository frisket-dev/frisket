"""Make the leaf importable on CPU without an editable install.

The recorded-output gate must run from a plain checkout, so the ``src`` layout
is put on ``sys.path`` here rather than requiring ``uv pip install -e``.
``frisket_models`` itself is expected on the path already (the sidecar dev
env).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
