"""Promise module battery.

Derived from the generic-predicate-list spike, plus the v3 additions:
``unbounded`` semantics, basis-keyed ratchet (including the
incompatible-basis non-ratchet case), the coverage-envelope computation,
and the canonical hash contract (dict-order invariance, domain separation,
display-field exclusion, NFC, float rejection).
"""

from __future__ import annotations

import ast
import json
import random
from pathlib import Path

import pytest

from frisket.execution.commercial import LIVE_COST_TERM_KEYS
from frisket.execution.promises import (
    DOMAIN_TEST,
    EGRESS_ORDER_REF,
    PROMISE_FIELDS,
    REGISTRY,
    SATISFIED,
    UNEVALUABLE,
    VIOLATED,
    OrderTable,
    Promise,
    PromiseSet,
    RegistryError,
    ThroughputTable,
    basis_identity,
    builtin_registry,
    canonical_json,
    claim_covered_by_envelope,
    compute_coverage_envelope,
    content_hash,
    evaluate as _evaluate,
    evaluate_set as _evaluate_set,
    promise_row_fingerprint,
    promise_set_hash,
)

PRICING_KEY = "test.hosted_compute.accelerator_second"
THROUGHPUT_REF = "accelerator_throughput.v1"
_ABSENT = object()

TEST_REGISTRY = builtin_registry()
TEST_REGISTRY.publish(
    "throughput",
    THROUGHPUT_REF,
    ThroughputTable(
        factors=(
            ("slow", "0.4"),
            ("baseline", "1"),
            ("fast", "2"),
        )
    ),
)


def evaluate(promise, binding):
    """Evaluate against neutral, test-local throughput data."""
    return _evaluate(promise, binding, TEST_REGISTRY)


def evaluate_set(promises, binding):
    """Evaluate a set against neutral, test-local throughput data."""
    return _evaluate_set(promises, binding, TEST_REGISTRY)


def _gates(promises) -> bool:
    """The gate renders exactly when at least one ``user_claim`` row exists.

    The production copy is inline in ``engine/runner/validation.py``, the one
    place that decides.
    """
    return any(p.audience == "user_claim" for p in promises)


def _fingerprint(promise) -> str:
    return promise_row_fingerprint(promise.to_row())


def _cost_basis(**overrides) -> dict:
    basis = {
        "pricing_key": PRICING_KEY,
        "unit_rate": "0.02",
        "estimated_quantity": 1200,
        "quantity_unit": "accelerator_second",
        "terms_version": None,
        "quantity_rounding_mode": "half_even",
        "quantity_rounding_decimal_places": 3,
        "meter_key": "accelerator_seconds",
        "meter_units_per_quantity_unit": "1",
        "ceiling_mode": "none",
        "row_settlement_mode": "all_metered",
        "charge_authority": "provider_direct",
        "hardware_class": "baseline",
        "throughput_ref": THROUGHPUT_REF,
    }
    basis.update(overrides)
    return basis


def _live_cost_fact(**overrides) -> dict:
    """The complete run-start projection for :func:`_cost_basis`.

    Settlement terms became admission facts in the commercial-offering seam:
    omitting one now correctly means a different/unknown basis.  Tests aimed
    at rate or hardware behavior therefore start from this complete fact and
    remove only the field whose absence they are exercising.
    """

    basis = _cost_basis()
    fact = {
        "pricing_key": basis["pricing_key"],
        "unit_rate": basis["unit_rate"],
        **{key: basis[key] for key in LIVE_COST_TERM_KEYS},
        "hardware_class": basis["hardware_class"],
    }
    for key, value in overrides.items():
        if value is _ABSENT:
            fact.pop(key, None)
        else:
            fact[key] = value
    return fact


def _transcribe_promises() -> list[Promise]:
    """A hosted-transcription promise set (the O2/O3 shape)."""
    return [
        Promise.make(
            "egress_class",
            "satisfies_order",
            "frisket_shared",
            order_ref=EGRESS_ORDER_REF,
            audience="user_claim",
        ),
        Promise.make(
            "operator",
            "eq",
            "frisket",
            audience="system_promise",
        ),
        Promise.make(
            "cost",
            "le",
            "50",
            basis=_cost_basis(),
            audience="user_claim",
        ),
    ]


def _binding(**over) -> dict:
    binding = {
        "egress_class": "frisket_shared",
        "operator": "frisket",
        "cost": _live_cost_fact(),
    }
    binding.update(over)
    return binding


# ---------------------------------------------------------------------------
# play scene: redeploy-resume — persisted set decodes and evaluates the same
# ---------------------------------------------------------------------------


def test_redeploy_resume_roundtrip():
    ps = PromiseSet.make(_transcribe_promises())
    blob = ps.to_json()  # persisted at enqueue
    revived = PromiseSet.from_json(blob)  # worker process, post-redeploy
    assert revived.promises == ps.promises
    assert revived.set_hash == ps.set_hash
    ev = evaluate_set(revived.promises, _binding())
    assert ev.overall == SATISFIED
    assert all(len(result) == 2 for result in ev.results)


# ---------------------------------------------------------------------------
# play scene: safer-rebind satisfies by order implication
# ---------------------------------------------------------------------------


def test_resume_rebinds_strictly_safer_by_order_implication():
    ps = PromiseSet.make(_transcribe_promises())
    ev = evaluate_set(ps.promises, _binding(egress_class="frisket_dedicated_org"))
    assert ev.results[0][1].status == SATISFIED  # safer class satisfies
    ev2 = evaluate_set(ps.promises, _binding(egress_class="third_party_api"))
    assert ev2.results[0][1].status == VIOLATED  # unordered: never substitutable


# ---------------------------------------------------------------------------
# play scene: slower accelerator via hardware re-projection
# ---------------------------------------------------------------------------


def test_slower_accelerator_reprojects_and_violates_cost_bound():
    cost = _transcribe_promises()[2]
    # baseline -> slow: factors 1 -> 0.4, scale 2.5; bound 50,
    # 0.02*1200*2.5 = 60.
    slow = evaluate(
        cost,
        _binding(cost=_live_cost_fact(hardware_class="slow")),
    )
    assert slow.status == VIOLATED and slow.reason == "bound_exceeded"
    # Faster hardware satisfies: baseline -> fast, scale 0.5, projected 12.
    fast = evaluate(
        cost,
        _binding(cost=_live_cost_fact(hardware_class="fast")),
    )
    assert fast.status == SATISFIED


def test_cost_rate_is_fact_only_never_the_promises_own_basis():
    """E-1: the rate must be OBSERVED, or the predicate is self-referential.

    A candidate that reports no rate used to inherit the promise's own
    consented rate and evaluate satisfied — a bound comparing itself against
    itself. It is now honestly unevaluable, and a raised live rate violates.
    """
    cost = _transcribe_promises()[2]  # bound 50 = 0.02 x 1200 on baseline
    silent = evaluate(
        cost,
        {"cost": _live_cost_fact(unit_rate=_ABSENT)},
    )
    assert silent.status == UNEVALUABLE and silent.reason == "missing_fact"
    raised = evaluate(
        cost,
        {"cost": _live_cost_fact(unit_rate="0.05")},
    )
    assert raised.status == VIOLATED and raised.reason == "bound_exceeded"
    # The QUANTITY still comes from the consented basis (the admission gate
    # owns quantity): a fact that claims a different one cannot move the bound.
    inflated = evaluate(
        cost,
        {"cost": _live_cost_fact(estimated_quantity=10**9)},
    )
    assert inflated.status == SATISFIED


def test_unknown_hardware_class_is_unevaluable():
    cost = _transcribe_promises()[2]
    r = evaluate(
        cost,
        _binding(cost=_live_cost_fact(hardware_class="h999")),
    )
    assert r.status == UNEVALUABLE and r.reason == "unknown_hardware_class"


def test_hardware_invariant_sentinel_ignores_candidate_hardware():
    # throughput_ref null + no hardware_class = hardware-invariant pricing:
    # the candidate's hardware is irrelevant, never unevaluable.
    p = Promise.make(
        "cost",
        "le",
        "50",
        basis=_cost_basis(hardware_class=None, throughput_ref=None),
        audience="user_claim",
    )
    r = evaluate(
        p,
        {"cost": _live_cost_fact(hardware_class="irrelevant")},
    )
    assert r.status == SATISFIED  # 0.02*1200 = 24 <= 50, no re-projection


def test_missing_candidate_hardware_class_is_unevaluable():
    cost = _transcribe_promises()[2]
    r = evaluate(cost, {"cost": _live_cost_fact(hardware_class=_ABSENT)})
    assert r.status == UNEVALUABLE and r.reason == "missing_fact"


def test_unknown_throughput_table_version_is_unevaluable():
    p = Promise.make(
        "cost",
        "le",
        "50",
        basis=_cost_basis(throughput_ref="gpu_throughput.v99"),
    )
    r = evaluate(
        p,
        _binding(cost=_live_cost_fact(hardware_class="slow")),
    )
    assert r.status == UNEVALUABLE and r.reason == "unknown_throughput_table"


# ---------------------------------------------------------------------------
# D2: pricing-key mismatch is recorded as unevaluable
# ---------------------------------------------------------------------------


def test_different_pricing_key_is_unevaluable_not_violated():
    cost = _transcribe_promises()[2]
    r = evaluate(
        cost, {"cost": {"pricing_key": "datalab.ocr.page", "unit_rate": "0.5"}}
    )
    assert r.status == UNEVALUABLE and r.reason == "basis_mismatch"


# ---------------------------------------------------------------------------
# play scene: absent facts are recorded as unevaluable
# ---------------------------------------------------------------------------


def test_absent_fact_is_recorded_as_unevaluable():
    ps = PromiseSet.make(_transcribe_promises())
    ev = evaluate_set(ps.promises, {"operator": "frisket"})  # no egress/cost
    statuses = [result.status for _, result in ev.results]
    assert statuses == [UNEVALUABLE, SATISFIED, UNEVALUABLE]
    assert ev.overall == UNEVALUABLE


# ---------------------------------------------------------------------------
# Coverage envelope.
# ---------------------------------------------------------------------------


def test_envelope_high_water_and_le_coverage():
    s1 = PromiseSet.make(_transcribe_promises())
    rows2 = _transcribe_promises()
    rows2[2] = Promise.make(
        "cost",
        "le",
        "120",
        basis=_cost_basis(),
        audience="user_claim",
    )
    s2 = PromiseSet.make(rows2)
    envelope = compute_coverage_envelope([s1, s2])

    # Order does not matter: the fold keeps the high-water bound either way.
    assert compute_coverage_envelope([s2, s1]) == envelope
    # The envelope is a plain JSON-serializable dict.
    assert json.loads(json.dumps(envelope)) == envelope

    covered = Promise.make(
        "cost",
        "le",
        "80",
        basis=_cost_basis(),
        audience="user_claim",
    )
    uncovered = Promise.make(
        "cost",
        "le",
        "121",
        basis=_cost_basis(),
        audience="user_claim",
    )
    assert claim_covered_by_envelope(envelope, covered)
    assert not claim_covered_by_envelope(envelope, uncovered)


def test_envelope_excludes_categoricals_and_uncomparable_rows():
    rows = _transcribe_promises()
    unknown_bound = Promise.from_row(
        {
            "field": "cost",
            "op": "le",
            "value": "not-a-number",
            "basis": _cost_basis(),
            "order_ref": None,
            "audience": "user_claim",
        }
    )
    envelope = compute_coverage_envelope(
        [PromiseSet.make(rows), PromiseSet(promises=(unknown_bound,))]
    )
    # Only the monotone cost row contributes; egress/operator (categorical,
    # exact-set-hash coverage only) and the uncomparable bound do not.
    assert len(envelope) == 1
    entry = next(iter(envelope.values()))
    assert entry["field"] == "cost" and entry["op"] == "le"
    assert entry["value"] == "50"  # the junk bound never became high-water

    egress = rows[0]
    assert not claim_covered_by_envelope(envelope, egress)


def test_bounds_under_incomparable_bases_never_cover_each_other():
    """Same field+op, different pricing keys: incomparable bases live under
    distinct envelope keys, so a large consented bound under key A never
    covers a claim under key B."""
    a = Promise.make(
        "cost",
        "le",
        "500",
        basis=_cost_basis(),
        audience="user_claim",
    )
    b_basis = _cost_basis(
        pricing_key="datalab.ocr.page",
        quantity_unit="page",
        hardware_class=None,
        throughput_ref=None,
    )
    b = Promise.make("cost", "le", "1", basis=b_basis, audience="user_claim")
    assert basis_identity(a) != basis_identity(b)

    envelope = compute_coverage_envelope([PromiseSet.make([a]), PromiseSet.make([b])])
    assert len(envelope) == 2  # both retained, neither ratcheted the other
    claim_under_b = Promise.make(
        "cost", "le", "30", basis=b_basis, audience="user_claim"
    )
    assert not claim_covered_by_envelope(envelope, claim_under_b)


def test_unbounded_covered_only_by_explicit_unbounded():
    bounded = Promise.make(
        "cost",
        "le",
        "999999",
        basis=_cost_basis(),
        audience="user_claim",
    )
    unbounded = Promise.make("cost", "unbounded", None, audience="user_claim")
    bounded_env = compute_coverage_envelope([PromiseSet.make([bounded])])
    unbounded_env = compute_coverage_envelope([PromiseSet.make([unbounded])])
    assert not claim_covered_by_envelope(bounded_env, unbounded)
    assert claim_covered_by_envelope(unbounded_env, unbounded)


# ---------------------------------------------------------------------------
# unbounded semantics (v3)
# ---------------------------------------------------------------------------


def test_unbounded_is_trivially_satisfied_and_gates_by_presence():
    p = Promise.make("cost", "unbounded", None, audience="user_claim")
    # Satisfied under any binding, even an empty one: it promises nothing.
    assert evaluate(p, {}).status == SATISFIED
    assert evaluate(p, {"cost": {"pricing_key": "whatever"}}).status == SATISFIED
    # Its presence is the claim: the gate must render.
    assert _gates([p])


# ---------------------------------------------------------------------------
# Current decoder: unknown vocabulary/future extras survive current rows
# ---------------------------------------------------------------------------


def test_unknown_op_survives_decode_and_fails_closed():
    # A 2028 writer recorded op="matches_glob"; this reader decodes it.
    row = {
        "field": "region",
        "op": "matches_glob",
        "value": "eu-*",
        "basis": None,
        "order_ref": None,
        "audience": "user_claim",
        "future_column": 7,
    }
    blob = json.dumps({"promises": [row]})
    ps = PromiseSet.from_json(blob)
    assert ps.promises[0].op == "matches_glob"  # preserved, not dropped
    assert json.loads(ps.promises[0].extras_json)["future_column"] == 7
    assert json.loads(ps.to_json())["promises"][0]["future_column"] == 7
    ev = evaluate_set(ps.promises, {"region": "eu-west"})
    result = ev.results[0][1]
    assert result.status == UNEVALUABLE and result.reason == "unknown_op"


def test_unknown_field_survives_decode_and_fails_closed():
    row = {
        "field": "data_residency",
        "op": "eq",
        "value": "eu",
        "basis": None,
        "order_ref": None,
        "audience": "user_claim",
    }
    ps = PromiseSet.from_json(json.dumps({"promises": [row]}))
    assert ps.promises[0].field == "data_residency"
    r = evaluate(ps.promises[0], {"operator": "self"})  # fact never resolved
    assert r.status == UNEVALUABLE and r.reason == "missing_fact"


def test_order_ref_missing_or_unknown_version_is_unevaluable():
    decoded = Promise.from_row(
        {
            "field": "egress_class",
            "op": "satisfies_order",
            "value": "frisket_shared",
            "basis": None,
            "order_ref": None,
            "audience": "user_claim",
        }
    )  # no order_ref recorded
    r = evaluate(decoded, {"egress_class": "none"})
    assert r.status == UNEVALUABLE and r.reason == "missing_order_ref"

    future = Promise.from_row(
        {
            "field": "egress_class",
            "op": "satisfies_order",
            "value": "frisket_shared",
            "basis": None,
            "order_ref": "egress.v99",
            "audience": "user_claim",
        }
    )
    r2 = evaluate(future, {"egress_class": "none"})
    assert r2.status == UNEVALUABLE and r2.reason == "unknown_order_table"


def test_unknown_egress_enum_from_future_is_unevaluable():
    p = _transcribe_promises()[0]
    r = evaluate(p, _binding(egress_class="quantum_mesh"))
    assert r.status == UNEVALUABLE and r.reason == "unknown_element"


# ---------------------------------------------------------------------------
# authoring is strict (make), decoding is not (from_row)
# ---------------------------------------------------------------------------


def test_make_is_strict_about_vocabulary():
    with pytest.raises(ValueError):
        Promise.make("data_residency", "eq", "eu")  # closed field registry
    with pytest.raises(ValueError):
        Promise.make("operator", "le", "5")  # op not allowed for field
    with pytest.raises(ValueError):
        Promise.make("egress_class", "satisfies_order", "none")  # no order_ref
    with pytest.raises(ValueError):
        Promise.make("cost", "le", 50.0, basis=_cost_basis())  # float bound
    with pytest.raises(ValueError):
        Promise.make("cost", "le", "50", basis={"unit_rate": 0.02})  # float in basis
    # ``credential_source`` left the AUTHORING registry; it is
    # a pre-effect USE constraint at the adapter now, not a run-start promise.
    with pytest.raises(ValueError):
        Promise.make("credential_source", "eq", "local")
    assert sorted(PROMISE_FIELDS) == ["cost", "egress_class", "operator", "region"]


def test_current_row_schema_refuses_retired_or_missing_keys():
    current = {
        "field": "operator",
        "op": "eq",
        "value": "self",
        "basis": None,
        "order_ref": None,
        "audience": "system_promise",
    }
    for retired in ("severity", "mode"):
        with pytest.raises(ValueError, match="retired"):
            Promise.from_row({**current, retired: "legacy"})
    with pytest.raises(ValueError, match="retired field"):
        Promise.from_row(
            {
                **current,
                "field": "credential_source",
                "value": "local",
            }
        )
    for required in current:
        incomplete = {key: value for key, value in current.items() if key != required}
        with pytest.raises(ValueError, match="missing required"):
            Promise.from_row(incomplete)


def test_le_type_confusion_is_unevaluable():
    p = Promise.make("cost", "le", "50")
    assert evaluate(p, {"cost": "cheap"}).status == UNEVALUABLE
    assert evaluate(p, {"cost": True}).status == UNEVALUABLE  # bool not quantity
    p2 = Promise.from_row(
        {
            "field": "cost",
            "op": "le",
            "value": "fifty",
            "basis": None,
            "order_ref": None,
            "audience": "system_promise",
        }
    )
    assert evaluate(p2, {"cost": 10}).status == UNEVALUABLE


# ---------------------------------------------------------------------------
# registry: append-only, publish-only
# ---------------------------------------------------------------------------


def test_registry_is_append_only_publish_only():
    reg = builtin_registry()
    with pytest.raises(RegistryError):
        reg.publish("order", EGRESS_ORDER_REF, OrderTable(chain=("x",)))
    reg.publish(
        "throughput",
        THROUGHPUT_REF,
        ThroughputTable(factors=(("baseline", "1"),)),
    )
    with pytest.raises(RegistryError):
        reg.publish(
            "throughput",
            THROUGHPUT_REF,
            ThroughputTable(factors=(("baseline", "1"),)),
        )
    reg.publish("order", "egress.v2", OrderTable(chain=("none",)))  # append ok
    with pytest.raises(RegistryError):
        reg.publish("order", "not a ref", OrderTable(chain=("none",)))
    # There is deliberately no update or delete surface.
    assert not any(
        name in ("update", "delete", "remove", "unpublish")
        for name in dir(reg)
        if not name.startswith("_")
    )


def test_builtin_order_table_pins_its_published_truth():
    """The built-in v1 order truth, asserted in full (also corpus-pinned)."""
    order = REGISTRY.order_table(EGRESS_ORDER_REF)
    assert [
        order.le(a, "frisket_shared")
        for a in (
            "none",
            "operator_lan",
            "frisket_dedicated_org",
            "frisket_shared",
            "third_party_api",
        )
    ] == [True, True, True, True, False]
    assert order.le("third_party_api", "third_party_api") is True
    assert order.le("frisket_shared", "none") is False
    assert order.le("quantum_mesh", "none") is None


# ---------------------------------------------------------------------------
# Canonical hash contract.
# ---------------------------------------------------------------------------


def test_hash_dict_order_invariance():
    basis_a = {
        "pricing_key": PRICING_KEY,
        "unit_rate": "0.02",
        "estimated_quantity": 1200,
        "quantity_unit": "accelerator_second",
        "hardware_class": "baseline",
        "throughput_ref": THROUGHPUT_REF,
    }
    basis_b = dict(reversed(list(basis_a.items())))  # same keys, other order
    a = Promise.make("cost", "le", "50", basis=basis_a, audience="user_claim")
    b = Promise.make("cost", "le", "50", basis=basis_b, audience="user_claim")
    assert _fingerprint(a) == _fingerprint(b)
    assert promise_set_hash([a]) == promise_set_hash([b])


def test_hash_domain_separation():
    body = {"promises": []}
    assert content_hash("frisket.promise_set.v1", body) != content_hash(
        "frisket.action_identity.v1", body
    )


def test_display_fields_are_excluded_from_hashed_material():
    plain = Promise.make(
        "cost",
        "le",
        "50",
        basis=_cost_basis(),
        audience="user_claim",
    )
    row = plain.to_row()
    row["display"] = "About $0.50 of GPU time"  # display-only, unhashed
    decorated = Promise.from_row(row)
    assert json.loads(decorated.extras_json)["display"]  # preserved
    assert _fingerprint(decorated) == _fingerprint(plain)
    assert promise_set_hash([decorated]) == promise_set_hash([plain])


def test_hash_nfc_normalization_and_float_rejection():
    composed = "café"
    decomposed = "café"
    assert canonical_json(composed) == canonical_json(decomposed)
    assert content_hash(DOMAIN_TEST, {"k": composed}) == content_hash(
        DOMAIN_TEST, {"k": decomposed}
    )
    with pytest.raises(ValueError):
        canonical_json({"cost": 0.5})  # no floats in hashed material


# ---------------------------------------------------------------------------
# E-2: the family registry is the ONE place a hash domain becomes legal
# ---------------------------------------------------------------------------


def test_unregistered_hash_domain_refuses():
    """A domain nobody declared cannot mint a family by being hashed under.

    The before the route-bundle cutover fallback (``HASH_FAMILIES.get(domain, HashFamily(domain))``)
    turned a typo into a valid-looking digest under an undeclared, uncorpused
    family — recorded identity from a name that does not exist.
    """
    from frisket.execution.promises import HASH_FAMILIES

    with pytest.raises(ValueError) as exc:
        content_hash("frisket.promise_sett.v1", {"a": 1})  # one-key typo
    assert "unregistered hash-family domain" in str(exc.value)
    assert "frisket.promise_sett.v1" in str(exc.value)
    # ...and every registered domain still hashes.
    for domain in HASH_FAMILIES:
        assert len(content_hash(domain, {"a": 1})) == 64


def test_test_only_hash_family_is_never_used_by_src():
    """``frisket.test.v1`` exists so tests need not borrow a production
    family's identity; production code must never hash under it."""
    src = Path(__file__).resolve().parents[2] / "src" / "frisket"
    offenders = [
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        # rule19: test-only hash family is registered, so runtime cannot notice production use; the grep is the only mechanical fence; moving the family would weaken E-2's one-legal-home property
        if DOMAIN_TEST in path.read_text(encoding="utf-8")
        and path.name != "promises.py"  # its declaration home
    ]
    assert offenders == []


def test_seam_files_never_hash_outside_content_hash():
    """Seam-scoped bare-sha256 ban (E-2, deliberately NOT a codebase sweep).

    Every identity the execution seam records must come from the ONE
    family-parameterized construction, so ``hashlib.sha256`` may appear in
    ``execution/**`` and the route store only inside ``content_hash``'s own
    body. A second hasher next to them is how a family with no declared
    rules, no corpus pin, and no NFC policy gets recorded identity.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "frisket"
    scope = sorted(src.glob("execution/**/*.py")) + [
        src / "engine" / "store" / "execution_routes.py"
    ]
    assert len(scope) > 5  # the glob really matched the seam

    found: list[tuple[str, str]] = []
    for path in scope:
        # rule19: closure fence — bare sha256 pinned to content_hash's own body
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Attribute)
                    and inner.attr == "sha256"
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == "hashlib"
                ):
                    found.append((str(path.relative_to(src)), node.name))
    assert found == [("execution/promises.py", "content_hash")], found


# ---------------------------------------------------------------------------
# totality fuzz
# ---------------------------------------------------------------------------


def test_evaluator_totality_fuzz():
    """evaluate() never raises, and unevaluable is never silently passed."""
    rng = random.Random(0)
    values = [None, 0, 1.5, "x", "50", True, [], {}, {"pricing_key": 3}, float("nan")]
    ops = ["eq", "le", "satisfies_order", "unbounded", "??", ""]
    fields = ["a", "egress_class", "cost", "operator"]
    bases = [
        None,
        {},
        {"pricing_key": "k"},
        {
            "pricing_key": PRICING_KEY,
            "unit_rate": "0.02",
            "hardware_class": "baseline",
            "throughput_ref": THROUGHPUT_REF,
        },
    ]
    refs = [None, EGRESS_ORDER_REF, "egress.v99", "junk"]
    for _ in range(600):
        promise = Promise.from_row(
            {
                "field": rng.choice(fields),
                "op": rng.choice(ops),
                "value": rng.choice(values),
                "basis": rng.choice(bases),
                "order_ref": rng.choice(refs),
                "audience": rng.choice(["user_claim", "system_promise", "whom"]),
            }
        )
        binding = {rng.choice(fields + ["z"]): rng.choice(values)}
        result = evaluate(promise, binding)  # must not raise
        assert result.status in (SATISFIED, VIOLATED, UNEVALUABLE)


# ---------------------------------------------------------------------------
# gate trigger
# ---------------------------------------------------------------------------


def test_gate_trigger_is_any_user_claim():
    local = [Promise.make("egress_class", "eq", "none", audience="system_promise")]
    assert not _gates(local)  # O1 rent bound: no gate for local
    assert _gates(_transcribe_promises())
