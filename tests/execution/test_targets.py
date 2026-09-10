"""Execution-target seam types (the capability contract, behavior-neutral).

Pins construction/validation of TargetEngineSupport and ExecutionTarget, the
full egress-order truth table including unordered third_party_api pairs, and
the deliberate absence of cost_posture from the target noun.
"""

from __future__ import annotations

import dataclasses

import pytest

from frisket.contracts.transcription_sidecar import TranscriptionOptionSupport
from frisket.execution.targets import (
    CostPosture,
    ExecutionTarget,
    TargetEngineSupport,
)


def _support(**overrides) -> TranscriptionOptionSupport:
    payload = {
        "diarization_mode": "none",
        "speaker_hint": "none",
        "language": True,
        "model_size": False,
        "vad": False,
        "context": False,
    }
    payload.update(overrides)
    return TranscriptionOptionSupport.model_validate(payload)


def _engine(engine: str = "faster_whisper", **overrides) -> TargetEngineSupport:
    kwargs = {
        "engine": engine,
        "transport": "frisket.transcription.v1",
        "capability": "transcribe",
        "options": _support(),
    }
    kwargs.update(overrides)
    return TargetEngineSupport(**kwargs)


# ---------------------------------------------------------------------------
# construction


def test_target_engine_support_defaults_and_fields():
    support = _engine(sizes=("base", "large-v3"))
    assert support.run_scoped is False
    assert support.sizes == ("base", "large-v3")
    assert _engine().sizes == ()


def test_execution_target_construction_and_defaults():
    target = ExecutionTarget(
        id="models-gateway",
        operator="self",
        egress_class="operator_lan",
        engines=(
            _engine("faster_whisper"),
            _engine("moss", transport="frisket.transcription.v1"),
        ),
    )
    assert target.region is None
    assert len(target.engines) == 2


def test_seam_types_are_frozen():
    target = ExecutionTarget(id="local", operator="self", egress_class="none")
    with pytest.raises(dataclasses.FrozenInstanceError):
        target.id = "other"
    with pytest.raises(dataclasses.FrozenInstanceError):
        _engine().run_scoped = True


def test_cost_posture_is_a_vocabulary_not_a_target_field():
    """Posture is a route-level fact; the target noun must never grow the
    field."""
    field_names = {field.name for field in dataclasses.fields(ExecutionTarget)}
    assert "cost_posture" not in field_names
    assert set(CostPosture.__args__) == {
        "operator_borne",
        "platform_metered",
        "org_key",
    }


# ---------------------------------------------------------------------------
# validation failures


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"engine": ""}, "non-empty"),
        # The refusal now names the DECLARING capability's wire set rather than
        # a global union, so it can say which wires this row may speak.
        ({"transport": "carrier-pigeon"}, "is not a 'transcribe' wire"),
        ({"transport": ""}, "is not a 'transcribe' wire"),
        # And a wire that IS valid — for another capability — refuses here too.
        ({"transport": "deepl.v2"}, "is not a 'transcribe' wire"),
        ({"options": {"language": True}}, "TranscriptionOptionSupport"),
        ({"sizes": ("base", "")}, "non-empty"),
        ({"sizes": ("base", "base")}, "unique"),
    ],
)
def test_target_engine_support_rejects_invalid_shapes(kwargs, match):
    with pytest.raises(ValueError, match=match):
        _engine(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"id": ""}, "target id"),
        ({"operator": ""}, "operator"),
        ({"egress_class": "vpn"}, "unknown egress class"),
        ({"region": ""}, "never an empty string"),
        ({"engines": ("faster_whisper",)}, "TargetEngineSupport"),
    ],
)
def test_execution_target_rejects_invalid_shapes(kwargs, match):
    base = {"id": "local", "operator": "self", "egress_class": "none"}
    base.update(kwargs)
    with pytest.raises(ValueError, match=match):
        ExecutionTarget(**base)


def test_execution_target_rejects_duplicate_engine_entries():
    """One target row speaks per-engine transports — a second row for the
    same engine is a contradiction, even on a different transport."""
    with pytest.raises(ValueError, match="duplicate engine"):
        ExecutionTarget(
            id="models-gateway",
            operator="self",
            egress_class="operator_lan",
            engines=(
                _engine("faster_whisper", transport="frisket.transcription.v1"),
                _engine("faster_whisper", transport="frisket.transcription.v1"),
            ),
        )


def test_execution_target_accepts_distinct_engines():
    target = ExecutionTarget(
        id="models-gateway",
        operator="self",
        egress_class="operator_lan",
        engines=(_engine("faster_whisper"), _engine("parakeet-tdt")),
    )
    assert [entry.engine for entry in target.engines] == [
        "faster_whisper",
        "parakeet-tdt",
    ]


def test_mirrored_option_support_is_frozen_and_targets_reject_blank_identifiers():
    """Contract guard: the frozen dataclass would be worthless if its embedded
    wire model were mutable — the app mirror's base is frozen=True, and this
    test keeps it that way. Blank-ish identifiers are rejected per the
    honest-absence rule."""
    import pydantic
    import pytest as _pytest

    from frisket.contracts.transcription_sidecar import (
        TranscriptionOptionSupport,
    )
    from frisket.execution.targets import ExecutionTarget, TargetEngineSupport

    options = TranscriptionOptionSupport(
        diarization_mode="intrinsic",
        speaker_hint="none",
        language=False,
        model_size=False,
        vad=False,
        context=True,
    )
    with _pytest.raises(pydantic.ValidationError):
        options.context = False  # type: ignore[misc]

    with _pytest.raises(ValueError):
        TargetEngineSupport(
            engine="  moss ",
            transport="frisket.transcription.v1",
            capability="transcribe",
            options=options,
        )
    with _pytest.raises(ValueError):
        ExecutionTarget(id=" x", operator="self", egress_class="none")


def test_r1r2_loop_round3_malformed_value_fences():
    """Fresh-resolution support values must reject malformed options/sizes at
    construction. They guide resolution but are not persisted in route
    snapshots under GD-06."""
    import pytest as _pytest

    from frisket.contracts.transcription_sidecar import (
        TranscriptionOptionSupport,
    )
    from frisket.execution.targets import ExecutionTarget, TargetEngineSupport

    options = TranscriptionOptionSupport(
        diarization_mode="none",
        speaker_hint="none",
        language=True,
        model_size=False,
        vad=False,
        context=False,
    )
    for bad_sizes in ((1,), (" base ",), ("  ",)):
        with _pytest.raises(ValueError):
            TargetEngineSupport(
                engine="faster_whisper",
                transport="local",
                capability="transcribe",
                options=options,
                sizes=bad_sizes,
            )
    with _pytest.raises(ValueError):
        TargetEngineSupport(
            engine="faster_whisper",
            transport="local",
            capability="transcribe",
            options=options,
            run_scoped="yes",  # type: ignore[arg-type]
        )
    with _pytest.raises(ValueError):
        ExecutionTarget(id="x", operator="self", egress_class="none", region="   ")


# ---------------------------------------------------------------------------
# The capability vocabulary, mechanically: declarers == consumers
#
# Six tables key on the capability token, across four modules. Before Phase 4
# there were two capabilities and a reader could hold them in their head; there
# are six now, and enumeration is the failure mode CLAUDE.md names — agents
# undercount call sites, confidently. So the closure is a TEST rather than a
# promise that somebody swept correctly: declare a capability anywhere and this
# goes red until every table that must answer for it does.


def _capability_tables() -> dict[str, set[str]]:
    from frisket.execution.resolve_for_action import (
        _CAPABILITY_COST_BASIS,
        _CAPABILITY_OPTION_KEYS,
    )
    from frisket.execution.resolver import (
        _CAPABILITY_INABILITY,
        _CAPABILITY_WORD,
    )
    from frisket.execution.targets import (
        CAPABILITY_OPTION_SUPPORT,
        EXECUTION_CAPABILITIES,
    )

    return {
        "targets.EXECUTION_CAPABILITIES": set(EXECUTION_CAPABILITIES),
        "targets.CAPABILITY_OPTION_SUPPORT": set(CAPABILITY_OPTION_SUPPORT),
        "resolver._CAPABILITY_INABILITY": set(_CAPABILITY_INABILITY),
        "resolver._CAPABILITY_WORD": set(_CAPABILITY_WORD),
        "resolve_for_action._CAPABILITY_COST_BASIS": set(_CAPABILITY_COST_BASIS),
        "resolve_for_action._CAPABILITY_OPTION_KEYS": set(_CAPABILITY_OPTION_KEYS),
    }


def test_every_capability_table_declares_the_same_vocabulary():
    """Cut any one row out of any one table and this names the table and the
    capability. A capability missing from ``_CAPABILITY_INABILITY`` raises at
    resolution, one missing from ``_CAPABILITY_COST_BASIS`` raises at the mint, and
    one missing from ``CAPABILITY_OPTION_SUPPORT`` raises at roster
    construction — three different late failures this one early failure
    replaces."""
    tables = _capability_tables()
    expected = tables["targets.EXECUTION_CAPABILITIES"]
    mismatched = {
        name: sorted(declared.symmetric_difference(expected))
        for name, declared in tables.items()
        if declared != expected
    }
    assert not mismatched, (
        "these capability tables disagree with EXECUTION_CAPABILITIES: "
        f"{mismatched}. A capability is registered in ALL of them or in none — "
        "a partial registration fails at dispatch, not here."
    )


def test_every_capability_canonicalizes_through_its_own_engine_table():
    """The seventh consumer, a FUNCTION rather than a dict and so not
    comparable above: a capability with no roster table cannot canonicalize an
    engine symbol to a venue at all."""
    from frisket.execution.resolver import capability_engine_table
    from frisket.execution.targets import EXECUTION_CAPABILITIES

    for capability in sorted(EXECUTION_CAPABILITIES):
        table = capability_engine_table(capability)
        assert table, f"{capability} canonicalizes through an EMPTY roster table"


def test_every_declared_wire_has_a_runtime_provider_kind():
    """``runtime_binding._TRANSPORT_PROVIDER_KIND`` is an eighth
    capability-adjacent table, keyed by TRANSPORT rather than capability, and
    it is the one a lane flip hits first: binding a route over a wire with no
    arm raises unknown-transport at dispatch.

    Registering the Phase-4 wires now — rather than documenting a deferral —
    is what makes that enumeration unnecessary; this keeps it that way when a
    ninth wire appears."""
    from frisket.execution.runtime_binding import _TRANSPORT_PROVIDER_KIND
    from frisket.execution.targets import CAPABILITY_TRANSPORTS

    declared = set().union(*CAPABILITY_TRANSPORTS.values())
    missing = sorted(declared - set(_TRANSPORT_PROVIDER_KIND))
    assert not missing, (
        f"these declared wires have no _TRANSPORT_PROVIDER_KIND arm: {missing}. "
        "A route bound over one raises unknown-transport at dispatch, after "
        "the route row is already written."
    )


def test_every_declared_venue_has_a_runtime_provider_label():
    """The sibling table, keyed by target id
    (``runtime_binding._provider_for_target``), which raises on an
    unrecognized venue for the same reason and at the same moment."""
    from frisket.execution.definitions import build_static_targets
    from frisket.execution.runtime_binding import _provider_for_target

    for target in build_static_targets():
        assert _provider_for_target(target.id, target.operator)


def test_every_capability_has_at_least_one_declared_venue():
    """A capability nothing can run is a vocabulary entry, not a capability.
    This is what catches registering a capability without its target rows: the
    constant, the option type and the SKU would all exist and every run of it
    would refuse ``no_capable_target``."""
    from frisket.execution.definitions import build_static_targets
    from frisket.execution.targets import EXECUTION_CAPABILITIES

    served = {
        support.capability
        for target in build_static_targets()
        for support in target.engines
    }
    assert served == set(EXECUTION_CAPABILITIES), sorted(
        served.symmetric_difference(EXECUTION_CAPABILITIES)
    )


def test_a_row_may_only_speak_its_own_capabilitys_wires():
    """The full negative matrix: every capability × every OTHER capability's
    transports must refuse at construction.

    A global "is this any known transport" union accepts
    ``transport="deepl.v2"`` on a transcription row. Nothing catches it until
    dispatch, where ``runtime_binding`` raises unknown-transport with a route
    already persisted — a declaration error surfacing as a runtime failure, at
    the worst possible moment. Validating against the declaring capability's
    own wire set is what makes it a construction-time refusal.

    Shared wires are the reason this is a matrix and not a partition:
    ``local`` is legitimately a transcribe, ocr, to_markdown AND translate
    wire, and ``datalab.convert`` is legitimately both an ocr and a
    to_markdown one. Those pairs must stay ACCEPTED while every genuinely
    foreign pair refuses.
    """
    from frisket.contracts.transcription_sidecar import TranscriptionOptionSupport
    from frisket.execution.targets import (
        CAPABILITY_OPTION_SUPPORT,
        CAPABILITY_TRANSPORTS,
    )

    def _options(capability: str):
        model = CAPABILITY_OPTION_SUPPORT[capability]
        # The mirrored transcription type has no defaults (it mirrors a wire
        # contract verbatim); every other capability's does.
        if model is TranscriptionOptionSupport:
            return _support()
        return model()

    checked_refusals = 0
    checked_accepts = 0
    for capability, wires in CAPABILITY_TRANSPORTS.items():
        options = _options(capability)
        for other_wires in CAPABILITY_TRANSPORTS.values():
            for wire in other_wires:
                kwargs = dict(
                    engine="e", transport=wire, capability=capability, options=options
                )
                if wire in wires:
                    assert TargetEngineSupport(**kwargs)  # type: ignore[arg-type]
                    checked_accepts += 1
                    continue
                with pytest.raises(ValueError, match="is not a"):
                    TargetEngineSupport(**kwargs)  # type: ignore[arg-type]
                checked_refusals += 1
    # Non-vacuity: the matrix must actually contain foreign pairs AND shared
    # ones, or it would be asserting nothing in one direction.
    assert checked_refusals > 20, checked_refusals
    assert checked_accepts > 6, checked_accepts


def test_a_row_carrying_another_capabilitys_option_type_refuses():
    """``CAPABILITY_OPTION_SUPPORT`` is enforced at construction, per
    capability — including for the two Phase-4 types that declare no option
    fields, which are structurally identical and must still not substitute for
    each other."""
    from frisket.execution.targets import (
        CAPABILITY_CENSUS,
        CAPABILITY_TO_MARKDOWN,
        CensusOptionSupport,
        GeocodeOptionSupport,
        ToMarkdownOptionSupport,
        TranslateOptionSupport,
    )

    with pytest.raises(ValueError, match="must be a TranslateOptionSupport"):
        TargetEngineSupport(
            engine="deepl",
            transport="deepl.v2",
            capability="translate",
            options=GeocodeOptionSupport(),
        )
    # The two field-less types are different classes, and isinstance still
    # discriminates them — a census row cannot pass as a to_markdown one.
    with pytest.raises(ValueError, match="must be a ToMarkdownOptionSupport"):
        TargetEngineSupport(
            engine="datalab",
            transport="datalab.convert",
            capability=CAPABILITY_TO_MARKDOWN,
            options=CensusOptionSupport(),
        )
    with pytest.raises(ValueError, match="must be a CensusOptionSupport"):
        TargetEngineSupport(
            engine="us_census_acs",
            transport="census.acs5",
            capability=CAPABILITY_CENSUS,
            options=ToMarkdownOptionSupport(),
        )
    assert TargetEngineSupport(
        engine="opus_mt",
        transport="local",
        capability="translate",
        options=TranslateOptionSupport(source_language=True, auto_detect_source=False),
    )
