from __future__ import annotations

import importlib
from importlib.util import find_spec

import pytest

_REPLAY_POLICY_MODULE = "frisket.sdk.replay_policy"
_ALLOWED_REPLAY_POLICIES = {"surface", "preserve", "clobber"}


def _module_present(name: str) -> bool:
    """True iff ``name`` is importable. Swallows the missing-parent-package
    lookup failure from ``find_spec`` so an absent product module reds as a
    plain ``AssertionError`` in the test body, never a collection/import
    error the admission classifier rejects (the tonight-standard probe)."""
    try:
        return find_spec(name) is not None
    except ModuleNotFoundError:
        return False


@pytest.mark.gap
def test_canonical_op_errors_helper_exists() -> None:
    # RED until harmonization: a code->message-template registry so an op lists
    # codes + a few overrides instead of ~11 hand-written ActionErrorSpecs.
    from frisket.sdk.errors import op_errors  # noqa: PLC0415

    specs = op_errors("map.clean_column")
    codes = {spec.code for spec in specs}
    assert {
        "missing_capability",
        "missing_idempotency_key",
        "invalid_input_ref",
        "output_column_exists",
        "idempotency_conflict",
        "stale_replay",
    } <= codes
    # the standard messages are kind-templated, not hand-written per op
    by_code = {spec.code: spec.message for spec in specs}
    assert "map.clean_column" in by_code["missing_capability"]


@pytest.mark.gap
def test_uniform_replay_edit_policy() -> None:
    # RED until the op-agnostic replay-edit policy is decided and implemented:
    # the replay value-check must be
    # uniform ("were the output cells edited since write?"), and there is ONE
    # documented policy for an edited cell (surface / preserve / clobber) -- least
    # surprising = consistent + durable edits. Today ops disagree (summarize + geocode
    # skip value_hash -> drift). Green when the policy module exists, exports the
    # uniform constant, and it is the DECIDED "surface" (preserve the edit AND surface
    # the fresh value). Probed find_spec/hasattr-guarded so the pre-build red is a
    # clean assertion failure in the test body, never a missing-module import
    # failure the admission classifier would reject.
    assert _module_present(_REPLAY_POLICY_MODULE), (
        f"missing {_REPLAY_POLICY_MODULE}: the uniform op-agnostic replay-edit "
        "policy constant (REPLAY_EDIT_POLICY) that every migrated map op honors "
        "has no product module yet"
    )
    module = importlib.import_module(_REPLAY_POLICY_MODULE)
    assert hasattr(module, "REPLAY_EDIT_POLICY"), (
        f"{_REPLAY_POLICY_MODULE} lacks the REPLAY_EDIT_POLICY constant"
    )
    policy = module.REPLAY_EDIT_POLICY
    assert policy in _ALLOWED_REPLAY_POLICIES, (
        f"REPLAY_EDIT_POLICY must be one of {sorted(_ALLOWED_REPLAY_POLICIES)}, "
        f"got {policy!r}"
    )
    assert policy == "surface", (
        "the decided uniform replay-edit policy is 'surface' (preserve the "
        f"human edit AND surface the fresh generated value); got {policy!r}"
    )
