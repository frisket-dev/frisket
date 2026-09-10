"""Dead engine names stay dead, and live aliases stay live.

There is no retirement window: a rename is a breaking
change at zero users. the retired venue and target spellings's venue/tier words, the collapsed twins'
spellings, and the `remote` placement id are simply gone. These tests pin
that contract:

- no dead name is accepted anywhere (symbolic sets, alias maps, execution
  alias maps) in any of the three media families;
- the advertised model-word aliases survive untouched.

The teaching validation rejections for dead names (naming the canonical
replacement) are pinned in tests/ops/test_engine_alias_replay_corpus.py.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from frisket.actions.markdown import ToMarkdownParams

from frisket.contracts.actions.schemas._engines import (
    OCR_DEAD_ENGINE_REPLACEMENTS,
    OCR_ENGINE_TABLE,
    TO_MARKDOWN_DEAD_ENGINE_REPLACEMENTS,
    TO_MARKDOWN_ENGINE_TABLE,
    TRANSCRIBE_DEAD_ENGINE_REPLACEMENTS,
    TRANSCRIBE_ENGINE_TABLE,
    EngineDeclaration,
    alias_map,
    execution_alias_map,
    symbolic_engine_names,
)
from frisket.ops import ocr_engines as ocr_ops
from frisket.sdk.ops import transcribe_engines as transcribe_ops

_FAMILIES = [
    pytest.param(
        TRANSCRIBE_ENGINE_TABLE,
        symbolic_engine_names(TRANSCRIBE_ENGINE_TABLE),
        TRANSCRIBE_DEAD_ENGINE_REPLACEMENTS,
        transcribe_ops,
        id="transcribe",
    ),
    pytest.param(
        OCR_ENGINE_TABLE,
        symbolic_engine_names(OCR_ENGINE_TABLE),
        OCR_DEAD_ENGINE_REPLACEMENTS,
        ocr_ops,
        id="ocr",
    ),
    pytest.param(
        TO_MARKDOWN_ENGINE_TABLE,
        symbolic_engine_names(TO_MARKDOWN_ENGINE_TABLE),
        TO_MARKDOWN_DEAD_ENGINE_REPLACEMENTS,
        None,
        id="to_markdown",
    ),
]


@pytest.mark.parametrize(("table", "symbolic", "dead", "ops_module"), _FAMILIES)
def test_dead_names_are_gone_from_every_acceptance_surface(
    table, symbolic, dead, ops_module
) -> None:
    names = symbolic_engine_names(table)
    contract_aliases = alias_map(table)
    execution_aliases = execution_alias_map(table)
    for spelling in dead:
        assert spelling not in names, f"dead name {spelling!r} regained acceptance"
        assert spelling not in symbolic
        assert spelling not in contract_aliases
        assert spelling not in execution_aliases
        if ops_module is None:
            with pytest.raises(ValidationError):
                ToMarkdownParams(source="document", engine=spelling)
        else:
            assert spelling not in ops_module.ENGINE_ALIASES
    # Every taught replacement is itself live (or a provider/model id).
    for replacement in dead.values():
        assert "/" in replacement or replacement in names


def test_advertised_model_word_aliases_survive() -> None:
    # Model words name the engine, not a venue — they were never retired.
    assert alias_map(TRANSCRIBE_ENGINE_TABLE) == {"whisper": "faster_whisper"}
    ocr_aliases = alias_map(OCR_ENGINE_TABLE)
    assert ocr_aliases.get("tess") == "tesseract"
    assert ocr_aliases.get("paddle") == "paddleocr-vl"
    assert ocr_aliases.get("paddleocr") == "paddleocr-vl"
    assert alias_map(TO_MARKDOWN_ENGINE_TABLE) == {}


# --------------------------------------------------------------------------- #
# ``resolves_to`` still functions (the one alias oddity that survives).


def test_a_future_resolves_to_row_still_rewrites_at_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``resolves_to`` rewrite (a symbolic id executing as a
    provider/model id, as the deleted `remote` row once did) stays
    functional for a future placement-style row: the ops-layer
    ``execution_alias_map`` rewrites the id and the execution resolver
    canonicalizes through it, while the contract-layer ``alias_map`` keeps
    the symbolic id untouched for validation."""
    from frisket.execution import resolver

    table = (
        EngineDeclaration(
            id="placement-word",
            label="Placement word (executes on a hosted model)",
            tier="hosted",
            provider="synthetic",
            resolves_to="synthetic/model-1",
        ),
    )
    assert execution_alias_map(table)["placement-word"] == "synthetic/model-1"
    # The contract map never carries the rewrite — validation stays symbolic.
    assert "placement-word" not in alias_map(table)
    monkeypatch.setattr(resolver, "TRANSCRIBE_ENGINE_TABLE", table)
    assert resolver._canonical_engine("placement-word") == "synthetic/model-1"


# --------------------------------------------------------------------------- #
# Dispatch-boundary option projection (independent of retirement, kept on
# live names).


def test_undeclared_options_never_cross_the_dispatch_boundary() -> None:
    """The dispatch layer is capability-filtered
    via project_transcription_engine_options — a spec carrying an option the
    selected engine does not declare (e.g. `context` for a non-context
    engine) projects to nothing, so it can never reach a wire or worker, even
    for direct run_engine callers that bypassed authoring validation.
    (Authoring validation additionally REJECTS such specs; projection-drop is
    the established second fence for non-authoring callers.)"""
    from frisket.contracts.actions.schemas._engines import (
        project_transcription_engine_options,
    )

    for engine in ("parakeet-tdt", "openai/whisper-1"):
        projected = project_transcription_engine_options(
            engine, {"context": "Acme Corp, Sortformer"}
        )
        assert "context" not in projected, engine
    # Both live Faster spellings and MOSS declare the prompt and project it.
    for engine in ("faster_whisper", "whisper", "moss"):
        assert project_transcription_engine_options(engine, {"context": "Acme"}) == {
            "context": "Acme"
        }
