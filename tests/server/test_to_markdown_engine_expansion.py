"""Chandra 2 (Datalab, github.com/datalab-to/chandra) joins docling as a
second to_markdown sidecar engine. It uses the same /to-markdown contract as
docling (POST multipart blob bytes + bearer, engine='chandra');
experimental, no quality claim in copy (Hy-MT2 precedent). Mirrors
test_ocr_engine_expansion.py's pattern for paddleocr-vl joining dots.mocr."""

from __future__ import annotations

import pytest

from frisket.ai.llm import ModelRouter
from frisket.actions.registry import ACTION_REGISTRY
from frisket.contracts.actions.schemas._engines import (
    TO_MARKDOWN_ENGINE_TABLE,
    engine_ids,
)
from frisket.engine.executor.actions import run_action_spec
from frisket.server.action_catalog_hints import (
    project_action_catalog_launcher_hints,
)
from frisket.engine.store import Project

SIDECAR_ENGINES = set(engine_ids(TO_MARKDOWN_ENGINE_TABLE, tier="sidecar"))

_UNCONFIGURED_SIDECAR = {
    "configured": False,
    "available": False,
    "engines": [],
    "error": None,
}


def _project(tmp_path, rows, coltype="text"):
    p = Project.create(tmp_path / "t.frisket", name="t")
    sheet = p.add_sheet("data")
    cols = {k: p.add_column(sheet, k, type=coltype) for k in rows[0]}
    p.add_rows(sheet, rows, cols)
    return p, sheet


def _spec(sheet, engine):
    return {
        "action_id": "media.to_markdown",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet},
        "params": {"source": "doc", "engine": engine},
        "output_names": {"markdown": "markdown", "ocr_used": "markdown_ocr_used"}
        if engine in SIDECAR_ENGINES
        else {"markdown": "markdown"},
        "idempotency_key": "markdown-engine-proof",
    }


def _to_markdown_engines(sidecar_capabilities=_UNCONFIGURED_SIDECAR):
    hints = project_action_catalog_launcher_hints(sidecar_capabilities)
    return hints["media.to_markdown"]["engines"]


def _by_id(engines, engine_id):
    return next(e for e in engines if e["id"] == engine_id)


def test_trafilatura_is_present_in_the_served_picker():
    assert _by_id(_to_markdown_engines(), "trafilatura_html")


def _run_with_exact_confirmation(project, spec: dict, router):
    result = run_action_spec(project, spec, project_id="t", router=router)
    if result.status == "needs_confirmation":
        spec = {**spec, "confirmation": result.errors[0].details["promise_set_hash"]}
        result = run_action_spec(project, spec, project_id="t", router=router)
    return result


# ---------- chandra is a sidecar engine alongside docling ----------


def test_chandra_and_docling_are_sidecar_engines():
    assert "docling" in SIDECAR_ENGINES
    assert "chandra" in SIDECAR_ENGINES


def test_chandra_needs_sidecar_url(tmp_path, monkeypatch):
    """Same as docling: the sidecar target's liveness is probed at
    RESOLUTION (Phase 4, mirroring ``test_ocr_dots_mocr_needs_sidecar_url``), so
    without FRISKET_MODELS_URL the run refuses ``no_live_target`` before any
    document is read, instead of failing every row with the same message."""
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    p, sheet = _project(tmp_path, [{"doc": "<html><body>hi</body></html>"}])
    router = ModelRouter(cache=None, cache_mode="off")
    refusal = _run_with_exact_confirmation(p, _spec(sheet, "chandra"), router)
    assert refusal.status == "failed", refusal.errors
    assert "FRISKET_MODELS_URL" in str(refusal.errors)
    assert p.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    p.close()


def test_chandra_routes_to_sidecar_to_markdown(tmp_path, monkeypatch):
    """engine='chandra' speaks the settled /to-markdown contract: POST
    multipart blob bytes + bearer token, same as docling."""
    captured = {}

    class FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "documents": [{"markdown": "# From Chandra", "ocr_used": [True, True]}]
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

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models:9000")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "sekrit")
    monkeypatch.setenv("FRISKET_TRANSCRIPTION_SIDECAR_TIMEOUT_SECONDS", "47")

    p, sheet = _project(tmp_path, [{"doc": "<html><body>hi</body></html>"}])
    result = _run_with_exact_confirmation(p, _spec(sheet, "chandra"), StubRouter())
    assert result.status == "completed", result.errors
    assert captured["url"] == "http://models:9000/to-markdown"
    assert captured["headers"]["Authorization"] == "Bearer sekrit"
    assert captured["timeout"].connect == 10.0
    assert captured["timeout"].read == 47.0
    assert captured["timeout"].write == 47.0
    assert captured["timeout"].pool == 47.0
    assert captured["data"]["engine"] == "chandra"
    assert captured["files"][0][0] == "files"
    (val,) = p.get_values(
        sheet, next(c for c in p.columns(sheet) if c["name"] == "markdown")["id"]
    ).values()
    assert val == "# From Chandra"
    (ocr_used,) = p.get_values(
        sheet,
        next(c for c in p.columns(sheet) if c["name"] == "markdown_ocr_used")["id"],
    ).values()
    assert ocr_used == [True, True]
    p.close()


# ---------- catalog hint entry: unavailable + remediation when unconfigured ----------


def test_chandra_catalog_hint_entry_present():
    engines = _to_markdown_engines()
    docling = _by_id(engines, "docling")
    chandra = _by_id(engines, "chandra")
    assert chandra["tier"] == "sidecar"
    assert chandra["billable"] is False
    # unconfigured sidecar → unavailable with the configure pointer, same
    # honest-unavailable fallback docling gets (no separate copy needed)
    assert chandra["available"] is False
    assert chandra["error"]
    assert chandra["error"] == docling["error"]
    # no quality claim in copy (Hy-MT2 precedent) — "experimental" is a
    # maturity label, not a performance claim
    assert "experimental" in chandra["label"].lower()


# ---------- engine license hints -------------------------------------------


def test_chandra_catalog_entry_carries_the_license_block():
    chandra = _by_id(_to_markdown_engines(), "chandra")
    assert chandra["license"] == {
        "name": "Modified OpenRAIL-M (Datalab)",
        "url": "https://github.com/datalab-to/chandra/blob/master/MODEL_LICENSE",
        "note": (
            "Free under $2M revenue/funding; use must not compete with "
            "Datalab products."
        ),
        "restricted": True,
    }


def test_permissive_to_markdown_engines_carry_no_license_field():
    # Only FLAGGED (restricted/gated/custom) licenses ever attach — a
    # permissive engine must not carry the key at all (not even `None`),
    # so the web picker's "render a hint only when present" check stays
    # meaningful.
    engines = _to_markdown_engines()
    for engine_id in ("markitdown", "docling"):
        assert "license" not in _by_id(engines, engine_id)


def test_chandra_listed_in_action_catalog(tmp_path):
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    app = create_app(tmp_path / "ws", router=ModelRouter(cache=None, cache_mode="off"))
    client = TestClient(app)
    entries = {
        entry["kind"]: entry
        for entry in client.get("/api/actions/v1/catalog").json()["actions"]
    }
    to_markdown = entries["media.to_markdown"]
    engines = {e["id"]: e for e in to_markdown["ui_hints"]["engines"]}
    assert engines["chandra"]["tier"] == "sidecar"
    assert engines["chandra"]["available"] is False


# ---------- ops <-> contract <-> catalog parity (two independently ----------
# maintained sources diffed against each other)


def _params_model():
    return ACTION_REGISTRY.get("media.to_markdown").definition.run.params_model


def test_every_declared_engine_validates_through_the_params_model():
    from frisket.contracts.action import TO_MARKDOWN_SYMBOLIC_ENGINES

    for engine in sorted(TO_MARKDOWN_SYMBOLIC_ENGINES):
        params = _params_model().model_validate({"source": "doc", "engine": engine})
        assert params.model_dump(mode="json")["engine"] == engine


def test_every_catalog_advertised_engine_validates_through_the_params_model():
    """The picker renders the catalog roster, so every advertised id must
    clear contract validation rather than bounce with
    invalid_to_markdown_engine."""
    for entry in _to_markdown_engines():
        params = _params_model().model_validate(
            {"source": "doc", "engine": entry["id"]}
        )
        assert params.model_dump(mode="json")["engine"] == entry["id"]


# ---------- closure fence (closure-sweep audit C1): accepted => ----------
# dispatchable-or-rejected, and estimate never prices an undispatchable
# engine as free


def test_provider_model_engine_ids_are_rejected_at_validation():
    """C1: a provider/model ("/") id used to validate (the OCR remote-VLM
    shape copied without its dispatch branch), estimate free, and then burn
    every row at runtime with 'unknown convert engine'. There is no VLM
    conversion path — the contract must fail closed."""
    import pytest
    from pydantic import ValidationError

    for engine in ("openai/gpt-4.1-mini", "gemini/gemini-2.5-flash", "x/y"):
        with pytest.raises(ValidationError):
            _params_model().model_validate({"source": "doc", "engine": engine})


@pytest.mark.parametrize(
    ("engine", "configured"),
    (
        ("openai/gpt-4.1-mini", False),
        ("not_an_engine", False),
        ("datalab", False),
        ("datalab", True),
        ("markitdown", False),
        ("trafilatura_html", False),
        ("docling", False),
        ("docling", True),
        ("chandra", False),
        ("chandra", True),
    ),
)
def test_estimate_only_prices_available_unbilled_engines_free(
    tmp_path, monkeypatch, engine, configured
):
    """Unavailable routes refuse; a configured gateway remains operator-borne."""
    from fastapi.testclient import TestClient
    from frisket.server.app import create_app

    for name in ("DATALAB_API_KEY", "FRISKET_MODELS_URL", "FRISKET_MODELS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    if configured:
        if engine == "datalab":
            monkeypatch.setenv("DATALAB_API_KEY", "test-key")
        else:
            monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.invalid")
            monkeypatch.setenv("FRISKET_MODELS_TOKEN", "test-token")
    with TestClient(create_app(tmp_path / "workspace")) as client:
        pid = client.post("/api/projects", json={"name": "Estimate"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("Documents")
        column = project.add_column(sheet, "doc", type="text")
        project.add_rows(sheet, [{"doc": "<h1>Document</h1>"}], {"doc": column})
        response = client.post(
            f"/api/projects/{pid}/actions/v1/estimate",
            json={"action": _spec(sheet, engine)},
        )
        if engine in {"openai/gpt-4.1-mini", "not_an_engine"}:
            assert response.status_code == 400, response.text
        elif not configured and engine in {"datalab", "docling", "chandra"}:
            assert response.status_code == 400, response.text
            assert (
                "DATALAB_API_KEY" if engine == "datalab" else "FRISKET_MODELS_URL"
            ) in response.text
        else:
            assert response.status_code == 200, response.text
            estimate = response.json()["estimate"]
            if engine == "datalab":
                assert estimate["cost"] is None
                assert estimate["cost_source"] != "free_local"
            else:
                assert estimate["cost"] == 0
                assert estimate["cost_source"] == "free_local"
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_catalog_roster_carries_every_listed_table_engine_with_its_label():
    """Every listed TO_MARKDOWN_ENGINE_TABLE entry reaches the catalog with
    the table's label."""
    from frisket.contracts.actions.schemas._engines import TO_MARKDOWN_ENGINE_TABLE

    by_id = {e["id"]: e for e in _to_markdown_engines()}
    for decl in TO_MARKDOWN_ENGINE_TABLE:
        if not decl.listed:
            assert decl.id not in by_id
            continue
        assert decl.id in by_id, f"table engine {decl.id!r} missing from catalog"
        assert by_id[decl.id]["label"] == decl.label
