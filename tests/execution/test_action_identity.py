"""Consent identity follows typed authoring, not mutable execution copies."""

from __future__ import annotations

import traceback

import pytest

from frisket.actions.core import routed_capability
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, SheetRows
from frisket.engine.executor.map_rows_action import (
    _typed_map_rows_plan,
    typed_queued_map_spec,
    typed_request_hash,
)
from frisket.execution.action_identity import (
    IDENTITY_EXCLUDED_KEYS,
    NON_IDENTITY_RUNNER_SPEC_KEYS,
    STRUCTURAL_RUNNER_SPEC_KEYS,
    UnknownActionIdentityKind,
    action_identity_hash,
    identity_fields,
    runner_spec_allowlist,
)


def _bound(**params):
    action = ACTION_REGISTRY.get("media.transcribe")
    return BoundTypedActionRequest.bind(
        action,
        ActionRequest(
            action_id=action.action_id,
            scope=SheetRows(sheet_id=3),
            params={"source": "media", "engine": "faster_whisper", **params},
            idempotency_key="transcribe-identity",
        ),
    )


def _spec(**params):
    return _typed_map_rows_plan(_bound(**params)).spec


def test_runner_annotations_and_copied_options_cannot_shift_typed_identity():
    spec = _spec()
    polluted = {
        **spec,
        "engine": "untrusted-copy",
        "diarize": True,
        "language": ["fr"],
        "output_names": {"text": "other"},
        "halted_code": "promise_violation",
        "halted_reason": "prior halt",
        "group_label": "group",
        "overwrite": True,
        "_frisket_queued_action_run": {"schema_version": "x"},
        "edition_run_context": {"edition": "open"},
        "future_runner_key": "ignored",
    }
    assert action_identity_hash(spec) == action_identity_hash(polluted)


def test_flow_flags_and_backfill_row_scope_do_not_shift_consent():
    spec = _spec()
    assert action_identity_hash(spec) == action_identity_hash(
        {
            **spec,
            "confirmed": True,
            "consented_promise_set_hash": "deadbeef",
            "row_ids": [1, 2, 3],
        }
    )


@pytest.mark.parametrize(
    "params",
    [
        {"engine": "whisper-turbo"},
        {"source": "audio"},
        {"language": ["fr"]},
        {"model_size": "base"},
        {"vad": False},
    ],
)
def test_actual_authored_parameters_change_identity(params):
    assert action_identity_hash(_spec(**params)) != action_identity_hash(_spec())


def test_source_sheet_is_part_of_identity():
    spec = _spec()
    assert action_identity_hash({**spec, "sheet_id": 4}) != action_identity_hash(spec)
    assert action_identity_hash({**spec, "sheet_id": 3.0}) == action_identity_hash(spec)


def test_default_presence_is_preserved_when_it_changes_engine_behavior():
    omitted = _bound(engine="openrouter/microsoft/mai-transcribe-2")
    disabled = _bound(engine="openrouter/microsoft/mai-transcribe-2", diarize=False)
    assert omitted.params.model_dump() == disabled.params.model_dump()
    omitted_spec = _typed_map_rows_plan(omitted).spec
    disabled_spec = _typed_map_rows_plan(disabled).spec
    assert omitted_spec["diarize"] is True
    assert disabled_spec["diarize"] is False
    assert typed_request_hash(omitted) != typed_request_hash(disabled)
    assert action_identity_hash(omitted_spec) != action_identity_hash(disabled_spec)


def test_supplied_vad_is_not_the_same_target_request_as_omission():
    omitted, supplied = _spec(), _spec(vad=True)
    assert "vad" not in omitted and supplied["vad"] is True
    assert action_identity_hash(omitted) != action_identity_hash(supplied)


def test_ordinary_default_equivalence_survives_option_presence_fix():
    assert action_identity_hash(_spec()) == action_identity_hash(_spec(diarize=False))


@pytest.mark.parametrize(
    "params",
    [
        {"unknown_number": 1.25},
        {"language": {"a": [0.5]}},
        {"num_speakers": 2.0, "diarize": True},
    ],
)
def test_malformed_typed_params_refuse_before_identity(params):
    spec = _spec()
    with pytest.raises(ValueError):
        action_identity_hash({**spec, "params": {**spec["params"], **params}})


def test_identity_is_bare_hex_sha256():
    digest = action_identity_hash(_spec())
    assert len(digest) == 64 and set(digest) <= set("0123456789abcdef")


def test_every_routed_typed_action_uses_its_validated_request_not_runner_copies():
    checked = set()
    for action in ACTION_REGISTRY.actions:
        if routed_capability(action.definition.run) is None:
            continue
        request = ActionRequest.model_validate(action.catalog_entry()["examples"][0])
        bound = BoundTypedActionRequest.bind(action, request)
        _, queued, program = typed_queued_map_spec(bound)
        spec = queued.runner_spec_fn(bound.params)
        assert program.consumes_resolution
        baseline = action_identity_hash(spec)
        assert (
            action_identity_hash(
                {
                    **spec,
                    "engine": "untrusted-runner-copy",
                    "output_names": {"value": "renamed"},
                }
            )
            == baseline
        )
        assert (
            action_identity_hash(
                {
                    **spec,
                    "params": {**spec["params"], "source": "different_source"},
                }
            )
            != baseline
        )
        with pytest.raises(ValueError):
            action_identity_hash(
                {
                    **spec,
                    "params": {**spec["params"], "unknown_number": 1.25},
                }
            )
        checked.add(action.action_id)
    assert checked == {
        "enrich.geocode",
        "enrich.census_demographics",
        "media.to_markdown",
        "media.ocr",
        "media.transcribe",
    }


def test_typed_specs_rebind_after_queue_serialization_without_changing_identity():
    from frisket.engine.executor.map_rows_action import (
        bound_typed_program_request_from_runner_spec,
    )

    for bound in (
        _bound(language=["en"], vad=True, model_size="base"),
        _bound(engine="parakeet-tdt", diarize=True),
        _bound(engine="openrouter/microsoft/mai-transcribe-2"),
    ):
        spec = _typed_map_rows_plan(bound).spec
        rebound = bound_typed_program_request_from_runner_spec(spec)
        assert rebound is not None
        assert action_identity_hash(
            _typed_map_rows_plan(rebound).spec
        ) == action_identity_hash(spec)


def _translate_spec(**params):
    """An unrouted typed model program: identity must project through its
    Params schema exactly as the routed programs above do."""
    action = ACTION_REGISTRY.get("map.translate")
    bound = BoundTypedActionRequest.bind(
        action,
        ActionRequest(
            action_id=action.action_id,
            scope=SheetRows(sheet_id=3),
            params={
                "source": ["text"],
                "model": "anthropic/claude-haiku-4-5",
                "target_language": "Spanish",
                **params,
            },
            idempotency_key="translate-identity",
        ),
    )
    return _typed_map_rows_plan(bound).spec


def test_unrouted_typed_identity_has_no_second_declaration_authority():
    # The op-declaration allowlist is retired for every kind: nothing remains
    # to project a runner spec through, and identity never falls back to it.
    with pytest.raises(
        UnknownActionIdentityKind, match="has no registered op declaration"
    ):
        runner_spec_allowlist("map.translate")
    with pytest.raises(
        UnknownActionIdentityKind, match="never falls back to hashing the raw spec"
    ):
        identity_fields("map.translate")
    # The structural/flow key rosters the projection excludes stay disjoint
    # from what any declaration could ever have re-admitted.
    assert IDENTITY_EXCLUDED_KEYS.isdisjoint(STRUCTURAL_RUNNER_SPEC_KEYS)
    assert NON_IDENTITY_RUNNER_SPEC_KEYS.isdisjoint(STRUCTURAL_RUNNER_SPEC_KEYS)

    spec = _translate_spec()
    baseline = action_identity_hash(spec)
    # Runner-envelope copies, flow flags, and backfill scope never shift it.
    assert (
        action_identity_hash(
            {
                **spec,
                "engine": "untrusted-runner-copy",
                "output_names": {"translation": "renamed"},
                "row_ids": [1, 2, 3],
                "confirmed": True,
                "consented_promise_set_hash": "deadbeef",
                "halted_code": "promise_violation",
            }
        )
        == baseline
    )
    # Authored intent does.
    assert action_identity_hash(_translate_spec(target_language="French")) != baseline
    assert action_identity_hash(_translate_spec(language=["fr"])) != baseline
    assert action_identity_hash({**spec, "sheet_id": 4}) != baseline


def test_typed_float_fence_remains_deterministic_and_redacted():
    """A malformed authored value refuses by validation type, never by value: this
    seam carries user-authored parameters and its refusal reaches logs."""
    spec = _translate_spec()
    malformed = {
        **spec,
        "params": {**spec["params"], "language": {"z": {"later": 0.75}, "a": [0.5]}},
    }
    messages = []
    for _ in range(2):
        with pytest.raises(ValueError) as exc:
            action_identity_hash(malformed)
        messages.append(str(exc.value))
    assert messages[0] == messages[1]
    assert messages[0] == 'action identity params validation failed: ["value_error"]'
    for authored_value in ("0.75", "0.5"):
        assert authored_value not in messages[0], (
            "identity refusal leaked an authored parameter value"
        )


@pytest.mark.parametrize("placement", ["nested_map_key", "extra_field"])
def test_identity_validation_never_echoes_user_authored_keys(placement):
    canary = "private-investigation-key-8d751a"
    params = {
        "source": ["text"],
        "fields": [{"name": "category", "labels": ["yes", "no"]}],
    }
    if placement == "nested_map_key":
        params["fields"][0].update(labels=[canary], label_descriptions={canary: 7})
        expected_code = "string_type"
    else:
        params[canary] = "private-extra-value"
        expected_code = "extra_forbidden"
    with pytest.raises(ValueError) as caught:
        action_identity_hash(
            {"action_kind": "map.classify", "sheet_id": 3, "params": params}
        )
    assert str(caught.value) == (
        'action identity params validation failed: ["' + expected_code + '"]'
    )
    for rendered in (
        str(caught.value),
        repr(caught.value),
        "".join(traceback.format_exception(caught.value)),
    ):
        assert canary not in rendered
        assert "private-extra-value" not in rendered


def test_typed_nested_identity_material_hashes_independent_of_key_order():
    left = _translate_spec(language=["fr"], context="Court filings")
    right = {**left, "params": dict(reversed(list(left["params"].items())))}
    assert list(right["params"]) != list(left["params"])
    assert action_identity_hash(left) == action_identity_hash(right)
    assert action_identity_hash(
        _translate_spec(language=["de"], context="Court filings")
    ) != action_identity_hash(left)


def test_unrouted_ytdlp_fractional_sleep_interval_remains_valid():
    from frisket.actions.media_download import MediaDownloadParams
    from frisket.ops.ytdlp import validate_extra_opts

    params = MediaDownloadParams(source="url", extra_opts={"sleep_interval": 0.5})
    validate_extra_opts(params.extra_opts or {})
    assert params.extra_opts == {"sleep_interval": 0.5}
    request = ActionRequest(
        action_id="media.ytdlp_download",
        scope=SheetRows(sheet_id=3),
        params=params.model_dump(mode="json"),
        idempotency_key="fractional-sleep",
    )
    registered = ACTION_REGISTRY.get(request.action_id)
    first = BoundTypedActionRequest.bind(registered, request)
    second = BoundTypedActionRequest.bind(
        registered,
        request.model_copy(
            update={
                "params": {**request.params, "extra_opts": {"sleep_interval": 0.75}}
            }
        ),
    )
    assert typed_request_hash(first) != typed_request_hash(second)


@pytest.mark.parametrize(
    "spec",
    [
        {"action_kind": "not.a.real.op", "sheet_id": 1},
        {"sheet_id": 1, "engine": "parakeet-tdt"},
    ],
)
def test_unknown_or_missing_action_kind_fails_loud(spec):
    with pytest.raises(UnknownActionIdentityKind):
        action_identity_hash(spec)


def test_private_recipe_identity_is_never_accepted():
    with pytest.raises(UnknownActionIdentityKind, match="must not carry"):
        action_identity_hash({**_spec(), "recipe": "transcribe"})
