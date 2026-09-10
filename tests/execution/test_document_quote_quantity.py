"""Document quotes cannot infer which admitted source a typed handler reads."""

from dataclasses import dataclass

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import (
    MediaBlobStore,
    media_cell,
    owned_media_metadata_document,
)
from frisket.execution.promise_compiler import (
    OperatorBorneZeroCost,
    PricedCostBasis,
    UnpriceableCost,
)
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.execution.resolve_for_action import (
    _document_quantity_pages,
    resolve_for_action,
)
from frisket.execution.resolver import ResolvedExecution
from frisket.execution.targets import CAPABILITY_OCR, CAPABILITY_TO_MARKDOWN
from frisket.ops.base import Recipe


@dataclass
class _DocumentProgram(Recipe):
    name: str = "example.document_reader"
    consumes_resolution = True
    execution_capability = CAPABILITY_TO_MARKDOWN
    cost_class = "metered"


@pytest.fixture
def documents(tmp_path):
    project = Project.create(tmp_path / "documents.frisket")
    sheet_id = project.add_sheet("Documents")
    columns = {
        name: project.add_column(sheet_id, name, type="json")
        for name in ("primary", "alternate")
    }
    try:
        yield project, sheet_id, columns
    finally:
        project.close()


def _document(project, *, pages=3, kind="document", mime="application/pdf"):
    probe = {"kind": kind}
    if pages is not None:
        probe["pages"] = pages
    digest = project.add_blob(
        repr(probe).encode(),
        filename="source.pdf",
        mime=mime,
        metadata=owned_media_metadata_document(probe=probe),
    )
    return media_cell(digest, mime=mime, filename="source.pdf")


def _spec(documents, rows, **extra):
    project, sheet_id, columns = documents
    row_ids = project.add_rows(sheet_id, rows, columns)
    return {
        "action_kind": "example.document_reader",
        "sheet_id": sheet_id,
        "input_columns": list(columns),
        "row_ids": row_ids,
        "engine": "datalab",
        **extra,
    }


def _resolve(project, spec, *, capability=CAPABILITY_TO_MARKDOWN):
    program = _DocumentProgram()
    program.execution_capability = capability
    return resolve_for_action(
        project,
        spec,
        program,
        composition=open_execution_composition(
            project, ModelRouter(), ExecutionCompositionContext.direct()
        ),
    )


@pytest.mark.parametrize("capability", [CAPABILITY_TO_MARKDOWN, CAPABILITY_OCR])
def test_single_stored_document_and_image_have_exact_selected_page_quote(
    documents, monkeypatch, capability
):
    monkeypatch.setenv("DATALAB_API_KEY", "synthetic-document-quote-key")
    project, sheet_id, columns = documents
    pdf = _document(project)
    image = _document(project, pages=None, kind="image", mime="image/png")
    spec = _spec(documents, [{"primary": pdf}, {"alternate": image}, {}])
    project.add_rows(sheet_id, [{"primary": _document(project, pages=99)}], columns)
    resolved = _resolve(project, spec, capability=capability)
    assert isinstance(resolved, ResolvedExecution)
    assert isinstance(resolved.cost_basis, PricedCostBasis)
    assert resolved.cost_basis.estimated_quantity == "4"
    assert resolved.resolution.estimate_basis.quantity_hint == 4
    assert resolved.cost_basis.bound > 0


@pytest.mark.parametrize(
    "value", ["<html>document</html>", "/tmp/source.pdf", 0, False]
)
def test_nonblob_source_is_unknown_not_zero(documents, monkeypatch, value):
    monkeypatch.setenv("DATALAB_API_KEY", "synthetic-document-quote-key")
    spec = _spec(documents, [{"primary": value}])
    assert _document_quantity_pages(documents[0], spec, _DocumentProgram()) is None
    resolved = _resolve(documents[0], spec)
    assert isinstance(resolved, ResolvedExecution)
    assert isinstance(resolved.cost_basis, UnpriceableCost)
    assert any(
        p.field == "cost" and p.op == "unbounded" and p.audience == "user_claim"
        for p in resolved.promise_set.promises
    )


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("alternate", ["pdf", "inline"])
def test_multiple_possible_actual_sources_never_pick_first(
    documents, monkeypatch, reverse, alternate
):
    monkeypatch.setenv("DATALAB_API_KEY", "synthetic-document-quote-key")
    project = documents[0]
    spec = _spec(
        documents,
        [
            {
                "primary": _document(project),
                "alternate": (
                    _document(project, pages=7) if alternate == "pdf" else "inline text"
                ),
            }
        ],
    )
    if reverse:
        spec["input_columns"].reverse()
    resolved = _resolve(project, spec)
    assert isinstance(resolved, ResolvedExecution)
    assert isinstance(resolved.cost_basis, UnpriceableCost)
    cost_claims = [p for p in resolved.promise_set.promises if p.field == "cost"]
    assert len(cost_claims) == 1
    assert cost_claims[0].op == "unbounded"
    assert cost_claims[0].audience == "user_claim"


@pytest.mark.parametrize("pages", [None, 0, -1, True, 1.5, "3"])
def test_missing_or_invalid_probe_page_count_is_unknown(documents, pages):
    spec = _spec(documents, [{"primary": _document(documents[0], pages=pages)}])
    assert _document_quantity_pages(documents[0], spec, _DocumentProgram()) is None


def test_unstored_or_cell_mime_spoofed_blob_cannot_quote_one_page(documents):
    project = documents[0]
    value = _document(project, pages=None)
    value["mime"] = "image/png"
    spec = _spec(documents, [{"primary": value}])
    assert _document_quantity_pages(project, spec, _DocumentProgram()) is None
    spec = _spec(documents, [{"primary": {"blob": "0" * 64, "mime": "image/png"}}])
    assert _document_quantity_pages(project, spec, _DocumentProgram()) is None


def test_blank_rows_contribute_zero(documents):
    spec = _spec(documents, [{}, {"primary": " "}, {"alternate": []}])
    assert _document_quantity_pages(documents[0], spec, _DocumentProgram()) == 0


def test_local_free_conversion_does_not_probe_unknown_inputs(documents, monkeypatch):
    spec = _spec(
        documents,
        [{"primary": "<html>inline</html>", "alternate": "/tmp/document.pdf"}],
        engine="markitdown",
    )

    def unexpected_probe(*args, **kwargs):
        pytest.fail("A free local conversion must not acquire document metadata")

    monkeypatch.setattr(MediaBlobStore, "probe_metadata", unexpected_probe)
    resolved = _resolve(documents[0], spec)
    assert isinstance(resolved, ResolvedExecution)
    assert isinstance(resolved.cost_basis, OperatorBorneZeroCost)
    assert not [p for p in resolved.promise_set.promises if p.field == "cost"]


def test_default_conversion_engine_uses_typed_declaration(documents):
    from frisket.actions.document_types import DEFAULT_DOCUMENT_ENGINE

    spec = _spec(documents, [{"primary": "<html>inline</html>"}])
    spec.pop("engine")
    resolved = _resolve(documents[0], spec)
    assert isinstance(resolved, ResolvedExecution)
    assert resolved.resolution.facts.engine == DEFAULT_DOCUMENT_ENGINE
    assert isinstance(resolved.cost_basis, OperatorBorneZeroCost)
