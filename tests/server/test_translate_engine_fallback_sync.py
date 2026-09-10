"""Translate fallback vs backend truth (closure-sweep fence G5, audit
2026-07-24): the hand-kept web pre-catalog fallback
(web/src/actions/translateEngineCatalog.ts) must mirror the backend's pinned
roster and per-engine language declarations (schemas/maps.py). The OCR chain
is generated+checked (sync_ocr_engine_catalog.py); translate's fallback is
hand-written, so this comparison test is its no-drift fence — the class that
shipped `trafilatura_html` in to_markdown's fallback (audit L9).
"""

from __future__ import annotations

import re
from pathlib import Path

from frisket.actions.translation_languages import (
    TRANSLATE_ENGINE_LANGUAGE,
    TRANSLATE_ENGINES,
)

ROOT = Path(__file__).resolve().parents[2]
TS = ROOT / "web/src/actions/translateEngineCatalog.ts"


def _fallback_entries(text: str) -> list[tuple[str, str]]:
    array = text.split("TRANSLATE_ENGINE_FALLBACK: EngineOption[] = [", 1)[1]
    array = array.split("];", 1)[0]
    return re.findall(
        r"id:\s*'([a-z_0-9]+)'(?:(?!id:\s*').)*?language:\s*(TRANSLATE_[A-Z0-9_]+)",
        array,
        re.S,
    )


def _declared_consts(text: str) -> dict[str, dict[str, object]]:
    decls: dict[str, dict[str, object]] = {}
    for match in re.finditer(
        r"const (TRANSLATE_[A-Z0-9_]+): LanguageDeclaration = \{(.*?)\};",
        text,
        re.S,
    ):
        body = match.group(2)
        mode = re.search(r"mode:\s*'([a-z_]+)'", body)
        detects = re.search(r"detects:\s*(true|false)", body)
        allows_auto = re.search(r"allows_auto:\s*(true|false)", body)
        assert mode is not None and detects is not None, match.group(1)
        decls[match.group(1)] = {
            "mode": mode.group(1),
            "detects": detects.group(1) == "true",
            "allows_auto": (
                None if allows_auto is None else allows_auto.group(1) == "true"
            ),
        }
    return decls


def test_fallback_roster_matches_backend_engines() -> None:
    # rule19: two-sources: hand-kept web fallback catalog diffed against the Python engine roster
    entries = _fallback_entries(TS.read_text())
    assert [engine_id for engine_id, _ in entries] == list(TRANSLATE_ENGINES)


def test_fallback_language_declarations_match_backend() -> None:
    # rule19: two-sources: hand-kept web fallback catalog diffed against the Python engine roster
    text = TS.read_text()
    consts = _declared_consts(text)
    for engine_id, const_name in _fallback_entries(text):
        backend = TRANSLATE_ENGINE_LANGUAGE[engine_id]
        frontend = consts[const_name]
        assert frontend["mode"] == backend.mode, engine_id
        assert frontend["detects"] == backend.detects, engine_id
        if backend.mode == "single" and frontend["allows_auto"] is not None:
            assert frontend["allows_auto"] == backend.allows_auto, engine_id
