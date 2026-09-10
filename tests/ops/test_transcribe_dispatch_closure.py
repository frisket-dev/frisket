"""Recipe-dispatch closure across transcription and OCR.

``run_transcription_engine`` dispatches BY TRANSPORT (target resolution: one
engine can be served over several transports), with id-literal branches only
inside the local transport — while the advertised roster and the static target
definitions are TABLE/definition-derived. ``OcrRecipe.run_engine_on_pages``
dispatches over the same declaration set since routed OCR. These assertions pin, PER
CAPABILITY: every (engine, target) support row the definitions declare reaches
a dispatch branch, the definitions cover exactly the roster, and
(transcription only, where the engine table declares one) the engine-level
transport appears among the per-target transports actually served.

The fence is per-capability rather than global on purpose: a target row is a
VENUE row and one venue serves several capabilities, so "which branch would
receive this" is only answerable once you know which recipe is asking.
"""

from __future__ import annotations

import pytest

from frisket.contracts.actions.schemas._engines import (
    OCR_ENGINE_TABLE,
    TRANSCRIBE_ENGINE_TABLE,
)
from frisket.execution.definitions import build_static_targets

# The id-literal branches inside each recipe's dispatch, and the transports it
# dispatches. If a branch is added/removed there, update this mirror — the
# point of the fence is that TABLE/definition additions cannot outrun it.
_LOCAL_DISPATCH_IDS = {
    "transcribe": {"faster_whisper", "parakeet-tdt"},
    # OcrRecipe.run_engine_on_pages: the LIGHT_ENGINE and tesseract branches.
    "ocr": {"rapidocr", "tesseract"},
}
_DISPATCHED_TRANSPORTS = {
    "transcribe": {
        "local",
        "frisket.transcription.v1",
        "modal",
        "remote",
    },
    # sidecar.ocr -> _ocr_sidecar (SIDECAR_ENGINES), datalab.convert ->
    # _ocr_datalab, remote -> the '/' VLM branch.
    "ocr": {"local", "sidecar.ocr", "datalab.convert", "remote"},
}
_ROSTER = {
    "transcribe": TRANSCRIBE_ENGINE_TABLE,
    "ocr": OCR_ENGINE_TABLE,
}

_CAPABILITIES = tuple(_ROSTER)


def _rows(capability: str):
    for target in build_static_targets():
        for support in target.engines:
            if support.capability == capability:
                yield target, support


@pytest.mark.parametrize("capability", _CAPABILITIES)
def test_every_declared_support_row_reaches_a_dispatch_branch(capability) -> None:
    for target, support in _rows(capability):
        assert support.transport in _DISPATCHED_TRANSPORTS[capability], (
            f"target {target.id!r} declares {capability} transport "
            f"{support.transport!r} which the recipe does not dispatch"
        )
        if support.transport == "local":
            assert support.engine in _LOCAL_DISPATCH_IDS[capability], (
                f"engine {support.engine!r} declares transport 'local' "
                "on target "
                f"{target.id!r} but the recipe has no id-literal branch "
                "for it: it would advertise in the catalog and raise at "
                "dispatch"
            )


@pytest.mark.parametrize("capability", _CAPABILITIES)
def test_every_roster_engine_reaches_a_dispatch_branch(capability) -> None:
    # Every roster engine must appear on at least one static target (and
    # test_every_declared_support_row... proves those rows dispatch), unless
    # it carries a ``resolves_to`` rewrite onto a provider/model id, which
    # dispatches through the '/' branch instead.
    supported = {support.engine for _target, support in _rows(capability)}
    for entry in _ROSTER[capability]:
        if entry.resolves_to is not None:
            assert "/" in entry.resolves_to, (
                f"engine {entry.id!r} resolves to no provider/model id: "
                "the '/' dispatch branch can never receive it"
            )
            continue
        assert entry.id in supported, (
            f"roster engine {entry.id!r} has no static target support row: "
            "it would advertise in the catalog and refuse at resolution"
        )


@pytest.mark.parametrize("capability", _CAPABILITIES)
def test_static_execution_targets_match_the_roster(capability) -> None:
    # The static target definitions name engines by id string; pin the
    # non-wildcard set to exactly the directly-dispatched roster ids so a new
    # roster engine cannot ship without a target (or a target outlive its
    # roster row). Wildcard rows (``provider/*``) cover provider/model ids.
    target_engines = {
        support.engine
        for _target, support in _rows(capability)
        if not support.engine.endswith("/*")
    }
    roster = {entry.id for entry in _ROSTER[capability] if entry.resolves_to is None}
    assert target_engines == roster


def test_engine_level_transport_is_among_served_transports() -> None:
    # The engine declaration's transport is the SEMANTIC default (its
    # preferred/local build); per-target transports are the served truth.
    # The declared default must actually be served by some target — a
    # declaration whose transport nothing serves is a dead promise.
    # Transcription only: the OCR roster declares no engine-level transport
    # (its per-target rows are the whole truth).
    served: dict[str, set[str]] = {}
    for _target, support in _rows("transcribe"):
        served.setdefault(support.engine, set()).add(support.transport)
    for entry in TRANSCRIBE_ENGINE_TABLE:
        if entry.resolves_to is not None or entry.transcription is None:
            continue
        assert entry.transcription.transport in served[entry.id], entry.id
