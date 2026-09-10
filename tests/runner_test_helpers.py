"""Test clients for the exact quote -> confirmation-echo handshake.

These helpers are deliberately narrow: they do not turn ``confirmed=True``
into ambient authority.  They first ask the real runner/executor for the
current scope-and-quote token, then echo only that token on the retry.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

from frisket.contracts.actions.schemas._base import ActionResult
from frisket.engine.executor import run_action_spec
from frisket.engine.runner import validation
from frisket.engine.runner.map_runner import MapRunner, PreparedRun, RunProgress
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.runner.validation import ClaimsGate
from frisket.ops.base import Recipe


def _retry_spec(spec: dict[str, Any], gate: ClaimsGate) -> dict[str, Any]:
    promise_set_hash = gate.promise_set_hash
    if not isinstance(promise_set_hash, str) or not promise_set_hash:
        raise AssertionError("confirmation challenge did not carry an exact token")
    retry = copy.deepcopy(spec)
    retry["consented_promise_set_hash"] = promise_set_hash
    return retry


def prepare_with_exact_confirmation(
    runner: MapRunner,
    spec: dict[str, Any],
) -> RunProgress:
    """Prepare once unconfirmed, echoing the challenge token if one is issued."""

    try:
        return runner.prepare_run(spec, confirmed=False)
    except ClaimsGate as gate:
        return runner.prepare_run(
            _retry_spec(spec, gate),
            confirmed=True,
        )


async def run_with_exact_confirmation(
    runner: MapRunner,
    spec: dict[str, Any],
    *,
    resume_run_id: int | None = None,
) -> RunProgress:
    """Run through the exact confirmation and output-reservation contracts.

    A direct ``MapRunner`` caller owns the same output lease that the queued
    action boundary would otherwise acquire.  Acquire it before materializing
    columns, bind that exact group to the prepared run, and pass its token to
    dispatch.  This keeps test helpers honest about the production write
    fence instead of granting runners ambient claimless authority.
    """

    recipe = validation.recipe_for_spec(spec)
    output_names = [str(field["name"]) for field in recipe.output_fields(spec)]
    claim_token = f"output-claim:test:{recipe.name}:{uuid.uuid4()}"
    claim_store = OutputColumnClaimStore(runner.project)
    claims, conflict = claim_store.acquire(
        sheet_id=int(spec["sheet_id"]),
        output_names=output_names,
        action_kind=recipe.name,
        claim_token=claim_token,
        lease_seconds=6 * 60 * 60,
    )
    if conflict is not None:
        raise AssertionError(
            "test runner could not reserve its output plan: "
            f"{conflict['output_name']!r} is already claimed"
        )
    if len(claims) != len(output_names):
        raise AssertionError("test runner did not reserve its exact output plan")

    prepared: PreparedRun | None = None
    dispatch_spec = spec
    confirmed = False
    try:
        try:
            prepared = runner._prepare(  # noqa: SLF001 - direct runner contract helper
                spec,
                confirmed=False,
                resume_run_id=resume_run_id,
            )
        except ClaimsGate as gate:
            dispatch_spec = _retry_spec(spec, gate)
            confirmed = True
            prepared = runner._prepare(  # noqa: SLF001 - exact challenge retry
                dispatch_spec,
                confirmed=True,
                resume_run_id=resume_run_id,
            )
        bound = claim_store.bind_to_run(
            claim_token=claim_token,
            run_id=prepared.run_id,
            expected_output_names=output_names,
        )
        if bound != len(output_names):
            raise AssertionError("test runner did not bind its exact output plan")
        progress = await runner.run(
            dispatch_spec,
            confirmed=confirmed,
            resume_run_id=resume_run_id,
            claim_token=claim_token,
            prepared_run=prepared,
        )
        claim_store.release(claim_token=claim_token)
        return progress
    except BaseException:
        # Normal runner finalization releases a bound claim.  This only
        # cleans up prepare/challenge failures before dispatch owns it.
        claim_store.release(claim_token=claim_token, status="failed")
        raise


async def run_with_output_claim(
    runner: MapRunner,
    spec: dict[str, Any],
    *,
    program: Recipe | None = None,
    confirmed: bool = False,
    resume_run_id: int | None = None,
    reopen_operator_cancelled: bool = True,
) -> RunProgress:
    """Run an already-authorized spec with a canonical output reservation."""

    recipe = program or validation.recipe_for_spec(spec)
    output_names = [str(field["name"]) for field in recipe.output_fields(spec)]
    claim_token = f"output-claim:test:{recipe.name}:{uuid.uuid4()}"
    claim_store = OutputColumnClaimStore(runner.project)
    claims, conflict = claim_store.acquire(
        sheet_id=int(spec["sheet_id"]),
        output_names=output_names,
        action_kind=recipe.name,
        claim_token=claim_token,
        lease_seconds=6 * 60 * 60,
    )
    if conflict is not None:
        raise AssertionError(
            "test runner could not reserve its output plan: "
            f"{conflict['output_name']!r} is already claimed"
        )
    if len(claims) != len(output_names):
        raise AssertionError("test runner did not reserve its exact output plan")
    try:
        prepared = runner._prepare(  # noqa: SLF001 - direct runner contract helper
            spec,
            program=program,
            confirmed=confirmed,
            resume_run_id=resume_run_id,
        )
        bound = claim_store.bind_to_run(
            claim_token=claim_token,
            run_id=prepared.run_id,
            expected_output_names=output_names,
        )
        if bound != len(output_names):
            raise AssertionError("test runner did not bind its exact output plan")
        progress = await runner.run(
            spec,
            program=program,
            confirmed=confirmed,
            resume_run_id=resume_run_id,
            reopen_operator_cancelled=reopen_operator_cancelled,
            claim_token=claim_token,
            prepared_run=prepared,
        )
        claim_store.release(claim_token=claim_token)
        return progress
    except BaseException:
        claim_store.release(claim_token=claim_token, status="failed")
        raise


def run_action_with_exact_confirmation(
    project: Any,
    action: dict[str, Any],
    **run_kwargs: Any,
) -> ActionResult:
    """Execute a v1 action through its real challenge-and-echo protocol."""

    challenge_action = copy.deepcopy(action)
    challenge_action.pop("confirmation", None)
    params = challenge_action.setdefault("params", {})
    if "confirmed" in params:
        params["confirmed"] = False
    params.pop("consented_promise_set_hash", None)
    result = run_action_spec(project, challenge_action, **run_kwargs)
    if result.status != "needs_confirmation":
        return result
    if not result.errors:
        raise AssertionError("confirmation challenge did not carry an error detail")
    promise_set_hash = result.errors[0].details.get("promise_set_hash")
    if not isinstance(promise_set_hash, str) or not promise_set_hash:
        raise AssertionError("confirmation challenge did not carry an exact token")
    retry = copy.deepcopy(action)
    if "action_id" in retry:
        retry["confirmation"] = promise_set_hash
    else:
        retry_params = retry.setdefault("params", {})
        retry_params["confirmed"] = True
        retry_params["consented_promise_set_hash"] = promise_set_hash
    return run_action_spec(project, retry, **run_kwargs)
