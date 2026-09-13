"""Engine-selectable OCR over image and PDF columns.

- light engine (rapidocr, onnx-class) runs offline in the base env
- PDFs go rasterize-then-OCR (PDFium), per-page blocks preserved
- engine param resolves local-light → sidecar (dots.mocr) → remote VLM (router)
"""

import asyncio
import base64
import mimetypes
from pathlib import Path
import shutil
import subprocess
import sys

import cv2
import numpy as np
import pytest

from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.engine.executor import run_action_spec
from frisket.engine.store import Project
from frisket.ops import ocr_engines as ocr_module
from frisket.ops.ocr_engines import (
    RAPIDOCR_MODELS_NOT_PROVISIONED,
    rapidocr_models_present,
)

from helpers import requires_ocr_runtime


def _render_png(path, text):
    img = np.full((120, 640, 3), 255, dtype=np.uint8)
    cv2.putText(img, text, (20, 70), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (0, 0, 0), 3)
    cv2.imwrite(str(path), img)
    return path


def _project(tmp_path, rows, coltype="image"):
    p = Project.create(tmp_path / "t.frisket", name="t")
    sheet = p.add_sheet("data")
    cols = {k: p.add_column(sheet, k, type=coltype) for k in rows[0]}
    for row in rows:
        for key, value in row.items():
            path = Path(value)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            digest = p.add_blob(path.read_bytes(), filename=path.name, mime=mime)
            row[key] = {"blob": digest, "filename": path.name, "mime": mime}
    p.add_rows(sheet, rows, cols)
    return p, sheet


def _spec(sheet, engine=None, **extra):
    return {
        "action_id": "media.ocr",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"source": "page", "engine": engine or "rapidocr", **extra},
        "output_names": {"text": "ocr_text", "blocks": "ocr_text_blocks"},
        "idempotency_key": "ocr-engine-test",
    }


def _col_value(p, sheet, name):
    col = next(c for c in p.columns(sheet) if c["name"] == name)
    (val,) = p.get_values(sheet, col["id"]).values()
    return val


def _require_nested_macos_sandbox() -> None:
    """Skip installed-runtime probes when the host forbids nested seatbelts."""

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


def _execute(project, spec, router=None, *, confirm=True):
    result = run_action_spec(project, spec, project_id="ocr-test", router=router)
    if confirm and result.status == "needs_confirmation":
        spec = {**spec, "confirmation": result.errors[0].details["promise_set_hash"]}
        result = run_action_spec(project, spec, project_id="ocr-test", router=router)
    return result


def test_ocr_image_light_engine_with_blocks(tmp_path):
    """The default light engine reads a synthetic PNG offline and emits
    per-page blocks with bbox geometry (feeds the bbox renderer)."""
    if not rapidocr_models_present()[0]:
        pytest.skip(RAPIDOCR_MODELS_NOT_PROVISIONED)
    _require_nested_macos_sandbox()
    path = _render_png(tmp_path / "scan.png", "FRISKET OCR 4271")
    p, sheet = _project(tmp_path, [{"page": str(path)}])
    prog = _execute(p, _spec(sheet), ModelRouter(cache=None, cache_mode="off"))
    assert prog.status == "completed", prog.errors
    text = _col_value(p, sheet, "ocr_text")
    assert "FRISKET" in text and "4271" in text
    pages = _col_value(p, sheet, "ocr_text_blocks")
    assert pages[0]["page"] == 1 and pages[0]["engine"] == "rapidocr"
    block = pages[0]["blocks"][0]
    assert block["text"] and len(block["bbox"]) == 4 and block["score"] > 0
    p.close()


def test_ocr_light_language_rides_worker_payload(tmp_path, monkeypatch):
    """spec language reaches the run-scoped pool's fixed worker config, where
    it becomes RapidOCR's Rec.lang_type."""
    from frisket.ops import ocr_engines_local as ocr_local

    captured = {}

    class FakePool:
        teardown_failed = False

        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def ocr(self, paths):
            return [{"text": "BONJOUR", "blocks": []}]

        async def close(self):
            return None

    class FakeLease:
        def release(self):
            return None

        def poison(self):
            return None

    monkeypatch.setattr(ocr_local, "RapidOCRProcessPool", FakePool)
    monkeypatch.setattr(
        ocr_local, "_rapidocr_model_root_dir", lambda language=None: None
    )
    monkeypatch.setattr(
        ocr_local, "acquire_local_engine_lease", lambda engine: FakeLease()
    )
    path = _render_png(tmp_path / "scan.png", "BONJOUR")
    p, sheet = _project(tmp_path, [{"page": str(path)}])
    prog = _execute(
        p, _spec(sheet, language="latin"), ModelRouter(cache=None, cache_mode="off")
    )
    assert prog.status == "completed", prog.errors
    assert captured["language"] == "latin"
    assert _col_value(p, sheet, "ocr_text") == "BONJOUR"
    p.close()


def test_ocr_light_language_constructs_real_engine(tmp_path):
    """language='ch' exercises the real RapidOCR(params={'Rec.lang_type': …})
    construction offline (the ch model ships in the wheel — no download)."""
    if not rapidocr_models_present()[0]:
        pytest.skip(RAPIDOCR_MODELS_NOT_PROVISIONED)
    _require_nested_macos_sandbox()
    path = _render_png(tmp_path / "scan.png", "FRISKET 4271")
    p, sheet = _project(tmp_path, [{"page": str(path)}])
    prog = _execute(
        p, _spec(sheet, language="ch"), ModelRouter(cache=None, cache_mode="off")
    )
    assert prog.status == "completed", prog.errors
    text = _col_value(p, sheet, "ocr_text")
    assert "FRISKET" in text and "4271" in text
    p.close()


@requires_ocr_runtime
def test_ocr_light_unknown_language_fails_per_row(tmp_path):
    """A bad language code is no silent no-op: the worker validates against
    rapidocr's lang enum and fails the row listing the valid codes.

    Unlike the other light-engine tests above, this one has no
    `rapidocr_models_present()` guard of its own to fall back on: the
    validation error it asserts on is raised inside the real sandboxed
    worker subprocess (`_language_param` imports `rapidocr.utils.typings`),
    so without the runtime this fails with a generic load error instead of
    skipping cleanly.
    """
    _require_nested_macos_sandbox()
    path = _render_png(tmp_path / "scan.png", "HELLO")
    p, sheet = _project(tmp_path, [{"page": str(path)}])
    prog = _execute(
        p, _spec(sheet, language="klingon"), ModelRouter(cache=None, cache_mode="off")
    )
    assert prog.status == "failed", prog.errors
    err = p.db.execute(
        "SELECT error FROM results WHERE run_id=? AND error IS NOT NULL", (prog.run_id,)
    ).fetchone()["error"]
    assert "klingon" in err and "japan" in err  # lists the valid codes
    p.close()


def test_ocr_pdf_rasterize_then_ocr(tmp_path):
    """A two-page PDF rasterizes via PDFium and OCRs every page; the text
    column joins pages, the blocks column stays per-page."""
    if not rapidocr_models_present()[0]:
        pytest.skip(RAPIDOCR_MODELS_NOT_PROVISIONED)
    _require_nested_macos_sandbox()
    from PIL import Image

    p1 = _render_png(tmp_path / "p1.png", "ALPHA 1111")
    p2 = _render_png(tmp_path / "p2.png", "BRAVO 2222")
    pdf = tmp_path / "scan.pdf"
    # bilevel pages → CCITT in-PDF encoding (no JPEG plugin needed)
    Image.open(p1).convert("1").save(
        pdf, save_all=True, append_images=[Image.open(p2).convert("1")], resolution=72
    )

    p, sheet = _project(tmp_path, [{"page": str(pdf)}], coltype="file")
    prog = _execute(p, _spec(sheet), ModelRouter(cache=None, cache_mode="off"))
    assert prog.status == "completed", prog.errors
    text = _col_value(p, sheet, "ocr_text")
    assert "ALPHA" in text and "1111" in text
    assert "BRAVO" in text and "2222" in text
    pages = _col_value(p, sheet, "ocr_text_blocks")
    assert [pg["page"] for pg in pages] == [1, 2]
    assert any("ALPHA" in b["text"] for b in pages[0]["blocks"])
    assert any("BRAVO" in b["text"] for b in pages[1]["blocks"])
    p.close()


def test_ocr_unknown_engine_refuses_the_run(tmp_path):
    """An unknown OCR engine is a RESOLUTION refusal naming the symbol,
    not one identical per-row error per selected row — the OCR roster is what
    canonicalizes the authored symbol now, and it answers before dispatch."""
    path = _render_png(tmp_path / "scan.png", "HELLO")
    p, sheet = _project(tmp_path, [{"page": str(path)}])
    result = _execute(p, _spec(sheet, engine="nonsense"), confirm=False)
    assert result.status == "failed", result.errors
    assert "nonsense" in str(result.errors)
    p.close()


def test_ocr_dots_mocr_needs_sidecar_url(tmp_path, monkeypatch):
    """The quality engine lives in the frisket-models sidecar; without
    FRISKET_MODELS_URL the run is refused with a pointer, not a crash.

    routed OCR moved WHEN this is noticed: the gateway target's liveness is probed at
    resolution, so an unconfigured sidecar refuses ``no_live_target`` before
    any page is rendered instead of failing every row with the same message.
    """
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    path = _render_png(tmp_path / "scan.png", "HELLO")
    p, sheet = _project(tmp_path, [{"page": str(path)}])
    result = _execute(p, _spec(sheet, engine="dots.mocr"), confirm=False)
    assert result.status == "failed", result.errors
    assert "FRISKET_MODELS_URL" in str(result.errors)
    p.close()


def test_ocr_sidecar_posts_pages_with_bearer(tmp_path, monkeypatch):
    """engine='dots.mocr' speaks the settled sidecar contract: POST /ocr with
    multipart page bytes + bearer token; per-page text/blocks come back."""
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

    class StubRouter:
        client = FakeHttp()

    router = StubRouter()
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:9000")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")

    path = _render_png(tmp_path / "scan.png", "HELLO")
    p, sheet = _project(tmp_path, [{"page": str(path)}])
    prog = _execute(p, _spec(sheet, engine="dots.mocr"), router)
    assert prog.status == "completed", prog.errors
    assert captured["url"] == "http://models:9000/ocr"
    assert captured["headers"]["Authorization"] == "Bearer sekrit"
    assert captured["follow_redirects"] is True
    assert captured["timeout"].connect == 10.0
    assert captured["timeout"].read == 3600.0
    assert captured["data"]["engine"] == "dots.mocr"
    # dots.mocr auto-detects; the settled /ocr contract carries no language
    assert "language" not in captured["data"]
    assert captured["files"][0][0] == "files"  # multipart page bytes
    assert _col_value(p, sheet, "ocr_text") == "FROM SIDECAR"
    pages = _col_value(p, sheet, "ocr_text_blocks")
    assert pages[0]["engine"] == "dots.mocr" and pages[0]["blocks"]
    p.close()


def test_ocr_remote_vlm_engine_rides_router(tmp_path, monkeypatch):
    """A provider/model engine id routes per-page image parts through the
    model router with a structured {text} schema."""
    seen = {}

    class StubRouter:
        client = None

        def credential_source_for(self, provider):
            # A faithful double: the real router reports the provenance of the
            # key it selected, and routed OCR's pre-effect credential fence refuses a
            # dispatch whose provenance it cannot read.
            return "local"

        def secret_values_for_model(self, _model):  # noqa: ANN001
            return ()

        def note_schema_reject(self, _provider):  # noqa: ANN001
            return None

        async def complete(
            self,
            req,
            *,
            recipe_version="1",
            trace=None,  # noqa: ANN001
        ):
            return await self.complete_transport(
                req, recipe_version=recipe_version, trace=trace
            )

        async def complete_transport(
            self,
            req,
            *,
            recipe_version="1",
            trace=None,  # noqa: ANN001
        ):
            seen["model"] = req.model
            parts = req.messages[-1]["content"]
            seen["has_image"] = any(p.get("type") == "image" for p in parts)
            seen["ask"] = next(p["text"] for p in parts if p.get("type") == "text")
            seen["schema"] = req.schema
            return LLMResponse(
                content=None,
                data={"text": "VLM READ 9999"},
                tokens_in=10,
                tokens_out=5,
                cost=0.0,
                model=req.model,
                duration_ms=321,
            )

    path = _render_png(tmp_path / "scan.png", "HELLO")
    p, sheet = _project(tmp_path, [{"page": str(path)}])
    # remote/VLM engines estimate unknown cost (None), so the cost gate asks
    # unless confirmed (tests/test_remote_engine_estimate_honesty.py pins
    # the estimate itself; this test is about the VLM call shape).
    monkeypatch.setenv("GEMINI_API_KEY", "key")  # the venue must be live
    prog = _execute(p, _spec(sheet, engine="gemini/gemini-2.5-flash"), StubRouter())
    assert prog.status == "completed", prog.errors
    assert seen["model"] == "gemini/gemini-2.5-flash"
    assert seen["has_image"] and "text" in seen["schema"]["properties"]
    assert "text is in" not in seen["ask"]  # no spec language → no hint
    assert _col_value(p, sheet, "ocr_text") == "VLM READ 9999"
    # The wire response's measured duration must land on the stored fact —
    # not a fabricated NULL for a live call.
    from frisket.engine.store.runs import RunResultStore

    run_id = int(p.db.execute("SELECT MAX(id) AS id FROM runs").fetchone()["id"])
    model_calls = RunResultStore(p).model_calls(run_id)
    assert len(model_calls) == 1
    assert model_calls[0]["duration_ms"] == 321
    p.close()


def test_minimax_m3_ocr_uses_publisher_request_profile(tmp_path):
    """MiniMax's curated OCR row uses its published sampling and image limit."""
    seen = {}

    class StubRouter:
        client = None

        def secret_values_for_model(self, _model):  # noqa: ANN001
            return ()

        def note_schema_reject(self, _provider):  # noqa: ANN001
            return None

        async def complete_transport(
            self,
            req,
            *,
            recipe_version="1",
            trace=None,  # noqa: ANN001
        ):
            del recipe_version, trace
            seen["request"] = req
            seen["part_types"] = [part["type"] for part in req.messages[-1]["content"]]
            image_part = next(
                part
                for part in req.messages[-1]["content"]
                if part.get("type") == "image"
            )
            image = cv2.imdecode(
                np.frombuffer(base64.b64decode(image_part["data"]), dtype=np.uint8),
                cv2.IMREAD_COLOR,
            )
            seen["shape"] = image.shape[:2]
            return LLMResponse(
                content=None,
                data={"text": "MINIMAX READ"},
                tokens_in=10,
                tokens_out=5,
                cost=0.001,
                model=req.model,
                duration_ms=123,
            )

    image = np.full((400, 4000, 3), 255, dtype=np.uint8)
    path = tmp_path / "wide.png"
    cv2.imwrite(str(path), image)
    usage = {"calls": 0, "in": 0, "out": 0, "cost": 0.0, "duration_ms": 0}
    result = asyncio.run(
        ocr_module.OcrEngines()._ocr_vlm(
            "openrouter/minimax/minimax-m3",
            [path],
            ocr_module.OpContext(http=None, extras={"router": StubRouter()}),
            usage,
        )
    )

    req = seen["request"]
    assert req.temperature == 1.0
    assert req.params == {"top_p": 0.95}
    assert req.reasoning_policy == "disabled"
    assert seen["part_types"] == ["text", "image"]
    assert max(seen["shape"]) == 3584
    assert result == [{"text": "MINIMAX READ", "blocks": []}]


def test_ocr_vlm_language_hint_reaches_prompt(tmp_path, monkeypatch):
    """spec language rides the VLM user turn as a hint (free-text — any
    phrasing the user gives goes straight into the prompt)."""
    seen = {}

    class StubRouter:
        client = None

        def credential_source_for(self, provider):
            # A faithful double: the real router reports the provenance of the
            # key it selected, and routed OCR's pre-effect credential fence refuses a
            # dispatch whose provenance it cannot read.
            return "local"

        def secret_values_for_model(self, _model):  # noqa: ANN001
            return ()

        def note_schema_reject(self, _provider):  # noqa: ANN001
            return None

        async def complete(
            self,
            req,
            *,
            recipe_version="1",
            trace=None,  # noqa: ANN001
        ):
            return await self.complete_transport(
                req, recipe_version=recipe_version, trace=trace
            )

        async def complete_transport(
            self,
            req,
            *,
            recipe_version="1",
            trace=None,  # noqa: ANN001
        ):
            parts = req.messages[-1]["content"]
            seen["ask"] = next(p["text"] for p in parts if p.get("type") == "text")
            return LLMResponse(
                content=None,
                data={"text": "OLA"},
                tokens_in=10,
                tokens_out=5,
                cost=0.0,
                model=req.model,
            )

    path = _render_png(tmp_path / "scan.png", "OLA")
    p, sheet = _project(tmp_path, [{"page": str(path)}])
    # remote/VLM engines estimate unknown cost (None) — confirm the gate.
    monkeypatch.setenv("GEMINI_API_KEY", "key")  # the venue must be live
    prog = _execute(
        p,
        _spec(sheet, engine="gemini/gemini-2.5-flash", language="Brazilian Portuguese"),
        StubRouter(),
    )
    assert prog.status == "completed", prog.errors
    assert "The text is in Brazilian Portuguese." in seen["ask"]
    assert _col_value(p, sheet, "ocr_text") == "OLA"
    p.close()
