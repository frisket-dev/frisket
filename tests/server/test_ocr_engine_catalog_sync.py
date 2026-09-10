"""OCR engine list parity: the web fallback is generated, not hand-synced.

Twin of tests/ai/test_model_catalog.py::test_web_fallback_is_in_sync_with_canonical_catalog.
A retired-model drift (gemini/gemini-2.5-flash surviving in
web/src/actions/engineCatalog.ts after the backend moved to
gemini/gemini-3.5-flash) shipped invisibly to CI because nothing checked
frontend/backend parity for the OCR engine list — see
scripts/dev/sync_ocr_engine_catalog.py and web/src/actions/engineCatalog.generated.ts.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_web_ocr_fallback_is_in_sync_with_canonical_catalog(tmp_path: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/dev/sync_ocr_engine_catalog.py"),
            "--check",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
