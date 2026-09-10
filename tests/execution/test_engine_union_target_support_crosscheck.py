"""Engine-union vs target-support cross-check (the route-bundle cutover duplication/spec sweep).

Authoring-time validation (contracts/actions/schemas/media.py) validates
transcription options against the hand-authored engine-capability UNION
declared in contracts/actions/schemas/_engines.py (``TRANSCRIBE_ENGINE_TABLE``
entries' ``transcription`` field). The per-target truth — what each serving
venue can ACTUALLY do — lives in execution/definitions.py's
``build_static_targets()``, as ``TargetEngineSupport`` rows on each
``ExecutionTarget``. The authored union is derived from
the target definitions, but nothing in code enforces that relationship: an
author could widen (or narrow) one side without touching the other and
nothing would fail — until a caller either gets rejected for an ability a
target actually has, or is accepted for one no live target can serve.

This module recomputes each field's union straight from the
``TargetEngineSupport`` rows and asserts it matches the authored engine-level
declaration, so a drift fails naming the specific field that no longer
agrees.
"""

from __future__ import annotations

from frisket.contracts.actions.schemas._engines import (
    TRANSCRIBE_ENGINE_TABLE,
    TranscriptionEngineCapabilities,
    transcription_engine_capabilities,
)
from frisket.execution.definitions import build_static_targets
from frisket.execution.targets import TargetEngineSupport


def _target_rows_for(engine_id: str) -> list[TargetEngineSupport]:
    """Every ``TargetEngineSupport`` row, across all static targets, whose
    ``engine`` is ``engine_id``."""
    return [
        support
        for target in build_static_targets()
        for support in target.engines
        if support.capability == "transcribe" and support.engine == engine_id
    ]


def _canonical_engines() -> list[tuple[str, TranscriptionEngineCapabilities]]:
    return [
        (entry.id, entry.transcription)
        for entry in TRANSCRIBE_ENGINE_TABLE
        if entry.transcription is not None
    ]


def test_every_canonical_engine_has_at_least_one_serving_target():
    """A roster engine with no serving target is unreachable dead weight —
    and would make every union check below vacuously pass, hiding drift."""
    for engine_id, _capabilities in _canonical_engines():
        rows = _target_rows_for(engine_id)
        assert rows, (
            f"engine {engine_id!r} has no TargetEngineSupport row on any "
            "static target (build_static_targets)"
        )


def test_vad_union_matches_authoring_declaration():
    for engine_id, capabilities in _canonical_engines():
        rows = _target_rows_for(engine_id)
        union_vad = any(row.options.vad for row in rows)
        assert union_vad == capabilities.vad, (
            f"{engine_id}: authored vad={capabilities.vad} but the "
            f"per-target union is {union_vad}"
        )


def test_language_union_matches_authoring_declaration():
    for engine_id, capabilities in _canonical_engines():
        rows = _target_rows_for(engine_id)
        union_language = any(row.options.language for row in rows)
        assert union_language == capabilities.accepts_language, (
            f"{engine_id}: authored accepts_language="
            f"{capabilities.accepts_language} but the per-target union is "
            f"{union_language}"
        )


def test_context_union_matches_authoring_declaration():
    for engine_id, capabilities in _canonical_engines():
        rows = _target_rows_for(engine_id)
        union_context = any(row.options.context for row in rows)
        assert union_context == capabilities.context, (
            f"{engine_id}: authored context={capabilities.context} but the "
            f"per-target union is {union_context}"
        )


def test_diarization_mode_matches_some_serving_target():
    """The engine's declared ``diarization_mode`` must be a mode some target
    row actually advertises — union-of-declared-support (per
    ``execution/definitions.py``'s parakeet-tdt commentary), never a mode
    invented above what any target can do, and never silently downgraded to
    'none' while a target still diarizes."""
    for engine_id, capabilities in _canonical_engines():
        rows = _target_rows_for(engine_id)
        modes = {row.options.diarization_mode for row in rows}
        assert capabilities.diarization_mode in modes, (
            f"{engine_id}: authored diarization_mode="
            f"{capabilities.diarization_mode!r} not found among per-target "
            f"modes {sorted(modes)}"
        )


def test_speaker_hint_matches_some_serving_target():
    for engine_id, capabilities in _canonical_engines():
        rows = _target_rows_for(engine_id)
        hints = {row.options.speaker_hint for row in rows}
        assert capabilities.speaker_hint in hints, (
            f"{engine_id}: authored speaker_hint={capabilities.speaker_hint!r} "
            f"not found among per-target speaker hints {sorted(hints)}"
        )


def test_max_speakers_requires_an_optional_diarizing_target():
    """``max_speakers`` lives ONLY on the engine declaration — no per-target
    field carries a speaker cap (``TranscriptionOptionSupport`` has none),
    so it cannot be unioned the same way as the fields above. What CAN be
    cross-checked: it is "the released-checkpoint speaker cap for a
    diarizing engine" (media.py's ``transcribe_max_speakers`` docstring) —
    concretely, today, the Sortformer build behind ``diarization_mode ==
    'optional'``. An 'intrinsic' engine (moss) reports whatever speakers it
    finds with no caller-facing cap knob, so it correctly has none. This
    pins the safe direction: a cap is never advertised without an optional
    diarizing target. Some optional engines do not expose a speaker-count
    input and therefore have no cap."""
    for engine_id, capabilities in _canonical_engines():
        rows = _target_rows_for(engine_id)
        has_optional_target = any(
            row.options.diarization_mode == "optional" for row in rows
        )
        if not has_optional_target:
            assert capabilities.max_speakers is None, (
                f"{engine_id}: no target optionally diarizes but the engine "
                f"declares max_speakers={capabilities.max_speakers}"
            )


def test_model_size_union_matches_authoring_declaration():
    """``model_size`` (whisper's author-selectable size knob) is the engine
    half of HALT 7's per-target ``sizes`` vocabulary: a target's non-empty
    ``sizes`` tuple is what makes the size knob authorable at all, so the
    union of "some target declares sizes" must match the authored flag."""
    for engine_id, capabilities in _canonical_engines():
        rows = _target_rows_for(engine_id)
        union_has_sizes = any(row.sizes for row in rows)
        assert union_has_sizes == capabilities.model_size, (
            f"{engine_id}: authored model_size={capabilities.model_size} but "
            "the per-target sizes union is "
            f"{'present' if union_has_sizes else 'empty'}"
        )


def test_remote_wildcard_union_matches_provider_model_branch():
    """Free-form ``provider/model`` engines share one explicit remote
    transport family, declared on BOTH sides of this seam: the authoring-time
    union branch in ``transcription_engine_capabilities`` (the '/' fallback,
    used for e.g. ``openai/whisper-1``) and the per-provider ``{provider}/*``
    wildcard ``TargetEngineSupport`` row minted by ``_remote_wildcard_support``
    for every router-known provider target. They must agree on every field
    the wildcard row actually carries (``detects_language`` is excluded: it
    is a per-model fact the wildcard row's ``options`` type has no field
    for)."""
    wildcard_rows = [
        support
        for target in build_static_targets()
        for support in target.engines
        # Remote-API targets now carry an OCR wildcard row beside the
        # transcription one. This check is about the TRANSCRIPTION union and
        # the transcription option-support type, so it selects by capability
        # rather than by engine spelling (both rows are "{provider}/*").
        if support.capability == "transcribe" and support.engine.endswith("/*")
    ]
    assert wildcard_rows, "no remote-api wildcard TargetEngineSupport rows found"
    for support in wildcard_rows:
        provider = support.engine.removesuffix("/*")
        authored = transcription_engine_capabilities(
            TRANSCRIBE_ENGINE_TABLE, f"{provider}/some-model"
        )
        assert authored.vad == support.options.vad, provider
        assert authored.accepts_language == support.options.language, provider
        assert authored.context == support.options.context, provider
        assert authored.clean == support.options.clean, provider
        assert authored.diarization_mode == support.options.diarization_mode, provider
        assert authored.speaker_hint == support.options.speaker_hint, provider
