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


def test_generic_ocr_roster_tracks_shared_vision_catalog() -> None:
    from frisket.ai.llm.model_catalog import MODEL_ENTRIES
    from frisket.server.action_catalog_hints import ocr_vision_engines

    expected = [
        f"{provider}/{entry['id']}"
        for provider, entries in MODEL_ENTRIES.items()
        for entry in entries
        if entry.get("vision") is True
    ]

    assert [entry["id"] for entry in ocr_vision_engines()] == expected
    assert len(expected) == len(set(expected))


def test_generic_ocr_roster_reads_the_current_catalog_snapshot(monkeypatch) -> None:
    from frisket.server import action_catalog_hints

    monkeypatch.setattr(
        action_catalog_hints,
        "MODEL_ENTRIES",
        {
            "sample": [
                {"id": "text-only", "label": "Text only", "vision": False},
                {"id": "vision", "label": "Vision", "vision": True},
            ]
        },
    )

    assert [entry["id"] for entry in action_catalog_hints.ocr_vision_engines()] == [
        "sample/vision"
    ]


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
