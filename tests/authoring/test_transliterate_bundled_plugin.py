"""Tests for the bundled ``frisket.transliterate`` plugin. Pattern mirrors
tests/test_ftm_bundled_plugin.py (manifest assertions + a direct-import
functional check of the handler) and tests/test_workbench_plugin_geo_bundled.py
(the ``real_bundled_root`` fixture that re-points bootstrap at the real
shipped tree so the plugin's real wiring, not a fixture stand-in, is what's
under test).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.actions.types import Row, Rows
from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.server.app import create_app
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status

ROOT = Path(__file__).resolve().parents[2]
BUNDLED_ROOT = ROOT / "src" / "frisket" / "authoring" / "bundled_plugins"
PLUGIN_DIR = BUNDLED_ROOT / "frisket.transliterate"
PLUGIN_ID = "frisket.transliterate"


@pytest.fixture()
def real_bundled_root(monkeypatch: pytest.MonkeyPatch):
    """Point the bundled-plugins root at the REAL shipped tree, overriding
    the suite-wide hermetic empty root from tests/conftest.py."""
    monkeypatch.setattr(plugin_runtime, "_bundled_plugins_root", lambda: BUNDLED_ROOT)
    monkeypatch.setattr(
        plugin_runtime_status, "_bundled_plugins_root", lambda: BUNDLED_ROOT
    )
    _reset_default_registry_for_tests()
    yield
    _reset_default_registry_for_tests()


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "_test_frisket_transliterate_plugin", PLUGIN_DIR / "plugin.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# 1. Manifest shape.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 2. Engine selection probes REAL importability + construction, not just
# find_spec discoverability.
# --------------------------------------------------------------------------- #


def test_active_engine_matches_the_real_probe_outcome() -> None:
    """`icu_available()` IS the real probe (import + Transliterator
    construction, cached) -- this just confirms `active_engine()` reads it
    honestly, not a redundant `find_spec` recheck. Discoverability alone
    cannot prove the native module constructs successfully."""
    module = _load_plugin_module()
    expected = (
        module.ENGINE_ICU if module.icu_available() else module.ENGINE_TEXT_UNIDECODE
    )
    assert module.active_engine() == expected


# Cyrillic, CJK, Greek, Arabic -- the description's advertised script
# coverage; text_unidecode's known fallback output for each, so the fallback
# assertions below check actual content, not just "some string came back".
_FALLBACK_CASES = [
    ("Москва", "Moskva"),
    ("北京", "Bei Jing "),
    ("Ελλάδα", "Ellada"),
    ("القاهرة", "lqhr@"),
]


@pytest.mark.parametrize("source, expected", _FALLBACK_CASES)
def test_text_unidecode_engine_always_works_with_no_extra(
    source: str, expected: str
) -> None:
    """text_unidecode is a DIRECT base dependency, so this must work with zero
    extras across every script the plugin advertises, not just Cyrillic."""
    module = _load_plugin_module()
    latin, engine_used = module.transliterate(
        source, engine=module.ENGINE_TEXT_UNIDECODE
    )
    assert engine_used == module.ENGINE_TEXT_UNIDECODE
    assert latin == expected
    assert latin.strip(), "fallback must not silently produce an empty string"


@pytest.mark.parametrize("source", ["Москва", "北京", "Ελλάδα"])
def test_icu_engine_when_available_produces_a_nonempty_latin_result(
    source: str,
) -> None:
    pytest.importorskip(
        "icu", reason="PyICU not installed (comes free with the entities extra)"
    )
    module = _load_plugin_module()
    latin, engine_used = module.transliterate(source, engine=module.ENGINE_ICU)
    assert engine_used == module.ENGINE_ICU
    assert latin.isascii()
    assert latin.strip(), (
        "an empty string trivially satisfies isascii() -- assert content, "
        "not just the character-set property"
    )
    assert latin != source


def test_icu_engine_forced_but_unavailable_raises_with_remediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(module, "_icu_transliterator", lambda: None)
    with pytest.raises(ValueError) as excinfo:
        module.transliterate("x", engine=module.ENGINE_ICU)
    assert "frisket-data[entities]" in str(excinfo.value)


def test_unknown_engine_raises() -> None:
    module = _load_plugin_module()
    with pytest.raises(ValueError):
        module.transliterate("x", engine="not-a-real-engine")


def test_discoverable_but_unloadable_icu_falls_back_on_the_default_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `find_spec`-visible `icu` module whose real import/construction fails
    (broken native lib, missing ICU data, ...) must fall back to
    text_unidecode on the AUTOMATIC path, not raise uncaught. Simulated with a
    fake `icu` module injected into sys.modules so this runs identically
    whether or not real PyICU happens to be installed in this env."""
    import types

    fake_icu = types.ModuleType("icu")

    class _BrokenTransliterator:
        @staticmethod
        def createInstance(id):  # noqa: A002 -- matches icu's real signature
            raise RuntimeError("simulated broken native ICU data")

    fake_icu.Transliterator = _BrokenTransliterator
    monkeypatch.setitem(sys.modules, "icu", fake_icu)

    module = _load_plugin_module()
    assert module.icu_available() is False
    assert module.active_engine() == module.ENGINE_TEXT_UNIDECODE

    latin, engine_used = module.transliterate("Москва")  # default/automatic path
    assert engine_used == module.ENGINE_TEXT_UNIDECODE
    assert latin == "Moskva"

    # An explicit force still raises clearly rather than silently downgrading.
    with pytest.raises(ValueError) as excinfo:
        module.transliterate("x", engine=module.ENGINE_ICU)
    assert "frisket-data[entities]" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 3. The row handler.
# --------------------------------------------------------------------------- #


def test_column_handler_returns_only_latin_text() -> None:
    module = _load_plugin_module()
    [result] = module.transliterate_column(
        module.TransliterateParams(text="text"), Rows({1: Row({"text": "café"})})
    ).values()
    assert result.output.latin_text == "cafe"
    assert result.output.model_dump() == {"latin_text": "cafe"}


def test_row_handler_handles_none_input() -> None:
    module = _load_plugin_module()
    [result] = module.transliterate_column(
        module.TransliterateParams(text="text"), Rows({1: Row({"text": None})})
    ).values()
    assert result.output.model_dump() == {"latin_text": None}


# --------------------------------------------------------------------------- #
# 4. Real bootstrap wiring: the plugin installs enabled out of the box.
# --------------------------------------------------------------------------- #


def _client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def test_bundled_transliterate_installs_enabled_via_bundled_path(
    tmp_path: Path, real_bundled_root: None
) -> None:
    client = _client(tmp_path)
    project = client.post("/api/projects", json={"name": "transliterate bundled"})
    assert project.status_code == 200, project.text
    project_id = project.json()["id"]

    index = client.get(f"/api/projects/{project_id}/workbench/plugins")
    assert index.status_code == 200, index.text
    plugins = {item["pluginId"]: item for item in index.json()["plugins"]}
    assert PLUGIN_ID in plugins, (
        "project bootstrap must seed the bundled transliterate plugin through "
        "the public bundled install path"
    )
    entry = plugins[PLUGIN_ID]
    assert entry["installState"] == "enabled"
    assert entry["registryActivated"] is True
    assert entry["source"] == {"kind": "bundled", "value": PLUGIN_ID}


def test_action_uses_the_requested_visible_sheet_and_writes_one_column(
    tmp_path: Path, real_bundled_root: None
) -> None:
    client = _client(tmp_path)
    project_id = client.post(
        "/api/projects", json={"name": "transliterate execution"}
    ).json()["id"]
    project = client.app.state.workspace.get(project_id)
    project.add_sheet("Other")
    sheet_id = project.add_sheet("Names")
    column_id = project.add_column(sheet_id, "name")
    project.add_rows(sheet_id, [{"name": "Москва"}], {"name": column_id})
    request = {
        "action_id": "frisket.transliterate.transliterate",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"text": "name"},
        "idempotency_key": "transliterate-visible-sheet",
    }

    validation = client.post(
        f"/api/projects/{project_id}/actions/v1/validate-params",
        json={"action": request},
    )
    assert validation.status_code == 200, validation.text
    assert validation.json()["logical_outputs"] == [
        {"key": "latin_text", "column_type": "text"}
    ]

    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=request)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed"
    assert [(output["name"], output["sheet_id"]) for output in result["outputs"]] == [
        ("latin_text", sheet_id)
    ]
    assert project.get_values(sheet_id, result["outputs"][0]["column_id"]) == {
        1: "Moskva"
    }
