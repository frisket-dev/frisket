"""Closure fence: no scratch compare/store-reader preview can bill a provider.

**Billable → run; not billable → preview.** One predicate, applied everywhere,
no preview taxonomy. A comparison that spends money at a provider endpoint
already IS a run — it simply was never recorded as one, which is why the
compare surface produced no run, no receipt, no attempt, no egress record and
no spend line for real money spent.

This file is the mechanical half of that rule, so nobody has to *promise* they
enumerated the call sites. Three independent framings, each of which goes red
on a different way of reintroducing the hole:

1. **Signature closure** — no compare entry point accepts ``router`` or
   ``allow_remote``. Red if someone re-adds the parameter (the boolean someone
   could flip back, and the credential handle that made spending possible).
2. **Roster closure** — every engine each family declares billable is refused
   by that family's compare entry point, enumerated FROM the declaration
   tables rather than from a hand-written list. Red the day a new billable
   engine is added to a roster without a compare refusal.
3. **Effect-site closure** — every dispatch helper refuses a billable engine
   itself, so the guarantee does not depend on a caller remembering. Red if a
   dispatch helper drops its own fence.

Framing 3 is the one that matters most: the recurring bug in this codebase is
a verification passed as an optional parameter that some call site forgets, so
the fence lives where the provider call actually happens.

This is deliberately not the MapRunner action-preview owner. Action preview
resolves canonical action recipes and admits only the exact-free/no-remote
subset at ``engine/runner/preview.py::_preview_validated``; its classifier and
pre-dispatch fence are pinned separately by
``tests/preview/test_action_preview_effect_fence.py``.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
from pathlib import Path

import pytest

import frisket.preview as preview_pkg
import frisket.preview.ocr as ocr_compare
import frisket.preview.translate as translate_compare
import frisket.preview.topic_segmentation as topic_compare
from frisket.contracts.actions.schemas._engines import (
    OCR_ENGINE_TABLE,
    is_billable_engine,
)
from frisket.preview.common import BillablePreviewDispatch
from frisket.engine.store import Project

# Every compare/bake-off entry point in the preview family. Adding a fifth
# compare surface without adding it here is caught by
# ``test_every_async_preview_coroutine_is_accounted_for`` below.
COMPARE_ENTRY_POINTS = (
    ocr_compare.compare_ocr_preview,
    translate_compare.compare_translate_scratch,
    topic_compare.compare_topic_segmentation_scratch,
)

# A provider/model id stands in for "any hosted model", which no roster
# enumerates because the id space is open.
PROVIDER_MODEL_ID = "provider/model"


@pytest.mark.parametrize("entry", COMPARE_ENTRY_POINTS, ids=lambda fn: fn.__name__)
def test_compare_entry_points_take_no_router_or_allow_remote(entry) -> None:
    params = inspect.signature(entry).parameters
    assert "router" not in params, (
        f"{entry.__name__} accepts a model router again. A compare surface with "
        "a router has provider credentials in scope, which is what let a "
        "bake-off spend money with no run behind it."
    )
    assert "allow_remote" not in params, (
        f"{entry.__name__} accepts allow_remote again. Billability is a fact "
        "about the engine, not a caller's boolean."
    )


def _public_coroutines_in_preview_package() -> dict[str, object]:
    """Every public coroutine defined anywhere in ``frisket.preview``.

    Deliberately NOT a curated module list plus a ``compare_`` name prefix: a
    blind review (2026-07-26, F2) evaded exactly that check twice — once with a
    new ``compare_*`` coroutine in an unlisted module, once with a
    ``bakeoff_*`` coroutine (carrying a ``router`` parameter) inside a listed
    one. Walking the package and taking every public coroutine has no such
    escape: a new entry point either joins ``COMPARE_ENTRY_POINTS`` or turns
    this red, whatever it is called and wherever it lives.
    """
    found: dict[str, object] = {}
    for info in pkgutil.iter_modules(preview_pkg.__path__):
        module = importlib.import_module(f"{preview_pkg.__name__}.{info.name}")
        for name, obj in vars(module).items():
            if name.startswith("_") or not inspect.iscoroutinefunction(obj):
                continue
            # Only functions DEFINED here; an imported coroutine is another
            # module's surface and is fenced where it lives.
            if getattr(obj, "__module__", None) != module.__name__:
                continue
            found[f"{module.__name__}.{name}"] = obj
    return found


#: Public coroutines in the package that are internal helpers rather than
#: entry points, each with the reason it needs no roster refusal of its own.
#: A NEW coroutine is not silently absorbed here — it turns
#: ``test_every_async_preview_coroutine_is_accounted_for`` red until someone
#: decides which list it belongs in.
FENCED_HELPERS = {
    # THE effect site for OCR. It carries its own billable fence and is
    # exercised directly by test_ocr_dispatch_helper_refuses_billable below.
    "frisket.preview.ocr.run_ocr_engine_preview",
    # Pure rasterization: turns PDF pages into images. Names no engine and
    # makes no provider call, so there is nothing for it to refuse.
    "frisket.preview.ocr.render_selected_pdf_pages",
}


@pytest.mark.parametrize(
    "name", sorted(_public_coroutines_in_preview_package()), ids=lambda n: n
)
def test_no_preview_coroutine_takes_a_router_or_allow_remote(name: str) -> None:
    """The signature closure applied to the WHOLE package, not a curated list.

    This needs no maintenance as the package grows: any coroutine anywhere in
    ``frisket.preview`` that grows a ``router`` or ``allow_remote`` parameter
    turns this red, whatever it is called. Provider credentials do not belong
    on a surface that writes no run.
    """
    fn = _public_coroutines_in_preview_package()[name]
    params = inspect.signature(fn).parameters
    assert "router" not in params, f"{name} accepts a model router again"
    assert "allow_remote" not in params, f"{name} accepts allow_remote again"


def test_every_async_preview_coroutine_is_accounted_for() -> None:
    """Every public coroutine in the package is either a fenced entry point or
    a documented helper — nothing is merely unexamined."""
    discovered = _public_coroutines_in_preview_package()
    assert discovered, "package walk found nothing — the fence lost its subject"
    entry_points = set(COMPARE_ENTRY_POINTS)
    unaccounted = sorted(
        name
        for name, fn in discovered.items()
        if fn not in entry_points and name not in FENCED_HELPERS
    )
    assert not unaccounted, (
        "new async entry point(s) in frisket.preview are not covered by this "
        f"closure file: {unaccounted}. Add them to COMPARE_ENTRY_POINTS with a "
        "billable refusal, or to FENCED_HELPERS with the reason they need "
        "none — rather than deleting this assertion."
    )
    # And FENCED_HELPERS may not rot into names that no longer exist.
    assert FENCED_HELPERS <= set(discovered), sorted(FENCED_HELPERS - set(discovered))


def _billable_ids(table) -> list[str]:
    return [entry.id for entry in table if entry.billable]


def test_ocr_roster_has_a_billable_engine_to_fence() -> None:
    # Guards the guard: if the roster ever declared nothing billable, the
    # parametrized refusal tests below would silently pass on an empty set.
    assert _billable_ids(OCR_ENGINE_TABLE)


@pytest.mark.parametrize(
    "engine", _billable_ids(OCR_ENGINE_TABLE) + [PROVIDER_MODEL_ID]
)
def test_ocr_compare_refuses_every_billable_engine(engine: str, tmp_path: Path) -> None:
    project = Project.create(tmp_path / "ocr.frisket", name="OCR closure")
    try:
        with pytest.raises(ocr_compare.OcrComparePreviewError) as refused:
            asyncio.run(
                ocr_compare.compare_ocr_preview(
                    project,
                    {
                        "sheet_id": 1,
                        "row_id": 1,
                        "input_column": "Document",
                        "pages": [1],
                        "engines": [engine, "rapidocr"],
                    },
                )
            )
        assert refused.value.code == "billable_engine_requires_run"
        assert refused.value.details == {"engine": engine, "action": "media.ocr"}
    finally:
        project.close()


@pytest.mark.parametrize("engine", sorted(translate_compare._REMOTE_ENGINES))
def test_translate_compare_refuses_every_billable_engine(engine: str) -> None:
    with pytest.raises(translate_compare.TranslateComparePreviewError) as refused:
        asyncio.run(
            translate_compare.compare_translate_scratch(
                None,
                {
                    "engines": [engine],
                    "text": "Hello",
                    "target_language": "Spanish",
                },
            )
        )
    assert refused.value.code == "billable_engine_requires_run"
    assert refused.value.details == {"engine": engine, "action": "map.translate"}


def test_free_engines_are_not_refused() -> None:
    """The other half of the predicate, so the fence cannot pass by refusing
    everything: a free engine survives coercion untouched."""
    for engine in (
        "rapidocr",
        "tesseract",
        "paddleocr-vl",
        "dots.mocr",
        "surya2",
    ):
        assert not is_billable_engine(OCR_ENGINE_TABLE, engine)
        other = "tesseract" if engine == "rapidocr" else "rapidocr"
        assert ocr_compare._coerce_request(
            {
                "sheet_id": 1,
                "row_id": 1,
                "input_column": "Document",
                "pages": [1],
                "engines": [engine, other],
            }
        ).engines == [engine, other]
    for engine in ("opus_mt", "hy_mt2"):
        assert engine not in translate_compare._REMOTE_ENGINES
        assert translate_compare._coerce_scratch_request(
            {"engines": [engine], "text": "hi", "target_language": "Spanish"}
        ).engines == [engine]


def test_ocr_dispatch_helper_refuses_billable(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "fence.frisket", name="fence")
    try:
        page = ocr_compare.RenderedPreviewPage(
            page=1, path=tmp_path / "p.png", width=1, height=1
        )
        for engine in _billable_ids(OCR_ENGINE_TABLE) + [PROVIDER_MODEL_ID]:
            with pytest.raises(BillablePreviewDispatch):
                asyncio.run(
                    ocr_compare.run_ocr_engine_preview(
                        engine,
                        [page],
                        project=project,
                        language=None,
                        scratch=tmp_path,
                    )
                )
    finally:
        project.close()


def test_translate_dispatch_helper_refuses_billable() -> None:
    import httpx

    async def probe(engine: str) -> None:
        async with httpx.AsyncClient() as http:
            await translate_compare._run_engine_with_project(
                engine,
                translate_compare.TranslateCompareScratchRequest(
                    engines=[engine], text="Hello", target_language="Spanish"
                ),
                http=http,
                project=None,
            )

    for engine in sorted(translate_compare._REMOTE_ENGINES):
        with pytest.raises(BillablePreviewDispatch):
            asyncio.run(probe(engine))


def test_semantic_cluster_preview_cannot_reach_a_remote_embedder(tmp_path) -> None:
    """The store readers bill too — this one did (blind review 2026-07-26, F1).

    ``POST /clusters/v1/preview`` with ``method=semantic`` used to resolve the
    project router and fall back to ``openai/text-embedding-3-small``, billing
    once per distinct column value with no run, no receipt and no spend line —
    and, unlike the compare surfaces, without even a ``cost`` block in the
    response. Reproduced with a spy router before the fix; this pins it shut.
    """
    import frisket.semantic as semantic

    embed_calls: list[list[str]] = []

    class SpyRouter:
        def has_embedding_backend(self) -> bool:  # pragma: no cover - not reached
            return True

        async def embed(self, texts):  # pragma: no cover - must not be reached
            embed_calls.append(list(texts))
            return [[0.0] for _ in texts]

    original_local = semantic.local_embedder
    semantic.local_embedder = lambda *a, **k: None
    try:
        project = Project.create(tmp_path / "c.frisket", name="cluster closure")
        try:
            sheet = project.add_sheet("data")
            cols = {"org": project.add_column(sheet, "org")}
            project.add_rows(
                sheet,
                [{"org": v} for v in ("Brooklyn", "Brookline", "Queens")],
                cols,
            )
            from frisket.preview.cluster import (
                ClusterPreviewError,
                resolve_cluster_preview,
            )

            with pytest.raises(ClusterPreviewError) as refused:
                resolve_cluster_preview(
                    project, sheet_id=sheet, input_column="org", method="semantic"
                )
            assert refused.value.code == "embedding_backend_unavailable"
            assert embed_calls == [], (
                "semantic cluster preview embedded through a remote provider; "
                "a preview has no cost gate to surface that spend with"
            )
        finally:
            project.close()
    finally:
        semantic.local_embedder = original_local


def test_preview_service_never_resolves_a_model_router_at_all() -> None:
    """``router_for`` mints provider credentials. No scratch/store-reader
    preview method in ``server/services/previews.py`` may call it.

    An earlier version of this fence checked only methods whose NAME contained
    "compare", which excluded by construction the one method that actually did
    mint a router — ``cluster_preview``, whose semantic path then billed OpenAI
    with no run behind it (blind review 2026-07-26, F1/F5). The check is now
    the whole module: previews are the free surface, full stop, so there is no
    method left that needs an exception. MapRunner action preview is a distinct
    surface with its own categorical fence in ``engine/runner/preview.py``.
    """
    import frisket.server.services.previews as previews

    module_source = inspect.getsource(previews)
    assert "router_for" not in module_source, (
        "server/services/previews.py resolves a model router again. A preview "
        "route with provider credentials in scope can spend money with no run, "
        "no receipt and no spend line behind it."
    )
    # The *_uses_remote helpers existed only to decide whether to mint that
    # router; nothing should import them.
    assert "uses_remote" not in module_source
