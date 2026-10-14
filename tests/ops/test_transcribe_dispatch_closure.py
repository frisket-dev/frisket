"""Roster and static-target consistency across transcription and OCR."""

from __future__ import annotations

import pytest

from frisket.contracts.actions.schemas._engines import (
    TRANSCRIBE_ENGINE_TABLE,
    ocr_engine_table,
)
from frisket.execution.definitions import build_static_targets
from frisket.ops.base import OpContext
from frisket.ops.ocr_engines import OcrEngines


def _roster(capability: str):
    return TRANSCRIBE_ENGINE_TABLE if capability == "transcribe" else ocr_engine_table()


_CAPABILITIES = ("transcribe", "ocr")


def _rows(capability: str):
    for target in build_static_targets():
        for support in target.engines:
            if support.capability == capability:
                yield target, support


@pytest.mark.asyncio
async def test_indra_ocr_dispatches_through_local_adapter(
    tmp_path, monkeypatch
) -> None:
    engines = OcrEngines()
    page = tmp_path / "page.png"
    scratch = tmp_path / "scratch"
    expected = [{"text": "local text", "blocks": []}]
    calls = []

    async def indra_adapter(page_paths, scratch_path):
        calls.append((page_paths, scratch_path))
        return expected

    monkeypatch.setattr(engines, "_ocr_indra", indra_adapter)

    result = await engines.run_engine_on_pages(
        "indra-ocr",
        [page],
        OpContext(),
        usage={},
        scratch=scratch,
    )

    assert result == expected
    assert calls == [([page], scratch)]


@pytest.mark.parametrize("capability", _CAPABILITIES)
def test_every_roster_engine_has_static_target_support(capability) -> None:
    # Every roster engine must appear on at least one static target unless it
    # carries a ``resolves_to`` rewrite onto a provider/model id.
    supported = {support.engine for _target, support in _rows(capability)}
    for entry in _roster(capability):
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
    roster = {entry.id for entry in _roster(capability) if entry.resolves_to is None}
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
