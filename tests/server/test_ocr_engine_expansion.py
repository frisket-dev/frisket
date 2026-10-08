from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.ops.ocr_engines import (
    ENGINE_ALIASES,
    LOCAL_ENGINES,
    SIDECAR_ENGINES,
    rapidocr_available,
    tesseract_available,
)
from frisket.engine.executor import run_action_spec
from frisket.actions.media import OcrParams
from frisket.server.action_catalog_hints import (
    project_action_catalog_launcher_hints,
)
from frisket.engine.store import Project
from frisket.server.app import create_app

_PYTESSERACT = importlib.util.find_spec("pytesseract") is not None
_UNCONFIGURED_SIDECAR = {
    "configured": False,
    "available": False,
    "engines": [],
    "error": None,
}


def _project(tmp_path, rows, coltype="image"):
    p = Project.create(tmp_path / "t.frisket", name="t")
    sheet = p.add_sheet("data")
    cols = {k: p.add_column(sheet, k, type=coltype) for k in rows[0]}
    materialized = []
    for row in rows:
        values = {}
        for name, path in row.items():
            digest = p.add_blob(
                Path(path).read_bytes(), filename=Path(path).name, mime="image/png"
            )
            values[name] = {
                "blob": digest,
                "filename": Path(path).name,
                "mime": "image/png",
            }
        materialized.append(values)
    p.add_rows(sheet, materialized, cols)
    return p, sheet


def _spec(sheet, engine):
    return {
        "action_id": "media.ocr",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"source": "page", "engine": engine},
        "output_names": {"text": "ocr_text", "blocks": "ocr_text_blocks"},
        "idempotency_key": "ocr-expansion",
    }


def _ocr_engines(sidecar_capabilities=_UNCONFIGURED_SIDECAR):
    hints = project_action_catalog_launcher_hints(sidecar_capabilities)
    return hints["media.ocr"]["engines"]


def _by_id(engines, engine_id):
    return next(e for e in engines if e["id"] == engine_id)


def _require_nested_macos_sandbox() -> None:
    sandbox_exec = shutil.which("sandbox-exec")
    if sys.platform != "darwin" or sandbox_exec is None:
        return
    probe = subprocess.run(  # noqa: S603 - fixed local capability probe
        [sandbox_exec, "-p", "(version 1)(allow default)", "/usr/bin/true"],
        capture_output=True,
        text=True,
        check=False,
    )
    if (
        probe.returncode != 0
        and "sandbox_apply: Operation not permitted" in probe.stderr
    ):
        pytest.skip("host forbids the nested macOS sandbox used by OCR workers")


def _run(project, spec):
    result = run_action_spec(project, spec, project_id="ocr-expansion")
    if result.status == "needs_confirmation":
        spec["confirmation"] = result.errors[0].details["promise_set_hash"]
        result = run_action_spec(project, spec, project_id="ocr-expansion")
    return result


# ---------- tesseract: local tier, binary + wrapper honesty ----------


def test_rapidocr_catalog_matches_exact_runtime_probe():
    rapidocr_available.cache_clear()
    available, error = rapidocr_available()
    rapid = _by_id(_ocr_engines(), "rapidocr")
    assert rapid["available"] is available
    if available:
        assert error is None
        assert "error" not in rapid
    else:
        assert error and "RapidOCR" in error and "ocr" in error.casefold()
        assert rapid["error"] == error


def test_tesseract_availability_needs_binary_and_wrapper():
    """Tesseract is a system binary (brew/apt), not a pip package — so
    availability must check BOTH the binary and the pytesseract wrapper, and
    the enablement hint must name whatever is missing."""
    available, error = tesseract_available()
    assert isinstance(available, bool)
    if not available:
        # dep-less env: the wrapper is not installed → honest False + a hint
        # that names how to enable it (never a bare/opaque unavailable).
        assert error
        assert "tesseract" in error.lower()
    else:
        assert error is None


def test_tesseract_is_a_recognized_local_free_engine(tmp_path):
    """The engine resolves as a LOCAL engine and estimates free (self-hosted
    compute, no per-request meter — matches rapidocr/dots.mocr, no fabricated
    cost)."""
    assert "tesseract" in LOCAL_ENGINES
    assert ENGINE_ALIASES.get("tess") == "tesseract"
    with TestClient(
        create_app(
            tmp_path / "workspace", router=ModelRouter(cache=None, cache_mode="off")
        )
    ) as client:
        pid = client.post("/api/projects", json={"name": "OCR estimate"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("Pages")
        column = project.add_column(sheet, "page", type="image")
        digest = project.add_blob(
            b"scan fixture", filename="scan.png", mime="image/png"
        )
        project.add_rows(
            sheet, [{"page": {"blob": digest, "mime": "image/png"}}], {"page": column}
        )
        response = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": _spec(sheet, "tesseract")},
        )
        assert response.status_code == 200, response.text
        est = response.json()["estimate"]
        assert est["rows"] == 1
        assert est["cost"] == 0.0
        assert est["cost_source"] == "free_local"


def test_tesseract_run_without_wrapper_fails_with_pointer(tmp_path):
    """With the wrapper absent the row fails with an enablement pointer, not a
    crash (the honest-unavailable path; only asserted when pytesseract is not
    installed — the dep-less state this run targets)."""
    if _PYTESSERACT and tesseract_available()[0]:
        pytest.skip("tesseract wrapper installed — installed-state, covered below")
    dummy = tmp_path / "scan.png"
    dummy.write_bytes(b"\x89PNG\r\n\x1a\n not-a-real-image")
    p, sheet = _project(tmp_path, [{"page": str(dummy)}])
    prog = _run(p, _spec(sheet, "tesseract"))
    assert prog.status == "failed", prog.errors
    err = p.db.execute(
        "SELECT error FROM results WHERE run_id=? AND error IS NOT NULL",
        (prog.run_id,),
    ).fetchone()["error"]
    assert "tesseract" in err.lower()
    p.close()


def test_tesseract_catalog_hint_entry_present():
    """A tesseract engine reaches both UI surfaces via the catalog hint (zero
    web/src edits — the no-drift design). Its availability mirrors the honest
    binary+wrapper probe. dots.mocr remains at the roster's pinned index 3."""
    engines = _ocr_engines()
    ids = [e["id"] for e in engines]
    assert ids[3] == "dots.mocr"  # parity guard — do not shift dots.mocr
    tess = _by_id(engines, "tesseract")
    assert tess["tier"] == "local"
    assert tess["billable"] is False
    assert tess["language"] == {
        "mode": "fixed",
        "default": "en",
        "fixed_language": "en",
        "choices": [{"value": "en", "label": "English"}],
        "detects": False,
    }
    available, error = tesseract_available()
    assert tess["available"] == available
    if not available:
        assert tess["error"]


# ---------- local/team sidecar OCR engines ----------


def test_public_sidecar_ocr_engines_are_registered():
    """Every self-hosted document model routes to frisket-models ``/ocr``."""
    assert {
        "dots.mocr",
        "glm-ocr",
        "surya2",
        "paddleocr-vl",
        "pp-ocrv6",
    } <= SIDECAR_ENGINES


def _routes_to_sidecar(tmp_path, monkeypatch, engine):
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "pages": [
                    {
                        "text": "FROM SIDECAR",
                        "blocks": [
                            {
                                "text": "FROM SIDECAR",
                                "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]],
                            }
                        ],
                    }
                ]
            }

    class FakeHttp:
        async def post(
            self,
            url,
            files=None,
            data=None,
            headers=None,
            follow_redirects=False,
            timeout=None,
        ):
            captured.update(
                url=url,
                files=files,
                data=data,
                headers=headers,
                follow_redirects=follow_redirects,
                timeout=timeout,
            )
            return FakeResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", FakeHttp.post)

    page = tmp_path / f"{engine.replace('/', '_')}.png"
    page.write_bytes(b"\x89PNG\r\n\x1a\n page-bytes")
    p, sheet = _project(tmp_path, [{"page": str(page)}])
    prog = _run(p, _spec(sheet, engine))
    p.close()
    return prog, captured


def test_paddleocr_vl_routes_to_sidecar_ocr(tmp_path, monkeypatch):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:9000")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    prog, captured = _routes_to_sidecar(tmp_path, monkeypatch, "paddleocr-vl")
    assert prog.status == "completed", prog.errors
    assert captured["url"] == "http://models:9000/ocr"
    assert captured["headers"]["Authorization"] == "Bearer sekrit"
    assert captured["follow_redirects"] is True
    assert captured["data"]["engine"] == "paddleocr-vl"


def test_pp_ocrv6_routes_to_sidecar_ocr(tmp_path, monkeypatch):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:9000")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    prog, captured = _routes_to_sidecar(tmp_path, monkeypatch, "pp-ocrv6")
    assert prog.status == "completed", prog.errors
    assert captured["url"] == "http://models:9000/ocr"
    assert captured["headers"]["Authorization"] == "Bearer sekrit"
    assert captured["follow_redirects"] is True
    assert captured["data"]["engine"] == "pp-ocrv6"


def test_dots_mocr_routes_to_sidecar_ocr(tmp_path, monkeypatch):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:9000")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    prog, captured = _routes_to_sidecar(tmp_path, monkeypatch, "dots.mocr")
    assert prog.status == "completed", prog.errors
    assert captured["url"] == "http://models:9000/ocr"
    assert captured["follow_redirects"] is True
    assert captured["data"]["engine"] == "dots.mocr"


def test_glm_ocr_routes_to_sidecar_ocr(tmp_path, monkeypatch):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:9000")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    prog, captured = _routes_to_sidecar(tmp_path, monkeypatch, "glm-ocr")
    assert prog.status == "completed", prog.errors
    assert captured["url"] == "http://models:9000/ocr"
    assert captured["follow_redirects"] is True
    assert captured["data"]["engine"] == "glm-ocr"


def test_surya2_routes_to_sidecar_ocr(tmp_path, monkeypatch):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:9000")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    prog, captured = _routes_to_sidecar(tmp_path, monkeypatch, "surya2")
    assert prog.status == "completed", prog.errors
    assert captured["url"] == "http://models:9000/ocr"
    assert captured["follow_redirects"] is True
    assert captured["data"]["engine"] == "surya2"


def test_public_sidecar_ocr_catalog_hint_entries_present():
    engines = _ocr_engines()
    for engine_id in (
        "paddleocr-vl",
        "dots.mocr",
        "glm-ocr",
        "pp-ocrv6",
        "surya2",
    ):
        entry = _by_id(engines, engine_id)
        assert entry["tier"] == "sidecar"
        # unconfigured sidecar → unavailable with the configure pointer
        assert entry["available"] is False
        assert entry["error"]


# ---------- installed-state (UNRUN dep-less — skips until wrapper present) ----------


def test_tesseract_reads_fixture_real(tmp_path):
    """INSTALLED-STATE (skips without the wrapper): tesseract OCRs a rendered
    PNG and returns text + 4-corner bbox blocks. Un-gap by installing the ocr
    extra (pytesseract) + the tesseract binary — the post-landing action."""
    pytest.importorskip("pytesseract")
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    if not tesseract_available()[0]:
        pytest.skip("tesseract binary not on PATH")
    _require_nested_macos_sandbox()
    img = np.full((120, 640, 3), 255, dtype=np.uint8)
    cv2.putText(
        img, "HELLO FRISKET", (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 3
    )
    path = tmp_path / "scan.png"
    cv2.imwrite(str(path), img)
    p, sheet = _project(tmp_path, [{"page": str(path)}])
    prog = _run(p, _spec(sheet, "tesseract"))
    assert prog.status == "completed", prog.errors
    col = next(c for c in p.columns(sheet) if c["name"] == "ocr_text")
    (text,) = p.get_values(sheet, col["id"]).values()
    assert "HELLO" in text.upper()
    blocks_col = next(c for c in p.columns(sheet) if c["name"] == "ocr_text_blocks")
    (pages,) = p.get_values(sheet, blocks_col["id"]).values()
    assert pages[0]["engine"] == "tesseract"
    for block in pages[0]["blocks"]:
        if block.get("bbox"):
            assert len(block["bbox"]) == 4
    p.close()


# ---------- remote VLM engine availability matches execution ----------------


def test_remote_ocr_engines_consult_project_keys_not_just_env(monkeypatch, tmp_path):
    """Env-only availability made the remote OCR engines lie 'unavailable' to
    users whose OpenAI/Gemini key lives in Settings → AI Providers rather
    than a shell env var. The
    catalog must resolve keys from the same three sources execution uses."""
    from frisket.server.action_catalog_hints import _recipe_engines
    from frisket.server import provider_config

    for env_name in provider_config.ENV_VAR.values():
        monkeypatch.delenv(env_name, raising=False)

    def by_id(project):
        return {e["id"]: e for e in _recipe_engines("media.ocr", {}, project=project)}

    # no env, no project: both remote engines unavailable with a hint that
    # names BOTH configuration paths
    engines = by_id(None)
    assert engines["openai/gpt-4.1-mini"]["available"] is False
    assert "Settings" in engines["openai/gpt-4.1-mini"]["error"]
    assert engines["gemini/gemini-3.5-flash"]["available"] is False
    assert engines["openrouter/minimax/minimax-m3"]["available"] is False

    class _ProjectWithKeys:
        path = tmp_path / "proj.frisket"

        def provider_model_keys(self):
            return {
                "openai": "sk-proj",
                "gemini": "gk-proj",
                "openrouter": "or-proj",
            }

    engines = by_id(_ProjectWithKeys())
    assert engines["openai/gpt-4.1-mini"]["available"] is True
    assert engines["gemini/gemini-3.5-flash"]["available"] is True
    assert engines["openrouter/minimax/minimax-m3"]["available"] is True

    # env-only still works too (parity with the old behavior)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    engines = by_id(None)
    assert engines["openai/gpt-4.1-mini"]["available"] is True
    assert engines["gemini/gemini-3.5-flash"]["available"] is False
    assert engines["openrouter/minimax/minimax-m3"]["available"] is False


# ---------- runtime <-> typed params <-> catalog engine acceptance ----------


def _ops_declared_engines():
    from frisket.ops.ocr_engines import DATALAB_ENGINE

    return LOCAL_ENGINES | SIDECAR_ENGINES | {DATALAB_ENGINE} | set(ENGINE_ALIASES)


def test_ops_engine_roster_equals_the_contract_symbolic_set():
    """Every engine id + alias the OCR helpers declare is accepted by the contract
    set, and the contract accepts no symbolic engine ops does not implement.
    Pins the drift class behind the invalid_ocr_engine survey bug: tesseract,
    paddleocr-vl, and their aliases were implemented and catalog-advertised
    but rejected by the request params."""
    from frisket.contracts.action import OCR_SYMBOLIC_ENGINES

    assert _ops_declared_engines() == OCR_SYMBOLIC_ENGINES


def test_every_ops_declared_ocr_engine_validates_through_the_params_model():
    for engine in sorted(_ops_declared_engines()):
        params = OcrParams.model_validate({"source": "page", "engine": engine})
        assert params.engine.root == engine


def test_every_catalog_advertised_ocr_engine_validates_through_the_params_model():
    """The picker renders the catalog roster, so every advertised id — local,
    sidecar, hosted, and remote provider/model — must clear contract
    validation rather than bounce with invalid_ocr_engine."""
    for entry in _ocr_engines():
        params = OcrParams.model_validate({"source": "page", "engine": entry["id"]})
        assert params.engine.root == entry["id"]


def test_catalog_roster_carries_every_listed_table_engine_with_its_label():
    """The catalog assembly (availability probes, ordering, VLM extras) is
    hand-built per engine — pin that every listed OCR_ENGINE_TABLE entry
    reaches the catalog and carries the table's label, so a table edit can
    never silently miss the picker."""
    from frisket.contracts.actions.schemas._engines import OCR_ENGINE_TABLE

    by_id = {e["id"]: e for e in _ocr_engines()}
    for decl in OCR_ENGINE_TABLE:
        if not decl.listed:
            continue
        assert decl.id in by_id, f"table engine {decl.id!r} missing from catalog"
        assert by_id[decl.id]["label"] == decl.label
