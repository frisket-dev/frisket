"""Direct live parity between the two transcription contract copies.

The shared golden fixture (sidecar/tests/fixtures/transcription.golden.json)
proves both copies ACCEPT the same two examples — but an optional field added
to one copy, a loosened bound, or a widened enum would keep accepting the
golden payloads and never fail CI. This suite instead compares the declared
schemas themselves, model pair by model pair, directly against each other:
this proves the two live copies agree today. A unilateral drift in one copy
fails; a synchronized schema, default, configuration, or model inventory
edit of both live copies passes.

ACCEPTED COST: this proves the copies agree TODAY, not that a released
version stayed compatible — a synchronized breaking edit to both copies
passes. Correct trade for a zero-user alpha where app and sidecar always
deploy from the same pinned release (see
sidecar/src/frisket_models/transcription/PROTOCOL.md); revisit if a deployed
fleet can ever run mismatched versions.

Import strategy: the main ``frisket`` package is NOT installed in the sidecar
venv (it is a separately packaged service by design), but the app's mirror
module (``src/frisket/contracts/transcription_sidecar.py``) is deliberately
self-contained — stdlib + pydantic only — so it is loaded here by file path
from the monorepo checkout.

What is compared, per mirrored pair:

* set of field names;
* per-field requiredness (required vs has-default) and default values;
* declared constraints and value sets, via each side's pydantic-generated
  JSON schema, normalized for pure representation differences (StrEnum vs
  Literal both emit the same enum value sets; integer ``gt=0`` == ``ge=1``);
* strict/closed/frozen model config.

Known deliberate divergences are asserted explicitly in ALLOWED_DIVERGENCES —
never silently skipped. Runtime helper METHODS (``validate_options``,
``supplied_options``, ...) are gateway/worker behavior, not fields, so they
are outside a field-level comparison by construction. The sidecar's
worker-side-only models (WorkerTranscriptionResponse, EngineProbe,
WorkerCapabilities) have no app mirror on purpose: the app speaks only
Boundary A.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from frisket_models.transcription import contract as sidecar

_APP_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "frisket"
    / "contracts"
    / "transcription_sidecar.py"
)


def _load_app_contract_module():
    # Standalone sidecar checkouts (the package ships separately) have no app
    # tree; in the monorepo — where CI runs this suite — it is always present.
    if not _APP_MODULE_PATH.exists():
        pytest.skip("app contract mirror not present (standalone sidecar checkout)")
    spec = importlib.util.spec_from_file_location(
        "_app_transcription_contract", _APP_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    # Registered before exec so pydantic can resolve the module's deferred
    # (`from __future__ import annotations`) forward references. A failed
    # exec must not leave a half-initialized module poisoning sys.modules
    # for later in-process collection (xdist worker reuse).
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


app = _load_app_contract_module()

# app-copy class name -> sidecar source-of-truth class name. Grep-complete
# over both modules' wire models: every app class mirrors exactly one sidecar
# class (checked exhaustively in test_every_app_wire_model_is_compared).
MIRRORED_PAIRS: tuple[tuple[str, str], ...] = (
    ("TranscriptionOptions", "TranscribeOptions"),
    ("TranscriptionOptionSupport", "TranscriptionOptionSupport"),
    ("TranscriptionEngineDescriptor", "TranscriptionEngineDescriptor"),
    ("TranscriptionWord", "TranscribeWord"),
    ("TranscriptionSegment", "TranscribeSegment"),
    ("TranscriptionResult", "TranscribeResult"),
    ("TranscriptionResponse", "GatewayTranscriptionResponse"),
    ("TranscriptionError", "TranscriptionError"),
    ("TranscriptionErrorEnvelope", "TranscriptionErrorEnvelope"),
)

_PAIR_IDS = [f"{a}~{b}" for a, b in MIRRORED_PAIRS]

# The one deliberate field-level divergence between the copies.
#
# accepted_options keys: the sidecar declares dict[TranscribeOptionName, ...]
# (JSON schema: a propertyNames enum over the v1 option names); the app copy
# declares dict[str, ...] and constrains the keys behaviorally instead — its
# before-validator revalidates the mapping through TranscriptionOptions,
# which is extra="forbid", so unknown keys are still rejected at runtime. The
# app cannot import the sidecar's StrEnum, and duplicating the enum would be a
# second vocabulary to drift. Asserted here (sidecar side MUST carry exactly
# this enum; app side MUST NOT), then normalized away — never silently skipped.
_ACCEPTED_OPTION_NAMES = frozenset(
    option.value for option in sidecar.TranscribeOptionName
)


def _normalize(
    node: Any, *, strip_option_enum: bool, stripped: list, path: str = "$"
) -> Any:
    """Normalize representation-only differences in a $ref-resolved schema.

    - drop title/description (cosmetic);
    - integer ``exclusiveMinimum: n`` -> ``minimum: n + 1`` (gt vs ge spelling
      of the identical integer value set);
    - sort enum/required lists (compare value SETS);
    - when ``strip_option_enum`` (sidecar side), remove the accepted_options
      propertyNames enum ONLY at its known schema path. An unscoped strip
      could mask the intended enum vanishing from
      ``accepted_options`` while an equivalent enum appeared elsewhere and
      still satisfy the count, after checking it is exactly the v1
      option-name set; each removal records its exact path in ``stripped``.
    """
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in ("title", "description"):
                continue
            if (
                key == "propertyNames"
                and isinstance(value, dict)
                and "enum" in value
                and strip_option_enum
            ):
                assert path.endswith("/accepted_options"), (
                    "propertyNames enum found OUTSIDE accepted_options at "
                    f"{path} — relocation, not sanctioned representation "
                    "difference"
                )
                assert set(value["enum"]) == _ACCEPTED_OPTION_NAMES, (
                    "sidecar accepted_options key enum drifted from "
                    "TranscribeOptionName"
                )
                stripped.append(path)
                continue
            out[key] = _normalize(
                value,
                strip_option_enum=strip_option_enum,
                stripped=stripped,
                path=f"{path}/{key}",
            )
        if out.get("type") == "integer" and "exclusiveMinimum" in out:
            out["minimum"] = out.pop("exclusiveMinimum") + 1
        for list_key in ("enum", "required"):
            if list_key in out and isinstance(out[list_key], list):
                out[list_key] = sorted(out[list_key])
        return out
    if isinstance(node, list):
        return [
            _normalize(
                item, strip_option_enum=strip_option_enum, stripped=stripped, path=path
            )
            for item in node
        ]
    return node


def _resolve_refs(node: Any, defs: dict) -> Any:
    if isinstance(node, dict):
        if "$ref" in node:
            target = dict(defs[node["$ref"].rsplit("/", 1)[-1]])
            target.update({k: v for k, v in node.items() if k != "$ref"})
            return _resolve_refs(target, defs)
        return {key: _resolve_refs(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(item, defs) for item in node]
    return node


def _comparable_schema(
    model: type[BaseModel], *, strip_option_enum: bool
) -> tuple[dict, int]:
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})
    resolved = _resolve_refs(schema, defs)
    stripped: list = []
    normalized = _normalize(
        resolved, strip_option_enum=strip_option_enum, stripped=stripped
    )
    return normalized, len(stripped)


# How many accepted_options key-enum occurrences the sidecar schema must carry
# per pair (nested inlining repeats it inside the response envelope). Zero for
# every model that has no accepted_options anywhere.
_EXPECTED_OPTION_ENUM_STRIPS = {
    "TranscribeResult": 1,
    "GatewayTranscriptionResponse": 1,
}


@pytest.mark.parametrize(("app_name", "sidecar_name"), MIRRORED_PAIRS, ids=_PAIR_IDS)
def test_field_names_requiredness_and_defaults_match(app_name, sidecar_name):
    app_model: type[BaseModel] = getattr(app, app_name)
    sidecar_model: type[BaseModel] = getattr(sidecar, sidecar_name)

    assert set(app_model.model_fields) == set(sidecar_model.model_fields), (
        f"{app_name} field names drifted from {sidecar_name}"
    )
    for name in app_model.model_fields:
        app_field = app_model.model_fields[name]
        sidecar_field = sidecar_model.model_fields[name]
        assert app_field.is_required() == sidecar_field.is_required(), (
            f"{app_name}.{name} requiredness drifted from {sidecar_name}.{name}"
        )
        if not app_field.is_required():
            app_default = app_field.get_default(call_default_factory=True)
            sidecar_default = sidecar_field.get_default(call_default_factory=True)
            assert app_default is not PydanticUndefined
            assert app_default == sidecar_default, (
                f"{app_name}.{name} default drifted from {sidecar_name}.{name}"
            )


@pytest.mark.parametrize(("app_name", "sidecar_name"), MIRRORED_PAIRS, ids=_PAIR_IDS)
def test_declared_constraints_and_value_sets_match(app_name, sidecar_name):
    """Deep parity via each side's generated JSON schema: constraints
    (min/max_length, pattern, numeric bounds, min_length on lists), enum and
    Literal VALUE SETS, closedness, and nested-model shape all compile into
    the schema, so a loosened bound on either side fails here."""
    app_schema, app_strips = _comparable_schema(
        getattr(app, app_name), strip_option_enum=False
    )
    sidecar_schema, sidecar_strips = _comparable_schema(
        getattr(sidecar, sidecar_name), strip_option_enum=True
    )
    # The ALLOWED divergence must actually exist where expected — an allowance
    # that stops matching anything is itself drift.
    assert app_strips == 0
    assert sidecar_strips == _EXPECTED_OPTION_ENUM_STRIPS.get(sidecar_name, 0), (
        f"{sidecar_name}: accepted_options key-enum occurrences changed"
    )
    assert app_schema == sidecar_schema, (
        f"declared schema drifted between {app_name} and {sidecar_name}"
    )


@pytest.mark.parametrize(("app_name", "sidecar_name"), MIRRORED_PAIRS, ids=_PAIR_IDS)
def test_both_copies_stay_strict_closed_and_frozen(app_name, sidecar_name):
    for model in (getattr(app, app_name), getattr(sidecar, sidecar_name)):
        assert model.model_config.get("extra") == "forbid"
        assert model.model_config.get("strict") is True
        assert model.model_config.get("frozen") is True


def test_context_bound_is_the_shared_500():
    """The `context` cap is one value on three surfaces: pinned literally here
    so neither copy can drift independently of the app constant."""
    assert app.TRANSCRIPTION_CONTEXT_MAX_CHARS == 500
    for model, name in (
        (app.TranscriptionOptions, "app"),
        (sidecar.TranscribeOptions, "sidecar"),
    ):
        schema = model.model_json_schema()
        arms = schema["properties"]["context"]["anyOf"]
        (string_arm,) = [arm for arm in arms if arm.get("type") == "string"]
        assert string_arm["maxLength"] == 500, f"{name} context cap drifted"
        assert string_arm["minLength"] == 1, f"{name} context non-emptiness drifted"


def test_contract_version_and_endpoint_literals_match():
    """Direct copy-to-copy equality, plus exact literal pins: neither side may
    silently drift off the wire values the other boundary, deployment config,
    and the web client all key off."""
    assert app.TRANSCRIPTION_CONTRACT_VERSION == sidecar.CONTRACT_VERSION
    assert app.TRANSCRIPTION_ENDPOINT == sidecar.TRANSCRIBE_ENDPOINT
    assert app.TRANSCRIPTION_CONTRACT_VERSION == "frisket.transcription.v1"
    assert app.TRANSCRIPTION_ENDPOINT == "/v1/transcribe"
    assert sidecar.CONTRACT_VERSION == "frisket.transcription.v1"
    assert sidecar.TRANSCRIBE_ENDPOINT == "/v1/transcribe"


def test_every_app_wire_model_is_compared():
    """Adding a wire model to the app copy without pairing it here must fail:
    an unmirrored model is exactly the silent-drift hole this suite closes."""
    app_wire_models = {
        name
        for name, obj in vars(app).items()
        if isinstance(obj, type)
        and issubclass(obj, BaseModel)
        and obj is not BaseModel
        and name
        not in {
            "TranscriptionWireModel",  # the shared config base
            # App-private subprocess materialization of the same v1 request
            # metadata. The sidecar transports audio as multipart bytes and
            # therefore has no matching path-bearing wire model.
            "LocalFasterWhisperRequest",
        }
    }
    assert app_wire_models == {pair[0] for pair in MIRRORED_PAIRS}
