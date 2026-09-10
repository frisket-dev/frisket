"""The unrouted confirmation hash reads a money projection, not the estimate.

``confirmation_context_hash`` is minted when a 402 quote is shown and
RECOMPUTED at dispatch (``frisket.execution.attempt_authority``) from a fresh
``estimate_run``; the two are compared by exact string equality, and a
mismatch refuses the run ``consent_missing``. So every field inside the hash
is a field that can silently invalidate a persisted consent.

Before ``ConsentQuote`` the whole estimate dict went in verbatim, which
made that set unbounded: a page count, a token sample, or a new display key
added by any recipe's ``estimate()`` was consent-bearing by accident. Now the
hash reads a declared projection of the money-bearing facts.

Two halves, both load-bearing:

* the golden digests freeze the canonical serialization — key set, key
  spelling, float rendering, and the payload's ``quote`` slot — so a
  drift that would invalidate live consents fails BY NAME here rather than
  silently at somebody's dispatch;
* the sensitivity tests prove the projection is neither too small (every
  money field still moves the digest) nor too large (a display field does
  not).
"""

from __future__ import annotations

from typing import Any

import pytest

from frisket.engine.runner.confirmation_context import (
    KNOWN_DISPLAY_KEYS,
    ConsentQuoteRefused,
    ConsentQuote,
    rate_estimate,
)
from frisket.engine.runner.validation import confirmation_context_hash
from frisket.execution.pricing_policy import IDENTITY_PRICING_POLICY


class _StubProject:
    """Just the two reads ``canonical_work_scope_binding`` makes. The scope
    half of the payload is held fixed on purpose: these tests move only the
    estimate."""

    def columns(self, sheet_id: int) -> list[dict[str, Any]]:
        return [{"id": 11, "name": "address"}, {"id": 12, "name": "notes"}]

    def get_values_with_refs(
        self, sheet_id: int, column_id: int, *, row_ids: list[int]
    ) -> tuple[dict[int, Any], dict[int, Any]]:
        values = {row_id: f"value-{column_id}-{row_id}" for row_id in row_ids}
        refs = {
            row_id: {"kind": "source", "row_id": row_id, "column_id": column_id}
            for row_id in row_ids
        }
        return values, refs


class _StubRecipe:
    # The recipe lane's ``family_kind``: required, and inside the hash, so a
    # confirmation minted for one recipe can never satisfy another's gate.
    name = "geocode"

    def source_columns(self, spec: dict[str, Any]) -> list[str]:
        return ["address"]


SPEC: dict[str, Any] = {
    "sheet_id": 1,
    "action_kind": "enrich.geocode",
    "engine": "opencage",
    "input_columns": ["address"],
}
ROW_IDS = [1, 2, 3]


def rated_digest(estimate: dict[str, Any]) -> str:
    """Hash an ALREADY-rated estimate. The tests that move ``billed_cost`` or
    ``policy_id`` by hand use this; everything else goes through
    :func:`digest`, which rates first."""
    return confirmation_context_hash(
        _StubProject(),  # type: ignore[arg-type]
        _StubRecipe(),  # type: ignore[arg-type]
        dict(SPEC),
        list(ROW_IDS),
        estimate,
    )


def digest(estimate: dict[str, Any]) -> str:
    """The real path: rate through base's identity policy, then hash.

    Rating is not optional at a money mint — ``ConsentQuote`` refuses an
    estimate no policy has touched — so a test that hashed a bare producer
    shape would be testing a state that cannot reach a user.
    """
    return rated_digest(rate_estimate(estimate, policy=IDENTITY_PRICING_POLICY))


# Three shapes real producers emit on the unrouted path: a priced external
# call (ops/cost_source-free, from ai/external_pricing.estimate_external_cost
# via enrich.geocode), a declared-free local engine
# (ops/cost_source.free_local_estimate), and an unpriceable billable engine
# (ops/cost_source.unknown_cost_estimate, plus confirmation_estimate's remote
# capability). Each carries the display fields its producer really ships.
PRICED_ESTIMATE: dict[str, Any] = {
    "rows": 3,
    "cost": 0.0075,
    "pricing_key": "opencage.geocode.forward",
    "engine": "opencage",
    "cost_source": "pricing_data",
}
FREE_LOCAL_ESTIMATE: dict[str, Any] = {
    "rows": 3,
    "cost": 0.0,
    "engine": "rapidocr",
    "cost_source": "free_local",
}
UNKNOWN_COST_ESTIMATE: dict[str, Any] = {
    "rows": 3,
    "cost": None,
    "engine": "openai/gpt-4o-mini",
    "cost_source": "unknown",
    "warning": "page count is unknown before the run; the cost cannot be estimated",
    "requires_confirmation": True,
    "remote_capability": "external:ocr",
}

# Re-pinned 2026-08-29 when the canonical ``action_kind`` replaced Recipe.name
# as the family identity. Pre-release, zero users, no persisted consent to
# strand; every digest below now binds the public action identity directly.
GOLDEN = {
    "priced": (
        PRICED_ESTIMATE,
        "32b96f4c1e1c4cfe70a2591c31291e362f421190ef721606ae90c761a1adefac",
    ),
    "free_local": (
        FREE_LOCAL_ESTIMATE,
        "146ca64433a301524bc589566c9be33456610d9a393e100c733388dcb9790e29",
    ),
    "unknown_cost": (
        UNKNOWN_COST_ESTIMATE,
        "a0dba957c2219266724737943742d348611578d180d14ad0dc48e11d38bda39f",
    ),
}


@pytest.mark.parametrize("case", sorted(GOLDEN))
def test_confirmation_hash_matches_its_golden_digest(case: str) -> None:
    estimate, expected = GOLDEN[case]
    assert digest(estimate) == expected, (
        f"the {case} confirmation hash moved. Every consent row persisted "
        "under the old digest now refuses its own dispatch as "
        "consent_missing — re-pin this fixture only when that is intended."
    )


# The money-bearing set, one entry per ConsentQuote field that carries a
# value from the estimate. Each mutation is a DIFFERENT quote: a different
# dollar figure, a different quantity, a different rate identity, a different
# derivation, a different payee, or a different gate verdict.
MONEY_MUTATIONS: dict[str, dict[str, Any]] = {
    "cost": {"cost": 0.0125},
    "cost_none": {"cost": None},
    "rows": {"rows": 4},
    "pricing_key": {"pricing_key": "opencage.geocode.reverse"},
    "cost_source": {"cost_source": "unknown"},
    "engine": {"engine": "nominatim"},
    "requires_confirmation": {"requires_confirmation": True},
    "remote_capability": {"remote_capability": "external:geocode"},
}

# The RATED half moves the digest too, and it has to: a deployment that
# changes its tariff, or one whose policy declines to price a run it used to
# price, is offering a different quote. Applied AFTER rating (hence
# ``rated_digest``), because rating is what would otherwise overwrite them.
RATED_MUTATIONS: dict[str, dict[str, Any]] = {
    # the cost-plus case in miniature: same provider cost, higher bill
    "billed_cost": {"billed_cost": 21_000},
    "billed_cost_none": {"billed_cost": None},
    "policy_id": {"policy_id": "acme.cost_plus.v3"},
}


@pytest.mark.parametrize("field", sorted(RATED_MUTATIONS))
def test_rated_field_change_changes_the_digest(field: str) -> None:
    """The wave's central guarantee: the BILLED figure is inside the consent.

    Without this, a hosted deployment could quote $0.11, persist a consent
    over the provider facts alone, then bill $0.21 under a tariff the user
    never saw and never approved — and the dispatch fence would agree.
    """
    rated = rate_estimate(PRICED_ESTIMATE, policy=IDENTITY_PRICING_POLICY)
    baseline = rated_digest(rated)
    moved = rated_digest({**rated, **RATED_MUTATIONS[field]})
    assert moved != baseline, (
        f"{field} left the confirmation hash: a run could be billed under a "
        "price the user never approved"
    )


@pytest.mark.parametrize("field", sorted(MONEY_MUTATIONS))
def test_money_field_change_changes_the_digest(field: str) -> None:
    baseline = digest(PRICED_ESTIMATE)
    moved = digest({**PRICED_ESTIMATE, **MONEY_MUTATIONS[field]})
    assert moved != baseline, (
        f"{field} left the confirmation hash: a run could be dispatched "
        "under a consent the user gave for a different quote"
    )


@pytest.mark.parametrize("field", ["cost_source", "pricing_key"])
def test_dropping_rate_provenance_changes_the_digest(field: str) -> None:
    baseline = digest(PRICED_ESTIMATE)
    dropped = {key: value for key, value in PRICED_ESTIMATE.items() if key != field}
    assert digest(dropped) != baseline


# Presentation keys the first-party client renders alongside money facts.
DISPLAY_KEYS: dict[str, Any] = {
    "avg_input_tokens": 1200,
    "audio_seconds": 931.204,
    "warning": "audio duration metadata is missing; run metadata backfill",
    "billing_label": "metered and billed through Acme credits",
    "venue_label": "Acme-operated shared infrastructure",
    "claims": [{"field": "egress", "display": "External OCR"}],
    "promise_set_hash": "a" * 64,
}


def test_display_fields_do_not_change_the_digest() -> None:
    """The point of the projection. A recipe that adds a page count, a
    throughput hint, or any other render-only field to its estimate must not
    invalidate consents already persisted against that quote."""

    assert digest({**PRICED_ESTIMATE, **DISPLAY_KEYS}) == digest(PRICED_ESTIMATE)


@pytest.mark.parametrize(
    "field",
    # ``policy_id`` is excluded because it is REQUIRED: absent and null are
    # the same thing there, and that thing is a refusal, not a quote. Its
    # own behaviour is pinned by
    # ``test_an_unrated_estimate_refuses_to_mint_a_money_consent``.
    sorted(set(ConsentQuote.model_fields) - {"policy_id"}),
)
def test_absent_and_null_project_identically(field: str) -> None:
    """Producers are inconsistent about omitting versus nulling a key
    (``unknown_cost_estimate`` omits ``pricing_key``;
    ``estimate_external_cost`` can carry it as ``None``). Two spellings of
    the same fact are one consent, deliberately — including
    ``requires_confirmation``, whose absence every gate already reads as
    False."""

    rated = rate_estimate(PRICED_ESTIMATE, policy=IDENTITY_PRICING_POLICY)
    absent = {key: value for key, value in rated.items() if key != field}
    nulled = {**absent, field: None}
    assert rated_digest(nulled) == rated_digest(absent)


def test_basis_is_frozen_and_closed() -> None:
    basis = ConsentQuote.from_estimate(
        rate_estimate(PRICED_ESTIMATE, policy=IDENTITY_PRICING_POLICY)
    )
    with pytest.raises(ValueError):
        basis.cost = 1.0  # type: ignore[misc]
    with pytest.raises(ValueError):
        ConsentQuote(pages=42)  # type: ignore[call-arg]


def test_an_unrated_estimate_refuses_to_mint_a_money_consent() -> None:
    """THE pricing-policy fence, at the one projection every family reaches.

    A money gate that minted without asking a pricing policy would hash the
    PROVIDER figure while the deployment bills a marked-up one — the user
    approves $0.11 and is charged $0.21, with nothing red anywhere. There are
    fourteen mint sites and five of them are in executor families that no
    composition root can reach, so the fence lives here rather than at the
    call sites: a list of sites is something somebody has to keep complete.

    Sever ``rate_estimate`` from any family's mint and this is what fires.
    """
    with pytest.raises(ConsentQuoteRefused) as caught:
        rated_digest(PRICED_ESTIMATE)
    assert caught.value.key == "policy_id"


def test_billed_cost_must_be_whole_micro_dollars() -> None:
    """A rated figure is minted by a policy through ``usd_to_micros``, never
    typed by hand, so a float or a negative here is a broken policy putting a
    number the user never saw inside their consent."""
    rated = rate_estimate(PRICED_ESTIMATE, policy=IDENTITY_PRICING_POLICY)
    for bad in (0.0075, -1, "7500"):
        with pytest.raises(ConsentQuoteRefused) as caught:
            rated_digest({**rated, "billed_cost": bad})
        assert caught.value.key == "billed_cost"


# --- refusals -------------------------------------------------------------
#
# Goldens are structurally unable to catch this class: a value that COLLAPSES
# during serialization produces a perfectly stable digest, just the wrong
# one. These assert the refusal and the inequality directly.


NON_FINITE = {"nan": float("nan"), "inf": float("inf"), "-inf": float("-inf")}


@pytest.mark.parametrize("spelling", sorted(NON_FINITE))
def test_non_finite_cost_refuses(spelling: str) -> None:
    """Named at the projection, not four frames later. Without this the
    non-finite float reaches ``json.dumps(allow_nan=False)`` and comes back
    as a bare "Out of range float values" ValueError that identifies no key
    — and at dispatch escapes unhandled, where the only modeled outcome is
    refuse-and-re-confirm.

    It also decouples the refusal from the encoder: pydantic's
    ``ser_json_inf_nan`` default renders non-finite floats as JSON null, so
    a payload encoded via ``model_dump_json()`` instead of
    ``model_dump(mode="json")`` + ``json.dumps`` would hash NaN, Inf, and an
    honestly-unknown cost identically."""

    with pytest.raises(ConsentQuoteRefused) as caught:
        digest({**PRICED_ESTIMATE, "cost": NON_FINITE[spelling]})
    assert caught.value.key == "cost"


def test_non_finite_cost_never_matches_the_unknown_cost_quote() -> None:
    """The inequality the refusal protects, asserted against the digest that
    would swallow it: a run whose cost is not a number must never be
    admitted under the consent a user gave for a run whose cost is honestly
    unknown. Both sides are checked — the unknown quote still hashes, the
    non-finite ones produce no hash at all."""

    unknown = digest({**PRICED_ESTIMATE, "cost": None})
    assert len(unknown) == 64
    for value in NON_FINITE.values():
        with pytest.raises(ConsentQuoteRefused):
            digest({**PRICED_ESTIMATE, "cost": value})


# One malformed spelling per money field. Every one of these used to be
# either a silent coercion or a raw pydantic ValidationError; both are
# wrong at dispatch, where the only modeled outcome is refuse-and-reconfirm
# and an escaping exception makes a consented run permanently unrunnable.
MALFORMED: dict[str, tuple[str, Any]] = {
    "cost_string": ("cost", "1.00"),
    "cost_bool": ("cost", True),
    "rows_fractional": ("rows", 12.5),
    "rows_bool": ("rows", True),
    "cost_source_enum": ("cost_source", 3),
    "pricing_key_mapping": ("pricing_key", {"key": "x"}),
    "engine_list": ("engine", ["opencage"]),
    "remote_capability_int": ("remote_capability", 7),
    # F4: bool("false") is True, so a string spelling would invert the gate
    # verdict inside the hash.
    "requires_confirmation_string": ("requires_confirmation", "false"),
    "requires_confirmation_int": ("requires_confirmation", 1),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_malformed_money_value_refuses_by_name(case: str) -> None:
    key, value = MALFORMED[case]
    with pytest.raises(ConsentQuoteRefused) as caught:
        digest({**PRICED_ESTIMATE, key: value})
    assert caught.value.key == key
    # The offending VALUE never reaches the message (a pydantic
    # ValidationError would carry it into logs); the key name is the whole
    # diagnosis.
    assert repr(value) not in str(caught.value)


def test_unclassified_estimate_key_refuses() -> None:
    """The enumeration fence. ``extra="forbid"`` cannot fire — the
    projection passes fixed keyword arguments — so a producer adding a
    money-bearing key would leave the hash blind and a $1.00 consent would
    stay echo-valid for a $5.00 dispatch. An unclassified key refuses until
    a human files it as money (a basis field) or display (a roster line)."""

    with pytest.raises(ConsentQuoteRefused) as caught:
        digest({**PRICED_ESTIMATE, "surcharge": 4.0})
    assert caught.value.key == "surcharge"


def test_display_roster_and_basis_fields_are_disjoint() -> None:
    assert not KNOWN_DISPLAY_KEYS & set(ConsentQuote.model_fields)


def test_every_display_key_used_by_the_tests_is_rostered() -> None:
    """Keeps the insensitivity test honest: an unrostered key would now
    REFUSE, so that test would be asserting about a refusal rather than
    about insensitivity."""

    assert set(DISPLAY_KEYS) <= KNOWN_DISPLAY_KEYS


# --- reachability and closure ---------------------------------------------


def test_only_the_routed_recipes_consume_resolution() -> None:
    """The roster's `pages`/`audio_seconds`/`rows` entries rest on this.

    On an UNKNOWN-cost estimate those keys are the only quantity present —
    what `rows` is to a per-row op — so filing them as display-only is safe
    exactly as long as they cannot reach an unrouted consent. They cannot,
    because their producers (``OcrRecipe``, ``TranscribeRecipe``,
    ``ConvertMarkdownRecipe``, ``GeocodeRecipe``, ``CensusDemographicsRecipe``)
    declare ``consumes_resolution = True``, and a resolution-consuming recipe
    is gated by the claims machinery rather than by
    ``confirmation_context_hash``. ``pages`` is shared by OCR and to_markdown
    (one quantity, one meaning, per
    ``test_the_display_key_roster_already_covers_the_four_lanes``), so a
    third producer joining changes nothing about the key itself — only this
    set.

    If any declaration flips, this goes red and its quantity key must move up
    into ``ConsentQuote`` — otherwise an unpriced 900-page job and an
    unpriced 3-page job would share one consent.
    """

    from frisket.actions.core import routed_capability
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.system import BoundTypedActionRequest
    from frisket.actions.types import ActionRequest
    from frisket.engine.executor.map_rows_action import typed_queued_map_spec

    consuming = set()
    # Typed capabilities must compose the same routed gate; catalog metadata
    # alone is insufficient proof that their executable programs consume it.
    for action in ACTION_REGISTRY.actions:
        if routed_capability(action.definition.run) is None:
            continue
        request = ActionRequest.model_validate(action.catalog_entry()["examples"][0])
        bound = BoundTypedActionRequest.bind(action, request)
        _, _, program = typed_queued_map_spec(bound)
        assert program.consumes_resolution, action.action_id
        consuming.add(action.action_id)
    assert consuming == {
        "media.ocr",
        "media.transcribe",
        "media.to_markdown",
        "enrich.geocode",
        "enrich.census_demographics",
    }


# One pinned estimate output per typed program that OVERRIDES the shared
# program estimate() and can reach an unrouted consent, plus the envelope
# shapes the runner itself produces. Closure below makes a newly-overriding
# program red by name; the runtime fence in from_estimate is what catches a
# new KEY on an existing one, which is why this stays a pinned table rather
# than a live sweep.
#
# geocode/census_demographics left this table at the routed-capability rollout: both now declare
# ``consumes_resolution = True`` (see
# ``test_only_the_routed_recipes_consume_resolution`` above), so their
# bare ``resolution=None`` fallback can no longer reach an unrouted CONSENT;
# the routed arm uses the shared canonical CostBasis projector and is gated by
# the claims machinery.
#
# map.translate left at the typed-action cutover: the hosted translator
# (deepl) is a routed capability now, so its character-count quote is gated by
# the claims machinery, and no unrouted producer emits ``hosted_char_count``.
PRODUCER_SHAPES: dict[str, tuple[dict[str, Any], ...]] = {
    # to_markdown joined "ocr"/"transcribe" as consumes_resolution=True (Phase
    # 4): its bare estimate fallback still exists for direct preview callers,
    # but an action-driven estimate_run always resolves first, so it no
    # longer reaches an UNROUTED consent — same reasoning as ocr's absence
    # here.
    "cluster.values": (
        {
            "rows": 3,
            "cost": 0.0,
            "cost_source": "free_local",
        },
        {
            "rows": 3,
            "cost": None,
            "engine": "openai/text-embedding-3-small",
            "cost_source": "unknown",
            "requires_confirmation": True,
            "remote_capability": "model:embed",
        },
    ),
    "join.semantic": (
        {
            "rows": 3,
            "cost": 0.0,
            "cost_source": "free_local",
        },
        {
            "rows": 3,
            "cost": 0.00012,
            "cost_source": "pricing_data",
            "pricing_key": "openai/text-embedding-3-small.tokens",
            "requires_confirmation": True,
        },
    ),
}

# engine/runner/validation.py's own producers: unestimated_cost (both arms)
# and the LLM token sampler (priced and unpriced).
RUNNER_SHAPES: tuple[dict[str, Any], ...] = (
    {"rows": 3, "cost": 0.0, "cost_source": "free_local"},
    {
        "rows": 3,
        "cost": None,
        "cost_source": "unknown",
        "requires_confirmation": True,
    },
    # estimate_run's two LLM branches. The sampled quantity is display-only;
    # the catalog rate keeps its source and identity in the quote.
    {
        "rows": 3,
        "cost": 0.0042,
        "cost_source": "pricing_data",
        "pricing_key": "anthropic/claude-haiku-4-5.tokens",
        "avg_input_tokens": 1200,
    },
    {
        "rows": 3,
        "cost": None,
        "cost_source": "unknown",
        "avg_input_tokens": 1200,
    },
)


@pytest.mark.parametrize(
    "shape",
    [s for shapes in PRODUCER_SHAPES.values() for s in shapes] + list(RUNNER_SHAPES),
)
def test_pinned_producer_shapes_project_without_refusing(shape: dict[str, Any]) -> None:
    assert len(digest(shape)) == 64


def test_every_unrouted_recipe_overriding_estimate_has_a_pinned_shape() -> None:
    """Closure over the typed project-run programs, so roster drift is one
    named failure here rather than ``ConsentQuoteRefused`` surfacing in
    unrelated suites.

    The recipe registry is retired; every queued program is reconstructed
    from its canonical ActionRequest at the queue seam, so the sweep walks the
    declared queued placement and each kind's catalog examples. The shared
    ``_TypedMapRowsProgram.estimate`` returns ``None`` off the routed arm (the
    runner's own producers then apply, pinned in ``RUNNER_SHAPES``), so only a
    program class that REPLACES it can mint an unrouted quote of its own.
    """

    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.engine.executor.action_specs import queued_project_run_kinds
    from frisket.engine.executor.map_rows_action import _TypedMapRowsProgram
    from frisket.engine.executor.queued_actions import queued_v1_action_request

    overriding: set[str] = set()
    swept = 0
    for kind in sorted(queued_project_run_kinds()):
        examples = ACTION_REGISTRY.get(kind).catalog_entry()["examples"]
        assert examples, f"{kind} ships no catalog example to sweep"
        for example in examples:
            queued = queued_v1_action_request(example)
            assert queued is not None, kind
            swept += 1
            program = queued.program
            if program.consumes_resolution:
                continue
            if type(program).estimate is not _TypedMapRowsProgram.estimate:
                overriding.add(kind)
    assert swept >= 20
    assert overriding == set(PRODUCER_SHAPES), (
        "a typed program that can mint an unrouted consent grew (or lost) its "
        "own estimate(); pin its output shape above so the money projection is "
        "checked against it"
    )


# ---------------------------------------------------------------------------
# The envelope's free-form payloads are hashed, so they need the same
# non-finite discipline the quote's own `cost` gets.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_anywhere_in_the_envelope_refuses(bad: float) -> None:
    """A non-finite float in a scope/bindings payload must refuse, not null.

    The envelope carries free-form ``dict[str, Any]`` slots — a runner spec,
    a params dump, a family's live bindings. Pydantic's DEFAULT
    ``ser_json_inf_nan`` renders every non-finite float in those as JSON
    ``null``, which would make a spec carrying NaN, one carrying +Inf, and
    one carrying an honest ``None`` mint the SAME consent: three different
    runs, one approval, and the dispatch-time recompute agreeing with all
    three. ``_HASHABLE_PAYLOAD`` pins ``ser_json_inf_nan="constants"`` so the
    ``allow_nan=False`` encode refuses instead — the behavior the raw
    ``json.dumps`` of the pre-consolidation payloads had.

    Flip that one config value and this test is the thing that notices.
    """
    from frisket.engine.runner.confirmation_context import (
        ActionScope,
        ParamsScope,
        action_confirmation,
        mint_confirmation_hash,
        scope_confirmation,
    )

    envelopes = [
        # a money gate's live bindings
        action_confirmation(
            family_kind="sheet.refresh",
            scope=ActionScope(action_hash="sha256:x"),
            estimate={
                "cost": 1.0,
                "threshold": bad,
                "billed_cost": 1_000_000,
                "policy_id": "frisket.pricing.identity.v1",
            },
        ),
        # a scope gate's bindings
        scope_confirmation(
            family_kind="derive.join",
            scope=ParamsScope(params={"a": 1}),
            bindings={"estimated_rows": bad},
        ),
        # the params identity itself
        scope_confirmation(
            family_kind="derive.join",
            scope=ParamsScope(params={"ratio": bad}),
            bindings={"estimated_rows": 9},
        ),
    ]
    for envelope in envelopes:
        with pytest.raises(ValueError, match="not JSON compliant"):
            mint_confirmation_hash(envelope)


def test_a_null_binding_and_a_non_finite_one_are_not_one_consent() -> None:
    """The collision the config prevents, stated directly: if a non-finite
    binding ever hashes at all, it must not hash as the honest ``None``."""
    from frisket.engine.runner.confirmation_context import (
        ParamsScope,
        mint_confirmation_hash,
        scope_confirmation,
    )

    def envelope(value: Any) -> Any:
        return scope_confirmation(
            family_kind="derive.join",
            scope=ParamsScope(params={"a": 1}),
            bindings={"estimated_rows": value},
        )

    null_digest = mint_confirmation_hash(envelope(None))
    assert len(null_digest) == 64
    with pytest.raises(ValueError, match="not JSON compliant"):
        mint_confirmation_hash(envelope(float("nan")))
