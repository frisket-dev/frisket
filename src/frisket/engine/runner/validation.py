"""Pure validation, estimation, and cache-replay-probe guards for the map
runner: recipe resolution, row-scope
derivation, and the batch/cost/provider-key/network guards used by both
``run()`` (via ``_prepare``) and ``preview()`` — extracted as free functions
so the guards run identically whether or not the caller persists anything."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any


from frisket.ai.llm import (
    LLMRequest,
    ModelRouter,
    estimate_tokens,
    model_call_cannot_go_live,
    model_pricing,
    request_key,
)
from frisket.ops.base import Recipe
from frisket.ops.builtin import get_recipe
from frisket.engine.runner.confirmation_context import (
    MoneyConfirmationContext,
    RunnerScope,
    apply_rating,
    mint_confirmation_hash,
    quoted_usd,
    rate_estimate,
    recipe_confirmation,
)
from frisket.execution.pricing_policy import PersistedRating, PricingPolicy
from frisket.execution.promises import Promise, PromiseSet
from frisket.execution.provider import ExecutionComposition
from frisket.execution.consent_coverage import (
    ConsentCoverage,
    effective_consent_coverage,
)
from frisket.execution.claim_labels import cost_posture_label, trust_label

# Compatibility re-export for downstream users of this module.
from frisket.engine.runner.confirmation_context import (  # noqa: F401
    KNOWN_DISPLAY_KEYS,
    ConsentQuote,
    ConsentQuoteRefused,
)
from frisket.engine.runner.confirmation_echo import refuse_unless_exact_echo
from frisket.engine.runner.network_policy import remote_capability_for_spec
from frisket.engine.runner.row_inputs import (
    is_empty_cell_value,
    row_source_values_empty,
    row_values,
)
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import (
    CONSENT_BASIS_CONFIRMED,
    CONSENT_BASIS_EXACT_MATCH,
    CONSENT_BASIS_PREAPPROVED,
)
from frisket.engine.store.runs import RunResultStore
from frisket.execution.resolve_for_action import (
    consented_set_hash,
    exact_match_consent_covers,
    gate_claims_payload,
    project_uncovered_user_claims,
    resolve_for_action,
)
from frisket.execution.resolver import Persistence, Refusal, ResolvedExecution


def _cost_gate_usd() -> float:
    """Return the live threshold used to mint consent and gate legacy runs."""
    from frisket.engine.store.execution_routes import standing_cost_threshold_env

    return float(standing_cost_threshold_env())


# Compatibility constant; live checks use ``_cost_gate_usd``.
COST_GATE_USD = 2.0


class CostGate(Exception):
    def __init__(
        self,
        estimate: float | None,
        *,
        remote_claim: bool = False,
        gate_usd: float | None = None,
        estimate_details: dict[str, Any] | None = None,
    ):
        # Report the threshold that the caller actually compared.
        threshold = _cost_gate_usd() if gate_usd is None else gate_usd
        if estimate is None:
            message = (
                "Estimated cost is unknown (this run has no published "
                "price) — confirm to run it anyway."
            )
        elif remote_claim and estimate <= threshold:
            # Remote spend requires consent even when it rounds to $0.00.
            message = (
                f"this run calls a remote provider (estimated "
                f"${estimate:.2f}); remote spend requires confirmation "
                "regardless of the estimated amount — confirm to run it "
                "anyway"
            )
        else:
            message = (
                f"estimated cost ${estimate:.2f} exceeds the "
                f"${threshold:.2f} gate — confirm to run it anyway"
            )
        super().__init__(message)
        self.estimate = estimate
        self.estimate_details = (
            dict(estimate_details) if estimate_details is not None else None
        )
        self.promise_set_hash: str | None = None


class ClaimsGate(CostGate):
    """A cost-compatible gate carrying uncovered claims and their set hash."""

    def __init__(
        self,
        estimate: float | None,
        *,
        claims: list[dict[str, str]] | tuple[dict[str, str], ...] = (),
        promise_set_hash: str | None = None,
        remote_claim: bool = False,
        gate_usd: float | None = None,
        estimate_details: dict[str, Any] | None = None,
    ):
        super().__init__(
            estimate,
            remote_claim=remote_claim,
            gate_usd=gate_usd,
            estimate_details=estimate_details,
        )
        self.claims = [dict(claim) for claim in claims]
        self.promise_set_hash = promise_set_hash
        if self.claims:
            summary = " ".join(claim["display"] for claim in self.claims)
            self.args = (
                f"this run needs confirmation: {summary} Confirm to run it anyway.",
            )


def confirmation_context(
    project: Project,
    recipe: Recipe,
    spec: dict[str, Any],
    row_ids: list[int],
    estimate: dict[str, Any],
) -> MoneyConfirmationContext:
    """Bind a 402 approval to action identity, exact scope, and full quote.

    The recipe lane's scope binding, and nothing else: the payload shape,
    the money projection, and the hash itself belong to
    ``engine/runner/confirmation_context.py``, which every other family
    mints through too.

    Returns the ENVELOPE rather than the digest because two consumers need
    two halves of one object and must not build it twice: the gate hashes it
    (:func:`confirmation_context_hash`), and the writer persists its ``quote``
    onto the consent row so dispatch can read the rated figure back. A second
    construction of the quote would be a second answer to "what did the user
    approve".
    """

    from frisket.execution.scope_identity import canonical_work_scope_binding

    runner_spec = {
        key: value
        for key, value in spec.items()
        if key
        not in {
            "confirmed",
            "consented_promise_set_hash",
            "_frisket_queued_action_run",
        }
    }
    return recipe_confirmation(
        # The canonical public action kind is the family. The private Recipe
        # object is an implementation selected only after that kind passes
        # strict runner-spec admission.
        family_kind=str(spec["action_kind"]),
        scope=RunnerScope(
            runner_spec=runner_spec,
            row_ids=[int(row_id) for row_id in row_ids],
            work_scope=canonical_work_scope_binding(
                project,
                recipe,
                spec,
                row_ids,
            ),
        ),
        estimate=estimate,
    )


def confirmation_context_hash(
    project: Project,
    recipe: Recipe,
    spec: dict[str, Any],
    row_ids: list[int],
    estimate: dict[str, Any],
) -> str:
    """The digest of :func:`confirmation_context` — THE recipe lane's 402
    hash, and the value dispatch compares against a persisted consent."""

    return mint_confirmation_hash(
        confirmation_context(project, recipe, spec, row_ids, estimate)
    )


def _queued_resume_consent_covers(
    project: Project,
    recipe: Recipe,
    spec: Mapping[str, Any],
    *,
    run_id: int,
    principal: str,
    context_hash: str,
) -> bool:
    """Whether this queued run's admission consent covers the live quote."""

    marker = spec.get("_frisket_queued_action_run")
    if not isinstance(marker, Mapping) or marker.get("schema_version") != (
        "frisket.internal.queued_action_run.v1"
    ):
        return False
    from frisket.engine.store.execution_routes import RouteStore
    from frisket.execution.attempt_authority import attempt_identity

    identity = attempt_identity(recipe, spec)
    return any(
        consent.actor == principal
        and consent.action_identity_hash == identity
        and consent.promise_set_hash == context_hash
        for consent in RouteStore.for_run(project, run_id).consents()
    )


def _confirmation_facts(
    recipe: Recipe,
    spec: dict[str, Any],
    estimate: dict[str, Any],
) -> dict[str, Any]:
    """Combine the rated estimate with the live server-derived egress verdict.

    Quote time supplies a fresh rating; dispatch supplies the persisted rating.
    A known-zero external capability is exempt only when the billed quote is zero.
    """

    remote_capability = remote_capability_for_spec(
        recipe,
        spec,
    )
    if remote_capability is None:
        return dict(estimate)
    return {
        **estimate,
        "requires_confirmation": not (
            getattr(recipe, "external_cost_is_known_zero", False)
            and quoted_usd(estimate) == 0
        ),
        "remote_capability": remote_capability,
    }


def confirmation_estimate(
    recipe: Recipe,
    spec: dict[str, Any],
    estimate: dict[str, Any],
    *,
    policy: PricingPolicy,
) -> dict[str, Any]:
    """The exact estimate shape hashed by validation — neutral facts, RATED.

    ``policy`` is required and keyword-only. It is the deployment's pricing
    policy, and this is the one place in the recipe lane that asks it: the
    402's message, the estimate payload, and the consent hash all read the
    single answer it returns. A caller that has no policy of its own is not
    entitled to a default here — it takes :func:`default_pricing_policy`
    explicitly at its composition root, where a deployment can see and replace
    it.

    The DISPATCH side deliberately cannot reach this function's rating: see
    :func:`consented_confirmation_estimate`.
    """

    return _confirmation_facts(recipe, spec, rate_estimate(estimate, policy=policy))


def consented_confirmation_estimate(
    recipe: Recipe,
    spec: dict[str, Any],
    estimate: dict[str, Any],
    *,
    rating: PersistedRating,
) -> dict[str, Any]:
    """The same envelope, reconstructed at DISPATCH from a persisted consent.

    The split this function exists for, stated once:

    * **Recomputed live** — every neutral fact: the recipe's ``estimate()``
      output (cost, rows, cost_source, pricing_key, engine), the egress
      verdict, the row scope, and the canonical work-scope binding. That is
      dispatch's actual job, and it is what catches a provider price change,
      an edited source cell, or a widened row set between consent and run.
    * **Read from the consent** — the RATED projection only (``billed_cost``,
      ``policy_id``), because the policy that produced it is a deployment's
      and is deliberately not wired into dispatch. Rating at dispatch would
      mean a tariff change silently re-pricing an already-approved run.

    Reading the rated half is not a hole in the verification: the figure is
    the one the user was SHOWN, and the neutral facts it was derived from are
    all re-checked beside it. A persisted quote that is missing or malformed
    yields no rating at all, and the caller refuses ``consent_missing``.
    """

    return _confirmation_facts(recipe, spec, apply_rating(estimate, rating))


class ExecutionResolutionRefused(ValueError):
    """A resolver ``Refusal`` (no_capable_target / no_live_target / ...)
    mapped to the spec-validation error family: the message IS the remedy
    text, so both the queued path's ``prepare_run_error`` and direct callers
    surface an actionable 400 instead of burning rows."""

    def __init__(self, refusal: Refusal):
        super().__init__(refusal.remedy)
        self.refusal = refusal
        self.family = refusal.family


class NetworkDisabled(Exception):
    """Raised when the project's effective network policy is ``off`` and the
    spec resolves a remote capability. A safety
    default, not a guarantee: enforcement is app-level; plugin subprocesses,
    notification delivery and scheduled polling are not covered here. The
    classification is entirely server-derived (runner/network_policy.py) —
    never the client-supplied ``capabilities`` field."""

    def __init__(self, capability: str):
        super().__init__(
            f"this project's network setting is off; it blocks {capability}. "
            "Turn network on in the project settings to run this."
        )
        self.capability = capability


class ProviderKeyRefusal(Exception):
    """Base for pre-flight refusals about the provider KEY a run would spend
    through.

    Exists so the refusal-to-ActionError handlers catch THIS, never a leaf
    class. Adding a sibling refusal then reaches the user through every
    existing path with no call-site enumeration to get wrong — the house rule
    is to make enumeration unnecessary rather than to ask someone to promise
    they enumerated correctly. ``error_code`` / ``field`` / ``details`` are
    the whole payload a handler needs, so the handlers hold no per-refusal
    knowledge at all.
    """

    error_code: str = "provider_key_refused"
    field: str = "params.model"

    def __init__(
        self, message: str, *, provider: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.details: dict[str, Any] = {"provider": provider, **(details or {})}

    def action_message(self) -> str:
        """The user-facing sentence. Overridden where a shared remediation
        copy module already owns the wording."""
        return str(self)


class MissingProviderKey(ProviderKeyRefusal):
    """Raised by ``_prepare`` when ``spec["model"]`` names a provider
    with no configured adapter — the run would otherwise reach every row's
    ``_execute_row``, fail identically MAX_ROWS times, and only then surface a
    raw ``LLMError``. A typed, named error at run-confirm time (before any row
    work starts) lets the server turn this into a clear "add a key in
    Settings" ActionError instead of a silent per-row failed run.

    Not raised when ``model_call_cannot_go_live(router)``: strict replay WITH
    a cache attached never calls a live adapter (cache hit or a raised
    ``CacheMiss``), so a keyless router replaying a committed golden cache
    (tests/test_golden.py) is not a misconfiguration. The
    mode alone is NOT that proof — a cacheless ``replay_strict`` router skips
    both replay branches and does reach the adapter, so it needs a key."""

    error_code = "missing_provider_key"

    def __init__(self, provider: str):
        super().__init__(
            f"no API key configured for provider '{provider}'",
            provider=provider,
            details={"retryable": True, "resumable": True},
        )

    def action_message(self) -> str:
        from frisket.ai.llm.remediation import missing_provider_key_message

        return missing_provider_key_message(self.provider)


class ProviderSpendCapExceeded(ProviderKeyRefusal):
    """Raised when the project provider key this run would spend through has
    already accrued at or past its spend cap.

    The cap is a BOUND the journalist consented to, so the honest moment to
    act on it is launch: starting a run you are already over the cap for is
    the one case where refusing is both cheap and unambiguous. Deliberately
    NOT a mid-run halt or a reconsent flow: this is the user's external
    provider-key budget, so a run that crosses it while executing simply
    finishes and the provider's actual bill appears in the accrued total.
    Frisket's separate platform-retail 402 is a customer-charge ceiling at
    settlement; it does not pretend to cap a provider's direct invoice.

    ``spent_micro`` is the sum of calls we could price; ``unmetered_calls``
    is how many live calls on this key had no published price. The message
    says so rather than presenting a lower bound as if it were the total.
    """

    error_code = "provider_spend_cap_exceeded"

    def __init__(
        self,
        *,
        provider: str,
        cap_micro: int,
        spent_micro: int,
        unmetered_calls: int,
    ) -> None:
        cap_usd = cap_micro / 1_000_000
        spent_usd = spent_micro / 1_000_000
        detail = (
            f"the {provider} key in this project has spent ${spent_usd:,.2f} "
            f"against its ${cap_usd:,.2f} spend cap"
        )
        if unmetered_calls:
            detail += (
                f" (plus {unmetered_calls} call"
                f"{'s' if unmetered_calls != 1 else ''} with no published "
                "price, so the real figure is higher)"
            )
        super().__init__(
            detail + ". Raise or clear the spend cap for this provider in "
            "Settings → Provider keys to run this, or save a new key for this "
            "provider to start a fresh spend period.",
            provider=provider,
            details={
                "cap_usd": cap_usd,
                "spent_usd": spent_usd,
                "unmetered_calls": unmetered_calls,
                # The knob, named: fail closed and say which one.
                "setting": "spend_cap_usd",
                "retryable": False,
                "resumable": False,
            },
        )
        self.cap_micro = cap_micro
        self.spent_micro = spent_micro
        self.unmetered_calls = unmetered_calls


class ProviderSpendCapUnenforceable(ProviderKeyRefusal):
    """Raised when the project provider key this run would spend through has
    a cap, and calls on it that no price could be found for.

    Why this exists rather than a conservative floor. The unpriceable call
    is real spend of UNKNOWN SIZE; the ``CostBasis`` vocabulary already has
    the honest word for that (``Unpriceable``, distinct from
    ``PricedCostBasis`` and from ``OperatorBorneZeroCost``), and the house
    rule for it is "an honest cannot-say, never a fabricated total" — no
    fallback price exists anywhere in the product on purpose
    (llm/pricing.py). Inventing a floor here would be the one thing the
    vocabulary forbids, and folding the call in at 0 is the
    ``cost_actual REAL NOT NULL DEFAULT 0`` defect verbatim. So the refusal
    is about the CAP, not about the amount: the bound cannot be enforced, so
    it is not enforced silently.

    A billed vision-OCR call (257 tokens in, 35
    out, gpt-4.1-mini — no entry in the price table) recorded
    ``provider_cost_usd`` NULL, and the key kept reporting the same accrued
    total it had before the money was spent.
    """

    error_code = "provider_spend_cap_unenforceable"

    def __init__(self, *, provider: str, cap_micro: int, unmetered_calls: int) -> None:
        cap_usd = cap_micro / 1_000_000
        plural = "s" if unmetered_calls != 1 else ""
        super().__init__(
            f"the {provider} key in this project has made {unmetered_calls} "
            f"call{plural} with no published price, so its spend against the "
            f"${cap_usd:,.2f} cap cannot be determined and the cap cannot be "
            "enforced. Clear the spend cap for this provider in Settings → "
            "Provider keys to run anyway, or save a new key for this provider "
            "to start a fresh spend period.",
            provider=provider,
            details={
                "cap_usd": cap_usd,
                "unmetered_calls": unmetered_calls,
                # The knob, named: fail closed and say which one.
                "setting": "spend_cap_usd",
                "retryable": False,
                "resumable": False,
            },
        )
        self.cap_micro = cap_micro
        self.unmetered_calls = unmetered_calls


def assert_provider_spend_cap(project: Project, provider: str) -> None:
    """Refuse a provider whose project-owned key can no longer spend.

    Credential caps apply to the provider effect, not to the recipe taxonomy:
    remote media and embedding operations are non-LLM recipes but charge the
    same project key and must pass the same pre-egress check.
    """

    spend = project.provider_spend_state(provider)
    if spend is not None and spend.over_cap:
        assert spend.cap_micro is not None
        raise ProviderSpendCapExceeded(
            provider=provider,
            cap_micro=spend.cap_micro,
            spent_micro=spend.spent_micro,
            unmetered_calls=spend.unmetered_calls,
        )
    if spend is not None and not spend.cap_enforceable:
        assert spend.cap_micro is not None
        raise ProviderSpendCapUnenforceable(
            provider=provider,
            cap_micro=spend.cap_micro,
            unmetered_calls=spend.unmetered_calls,
        )


class BatchRowLimitExceeded(ValueError):
    def __init__(self, *, row_count: int, max_rows: int) -> None:
        super().__init__(f"batch recipe row count {row_count} exceeds cap {max_rows}")
        self.row_count = row_count
        self.max_rows = max_rows


class EmptyInputColumns(ValueError):
    """Raised by ``_validate_spec`` when a fresh (non-resume)
    launch's resolved source column(s) hold zero non-empty values across
    EVERY target row — the run would otherwise queue N rows that each fail
    identically with no persisted reason. The live incident: an undo emptied
    a media column AFTER the action's own resolve step validated it (a
    time-of-check/time-of-use gap — resolve runs at request time, the queued
    worker runs later); an 11-row queued transcribe then burned every row
    failing one at a time, and the receipt only ever said "failed for every
    target row".

    Genre-wide by construction: this lives in ``_validate_spec``, the ONE
    seam every recipe kind (LLM and non-LLM, direct and queued) passes
    through before any row work or `runs` row is created — not a per-kind
    check like ``media.*``'s blob resolver.

    Deliberately NOT raised when only SOME rows are empty (that is normal,
    legitimate per-row emptiness the recipe already handles) — only when
    literally nothing resolves anywhere is a full refusal warranted."""

    def __init__(self, columns: list[str]):
        self.columns = list(columns)
        cols = ", ".join(f"'{c}'" for c in self.columns)
        super().__init__(
            f"{cols} has no values among the target rows — it may have been "
            "undone or not yet populated. Run the step that fills it, then "
            "try again."
        )


@dataclass(frozen=True)
class ResumeClaimConfirmation:
    """A resume whose claims changed and whose user confirmed them.

    ``validate_spec`` cannot write — it is the pure guard half — so the
    backfill-confirm branch records WHAT to bind and the writer
    (:func:`persist_resolved_execution`) binds it. The two head ids are the
    head this invocation actually GATED against, carried rather than re-read:
    the successor append's CAS on them is what makes a concurrent confirm
    refuse instead of forking the chain.
    """

    resolved: ResolvedExecution
    head_route_id: str
    head_promise_set_id: str


@dataclass
class _ValidatedSpec:
    """Validated, write-free preparation state shared by runs and previews."""

    recipe: Recipe
    sheet_id: int
    columns: list[dict[str, Any]]
    col_map: dict[str, int]
    row_ids: list[int]
    output_fields: list[dict[str, Any]]
    est: dict[str, Any] | None
    has_stored_run_scope: bool
    stored_run_scope_count: int | None
    explicit_resume_scope: list[int] | None
    consent_coverage: ConsentCoverage | None = None
    resolved_execution: ResolvedExecution | None = None
    consent_required: bool = False
    consent_basis: str = CONSENT_BASIS_CONFIRMED
    # Legacy actions persist the exact hash and quote shown by their 402 gate.
    unrouted_confirmation_hash: str | None = None
    unrouted_confirmation_quote: dict[str, Any] | None = None
    # A resume confirmation advances an existing chain, never a fresh resolution.
    resume_confirmation: ResumeClaimConfirmation | None = None


def _bind_rated_quote(
    resolved: ResolvedExecution,
    estimate: Mapping[str, Any],
) -> ResolvedExecution:
    """Bind the canonical rated quote into routed consent identity.

    The quote must change the promise-set hash when tariffs change. Prefer a
    non-cost claim so settlement's provider cost basis remains untouched.
    """
    quote = ConsentQuote.from_estimate(estimate)
    quote_json = json.dumps(
        quote.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    promises = list(resolved.promise_set.promises)
    target_index = next(
        (
            index
            for index, promise in enumerate(promises)
            if promise.audience == "user_claim" and promise.field != "cost"
        ),
        None,
    )
    if target_index is None:
        target_index = next(
            (
                index
                for index, promise in enumerate(promises)
                if promise.audience == "user_claim" and promise.field == "cost"
            ),
            None,
        )
    if target_index is None:
        return resolved
    original = promises[target_index]
    basis = dict(original.basis or {})
    basis["consent_quote_json"] = quote_json
    promises[target_index] = Promise.make(
        original.field,
        original.op,
        original.value,
        basis=basis,
        order_ref=original.order_ref,
        audience=original.audience,
    )
    return replace(resolved, promise_set=PromiseSet.make(promises))


def recipe_for_spec(spec: dict) -> Recipe:
    if "recipe" in spec:
        raise ValueError("runner spec must not carry top-level 'recipe' identity")
    raw_kind = spec.get("action_kind")
    if not isinstance(raw_kind, str) or not raw_kind:
        raise ValueError("runner spec requires a canonical action_kind")
    if raw_kind != raw_kind.strip() or "." not in raw_kind:
        raise ValueError(f"runner spec action_kind {raw_kind!r} is not canonical")
    action_kind = raw_kind

    return get_recipe(action_kind)


class InvalidTargetRows(ValueError):
    def __init__(self, missing: list[int]) -> None:
        self.missing = missing
        super().__init__("row_ids must belong to the target sheet")


class InvalidTargetSheet(ValueError):
    def __init__(self, sheet_id: int) -> None:
        self.sheet_id = sheet_id
        super().__init__("sheet_id must identify a visible sheet")


class OutputColumnExists(ValueError):
    def __init__(self, columns: list[str]) -> None:
        self.columns = sorted(columns)
        super().__init__("map.ner output would overwrite an existing column")


def target_rows(project: Project, spec: dict) -> list[int]:
    sheet_id = spec["sheet_id"]
    sheet = project.db.execute(
        "SELECT 1 FROM sheets WHERE id=? AND hidden=0",
        (sheet_id,),
    ).fetchone()
    if sheet is None:
        raise InvalidTargetSheet(sheet_id)
    if "row_ids" in spec and spec.get("row_ids") is not None:
        row_ids: list[int] = []
        for raw_id in spec["row_ids"]:
            try:
                row_ids.append(int(raw_id))
            except (TypeError, ValueError):
                continue
        visible = project.visible_row_ids(sheet_id, row_ids)
        missing = sorted(set(row_ids) - set(visible))
        if missing:
            raise InvalidTargetRows(missing)
        return visible
    return project.visible_row_ids(sheet_id)


_is_empty_cell_value = is_empty_cell_value


def source_values_all_empty(
    project: Project,
    sheet_id: int,
    col_map: dict[str, int],
    source_names: list[str],
    row_ids: list[int],
) -> bool:
    """True only when EVERY named source column's value is empty for
    EVERY target row (EmptyInputColumns' all-empty gate — partial
    emptiness returns False as soon as one non-empty value turns up)."""
    for name in source_names:
        column_id = col_map.get(name)
        if column_id is None:
            continue
        for start in range(0, len(row_ids), 500):
            values = project.get_values(
                sheet_id,
                column_id,
                row_ids=row_ids[start : start + 500],
            )
            if any(not _is_empty_cell_value(v) for v in values.values()):
                return False
    return True


def assert_network_policy(project: Project, recipe: Recipe, spec: dict) -> None:
    """Raise NetworkDisabled when the spec
    resolves a remote capability and the project's effective policy is
    ``off``. Called from ``_validate_spec`` (fresh runs, resumes,
    previews) and re-asserted per row in ``_execute_row``, which is a
    public entry point any caller can reach without going through
    ``_validate_spec`` — the re-assert is what keeps a remote engine from
    egressing on such a path after the project flips ``off``. The policy
    is read at validate/dispatch time and never joins the idempotency
    hash or run identity."""
    capability = remote_capability_for_spec(recipe, spec)
    if capability is None:
        return
    if project.effective_network_policy() == "off":
        raise NetworkDisabled(capability)


def unestimated_cost(recipe: Recipe, spec: dict) -> dict[str, Any]:
    """What a recipe that produced NO estimate costs — the one site that used
    to answer ``{"cost": 0.0}`` for every recipe alike.

    That answer made the gate below unreachable for anything unpriced: an
    unestimated recipe priced at $0.00, which is under every threshold, so the
    ``est["cost"] is None`` branch never fired and a run that spends real money
    launched without a word. Absence and zero shared one representation.

    They no longer do. The recipe's own ``cost_class_for(spec)`` declaration
    says what absence means, and only a recipe that DECLARED itself free gets a
    number here (``cost_source: "free_local"`` — the same vocabulary
    to_markdown/ocr already stamp when they price a local engine). Metered and
    unpriceable both estimate ``cost: None``: the gate treats that exactly like
    an over-budget estimate, and ``requires_confirmation`` rides along so a
    client rendering the estimate shows UNKNOWN rather than $0.00.

    ``cost_class`` is an undefaulted ClassVar, so a recipe that never declared
    raises AttributeError here rather than quietly pricing itself free."""
    if recipe.cost_class_for(spec) == "free":
        return {"cost": 0.0, "cost_source": "free_local"}
    return {"cost": None, "cost_source": "unknown", "requires_confirmation": True}


def _resolved_presentation_labels(resolved: ResolvedExecution) -> dict[str, str]:
    """Display-only labels for one selected execution venue.

    Downstream composition copy wins exactly when supplied. Base can describe
    provider-direct and BYOK work from neutral route facts, but a
    ``platform_metered`` posture is not itself a commercial offering and
    therefore never creates commercial-looking labels.
    """
    presentation = resolved.presentation
    if presentation is not None:
        return {
            "billing_label": presentation.billing_label,
            "venue_label": presentation.venue_label,
        }
    facts = resolved.resolution.facts
    if facts.cost_posture not in {"operator_borne", "org_key"}:
        return {}
    return {
        "billing_label": cost_posture_label(facts.cost_posture),
        "venue_label": trust_label(
            facts.egress_class,
            facts.operator,
            facts.cost_posture,
        ),
    }


@dataclass(frozen=True)
class _EstimateRunResult:
    """One rating split into public presentation and bound consent identity."""

    presentation: dict[str, Any]
    bound_resolution: ResolvedExecution | None


def estimate_run(
    project: Project,
    spec: dict,
    *,
    program: Recipe | None = None,
    composition: ExecutionComposition | None = None,
    pricing_policy: PricingPolicy | None = None,
    resolution: ResolvedExecution | Refusal | None = None,
    resume_run_id: int | None = None,
    scope_row_ids: list[int] | None = None,
    consent_coverage: ConsentCoverage | None = None,
) -> dict:
    """Pre-run estimate for the cost gate. Token math is crude but stable.

    ``validate_spec`` passes its single per-invocation resolution here
    so a resolution-aware recipe (marker ``consumes_resolution = True``)
    estimates from the resolution's basis instead of re-reading env; called
    standalone (the /estimate endpoint, workbench facades) with no
    resolution, such a recipe resolves once here, EPHEMERALLY — estimates
    never persist routes. The returned dict for a resolution-aware recipe
    additionally carries ``claims`` + ``promise_set_hash`` when uncovered
    user claims exist (the RunEstimate additive fields, §5.2).

    ``resume_run_id`` names the run a resume extends, so claim coverage
    can read THAT run's consented high-water envelope — the same artifact the
    worker's effect-site fence reads. Without it a backfill re-gates on the
    categorical claim its own run already consented to, which is exactly what
    kept the resume gate's cost half unreachable.
    """
    return _estimate_run(
        project,
        spec,
        program=program,
        composition=composition,
        pricing_policy=pricing_policy,
        resolution=resolution,
        resume_run_id=resume_run_id,
        scope_row_ids=scope_row_ids,
        consent_coverage=consent_coverage,
    ).presentation


def _estimate_run(
    project: Project,
    spec: dict,
    *,
    program: Recipe | None = None,
    composition: ExecutionComposition | None = None,
    pricing_policy: PricingPolicy | None = None,
    resolution: ResolvedExecution | Refusal | None = None,
    resume_run_id: int | None = None,
    scope_row_ids: list[int] | None = None,
    consent_coverage: ConsentCoverage | None = None,
) -> _EstimateRunResult:
    recipe = program if program is not None else recipe_for_spec(spec)
    rows = (
        target_rows(project, spec)
        if scope_row_ids is None
        else [int(row_id) for row_id in scope_row_ids]
    )
    if not recipe.is_llm(spec):
        if resolution is None and recipe.consumes_resolution:
            if composition is None:
                raise TypeError(
                    "estimate_run requires a request-scoped composition for "
                    "resolution-aware work"
                )
            resolution = resolve_for_action(
                project,
                spec,
                recipe,
                composition=composition,
                persistence="ephemeral",
                scope_row_ids=rows,
            )
        if isinstance(resolution, Refusal):
            # An unavailable route has no executable quote. Do not discard its
            # reason and ask the recipe to invent a second, unrouted estimate.
            raise ExecutionResolutionRefused(resolution)
        col_map = {c["name"]: c["id"] for c in project.columns(spec["sheet_id"])}
        values = [
            row_values(project, recipe, spec, col_map, row_id, for_model=False)
            for row_id in rows
        ]
        if resolution is not None and recipe.consumes_resolution:
            est = recipe.estimate(project, spec, values, resolution=resolution)
        else:
            est = recipe.estimate(project, spec, values)
        out = {
            "rows": len(rows),
            **(est if est is not None else unestimated_cost(recipe, spec)),
        }
        if isinstance(resolution, ResolvedExecution):
            out.update(_resolved_presentation_labels(resolution))
            if pricing_policy is None:
                raise TypeError(
                    "estimate_run requires a pricing_policy for routed work"
                )
            # Rate once before coverage and hash binding.
            rated = confirmation_estimate(
                recipe,
                spec,
                out,
                policy=pricing_policy,
            )
            bound_resolution = _bind_rated_quote(resolution, rated)
            uncovered = project_uncovered_user_claims(
                project,
                bound_resolution.promise_set,
                spec=spec,
                run_id=resume_run_id,
                consent_coverage=consent_coverage,
            )
            out = {**rated, "requires_confirmation": bool(uncovered)}
            if uncovered:
                out["claims"] = gate_claims_payload(
                    uncovered,
                    bound_resolution.resolution.facts,
                    bound_resolution.presentation,
                    has_cost_claim=any(
                        promise.field == "cost"
                        for promise in bound_resolution.promise_set.promises
                    ),
                )
                out["promise_set_hash"] = consented_set_hash(
                    bound_resolution.promise_set
                )
            return _EstimateRunResult(out, bound_resolution)
        return _EstimateRunResult(out, None)
    sheet_id = spec["sheet_id"]
    columns = project.columns(sheet_id)
    col_map = {c["name"]: c["id"] for c in columns}
    sample = rows[: min(20, len(rows))]
    sample_tokens = []
    for row_id in sample:
        values = row_values(project, recipe, spec, col_map, row_id)
        call = recipe.render(values, spec)
        sample_tokens.append(estimate_tokens(json.dumps(call.messages)))
    avg_in = sum(sample_tokens) / max(1, len(sample_tokens))
    est_out = 150 + 50 * len(spec.get("fields", []) or [1])
    pricing = model_pricing(spec.get("model", ""))
    if pricing.price is None:
        out = {
            "cost": None,
            "cost_source": "unknown",
            "rows": len(rows),
            "avg_input_tokens": int(avg_in),
        }
        return _EstimateRunResult(
            out,
            _bind_rated_quote(resolution, out)
            if isinstance(resolution, ResolvedExecution)
            else None,
        )
    pin, pout = pricing.price
    cost = len(rows) * (avg_in * pin + est_out * pout) / 1e6
    out = {
        "cost": round(cost, 4),
        "cost_source": pricing.cost_source,
        **(
            {"pricing_key": pricing.pricing_key}
            if pricing.pricing_key is not None
            else {}
        ),
        "rows": len(rows),
        "avg_input_tokens": int(avg_in),
    }
    return _EstimateRunResult(
        out,
        _bind_rated_quote(resolution, out)
        if isinstance(resolution, ResolvedExecution)
        else None,
    )


def exact_replay_available(
    project: Project,
    router: ModelRouter,
    spec: dict[str, Any],
    *,
    program: Recipe | None = None,
) -> bool:
    """Prove every selected LLM row has its exact canonical cache key.

    This is a pure admission probe: it uses the execution renderer, row
    scope, source-value loader, request shape, recipe version, and cache
    key builder without creating an op, run, receipt, claim, or queue row.
    Unknown/non-map shapes fail closed.
    """
    cache = router.cache
    if cache is None or router.cache_mode not in {"replay", "replay_strict"}:
        return False
    try:
        recipe = program or recipe_for_spec(spec)
        if not recipe.is_llm(spec):
            return False
        row_ids = target_rows(project, spec)
        columns = project.columns(int(spec["sheet_id"]))
        col_map = {str(column["name"]): int(column["id"]) for column in columns}
        column_types = {str(column["name"]): str(column["type"]) for column in columns}
        for row_id in row_ids:
            values = row_values(
                project,
                recipe,
                spec,
                col_map,
                row_id,
                column_types=column_types,
            )
            if (
                not hasattr(recipe, "run_agent")
                and not recipe.allow_all_empty_input
                and row_source_values_empty(values)
            ):
                continue
            call = recipe.render(values, spec)
            request = LLMRequest(
                model=str(spec["model"]),
                messages=call.messages,
                schema=call.schema,
                max_tokens=call.max_tokens,
            )
            if cache.get(request_key(request, recipe.version)) is None:
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


def validate_spec(
    project: Project,
    router: ModelRouter,
    run_store: RunResultStore,
    spec: dict,
    *,
    program: Recipe | None = None,
    confirmed: bool,
    resume_run_id: int | None,
    pricing_policy: PricingPolicy,
    composition: ExecutionComposition,
    persistence: Persistence = "durable",
    precomputed_output_fields: list[dict[str, Any]] | None = None,
    consent_coverage: ConsentCoverage | None = None,
) -> _ValidatedSpec:
    """Pure guards, no writes. Resolves the
    recipe + row scope and raises BatchRowLimitExceeded / CostGate /
    MissingProviderKey exactly as the pre-split ``_prepare`` did. Both
    ``run()`` (via ``_prepare``) and ``preview()`` call this so the guards
    run identically whether or not the sample is persisted.

    ``pricing_policy`` is required and keyword-only: this function raises the
    402, and a 402 whose figure nobody rated is the defect the seam exists to
    prevent. Both callers hold a ``MapRunner``, which carries the policy its
    composition root gave it."""
    consent_coverage = effective_consent_coverage(project, consent_coverage)
    recipe = program if program is not None else recipe_for_spec(spec)
    est: dict[str, Any] | None = None
    sheet_id = spec["sheet_id"]
    columns = project.columns(sheet_id)
    col_map = {c["name"]: c["id"] for c in columns}
    has_stored_run_scope = False
    stored_run_scope_count: int | None = None
    explicit_resume_scope: list[int] | None = None
    if resume_run_id is not None:
        stored_run_scope_count = run_store.run_row_scope_count(resume_run_id)
        has_stored_run_scope = stored_run_scope_count is not None
        if spec.get("row_ids") is not None:
            explicit_resume_scope = target_rows(project, spec)
    # Ordinary resumes retain their stored scope; backfill may extend it.
    if has_stored_run_scope:
        if explicit_resume_scope is None:
            row_ids = []
        else:
            stored_run_scope = run_store.run_row_scope(resume_run_id)
            row_ids = list(stored_run_scope)
            seen = set(row_ids)
            row_ids.extend(r for r in explicit_resume_scope if r not in seen)
    else:
        row_ids = target_rows(project, spec)

    max_batch_rows = getattr(recipe, "max_batch_rows", None)
    if (
        isinstance(max_batch_rows, int)
        and max_batch_rows >= 0
        and len(row_ids) > max_batch_rows
    ):
        raise BatchRowLimitExceeded(
            row_count=len(row_ids),
            max_rows=max_batch_rows,
        )

    # Resumes may legitimately retry a subset whose source cells are empty.
    if resume_run_id is None and row_ids:
        source_names = recipe.source_columns(spec)
        if (
            source_names
            and not recipe.allow_all_empty_input
            and source_values_all_empty(
                project, sheet_id, col_map, source_names, row_ids
            )
        ):
            raise EmptyInputColumns(source_names)

    # Refuse forbidden egress before suggesting cost or key remediation.
    assert_network_policy(project, recipe, spec)

    # Strict replay proves only the cached model call cannot egress. Other
    # remote subeffects still require consent.
    model_call_cannot_egress = recipe.is_llm(spec) and model_call_cannot_go_live(router)
    replay_strict_cannot_egress = model_call_cannot_egress and (
        remote_capability_for_spec(
            recipe,
            spec,
            include_model_provider=False,
        )
        is None
    )

    # Estimate, gate, and persistence share one resolution per invocation.
    resolved_execution: ResolvedExecution | None = None
    resolution_refusal: Refusal | None = None
    consent_required = False
    consent_basis = CONSENT_BASIS_CONFIRMED
    unrouted_confirmation_hash: str | None = None
    unrouted_confirmation_quote: dict[str, Any] | None = None
    resume_confirmation: ResumeClaimConfirmation | None = None
    if resume_run_id is None:
        outcome = resolve_for_action(
            project,
            spec,
            recipe,
            composition=composition,
            persistence=persistence,
            scope_row_ids=row_ids,
        )
        if isinstance(outcome, Refusal):
            resolution_refusal = outcome
        else:
            resolved_execution = outcome

    # Validate output shape before hashing a quote. Preserve typed resolution
    # refusals instead of replacing them with output-projection errors.
    output_fields: list[dict[str, Any]] = []
    if resolution_refusal is None:
        output_fields = (
            [dict(field) for field in precomputed_output_fields]
            if precomputed_output_fields is not None
            else [dict(field) for field in recipe.output_fields(spec)]
        )

    if resume_run_id is None:
        estimate_result = _estimate_run(
            project,
            spec,
            program=recipe,
            composition=composition,
            pricing_policy=pricing_policy,
            consent_coverage=consent_coverage,
            resolution=(
                resolution_refusal
                if resolution_refusal is not None
                else resolved_execution
            ),
        )
        est = estimate_result.presentation
        if resolved_execution is not None:
            if estimate_result.bound_resolution is None:
                raise RuntimeError("routed estimate did not bind its consent quote")
            resolved_execution = estimate_result.bound_resolution
            # A confirmation binds only the exact claim set the user saw.
            uncovered = project_uncovered_user_claims(
                project,
                resolved_execution.promise_set,
                spec=spec,
                consent_coverage=consent_coverage,
            )
            if uncovered:
                set_hash = consented_set_hash(resolved_execution.promise_set)
                claims_payload = gate_claims_payload(
                    uncovered,
                    resolved_execution.resolution.facts,
                    resolved_execution.presentation,
                    has_cost_claim=any(
                        promise.field == "cost"
                        for promise in resolved_execution.promise_set.promises
                    ),
                )
                claims_gate = refuse_unless_exact_echo(
                    confirmed=confirmed,
                    echoed_hash=spec.get("consented_promise_set_hash"),
                    expected_hash=set_hash,
                    refuse=lambda: ClaimsGate(
                        quoted_usd(est),
                        gate_usd=float(consent_coverage.threshold_usd),
                        claims=claims_payload,
                        promise_set_hash=set_hash,
                        estimate_details=est,
                    ),
                )
                if claims_gate is not None:
                    raise claims_gate
                consent_required = True
            elif any(
                promise.audience == "user_claim"
                for promise in resolved_execution.promise_set.promises
            ):
                # Materialize exact-match consent for this run so its
                # subject-scoped worker chain is complete and resumable.
                consent_required = True
                consent_basis = (
                    CONSENT_BASIS_EXACT_MATCH
                    if exact_match_consent_covers(
                        project,
                        spec,
                        resolved_execution.promise_set,
                        consent_coverage=consent_coverage,
                    )
                    else CONSENT_BASIS_PREAPPROVED
                )
        else:
            # Unrouted recipes use their existing cost-confirmation path;
            # unavailable routed executions have already refused in estimate_run.
            gate_usd = float(consent_coverage.threshold_usd)
            est = confirmation_estimate(recipe, spec, est, policy=pricing_policy)
            # Gate on billed cost, not provider cost.
            quoted = quoted_usd(est)
            requires_confirmation = not replay_strict_cannot_egress and (
                quoted is None or quoted > gate_usd
            )
            if not replay_strict_cannot_egress and (
                requires_confirmation or quoted != 0 or est.get("requires_confirmation")
            ):
                # Preapproval and explicit confirmation bind the same exact quote.
                context = confirmation_context(
                    project,
                    recipe,
                    spec,
                    row_ids,
                    est,
                )
                context_hash = mint_confirmation_hash(context)
                cost_gate = refuse_unless_exact_echo(
                    confirmed=confirmed,
                    echoed_hash=spec.get("consented_promise_set_hash"),
                    expected_hash=context_hash,
                    refuse=lambda: ClaimsGate(
                        quoted,
                        promise_set_hash=context_hash,
                        remote_claim=bool(est.get("requires_confirmation")),
                        gate_usd=gate_usd,
                        estimate_details=est,
                    ),
                )
                if requires_confirmation and cost_gate is not None:
                    raise cost_gate
                if not recipe.consumes_resolution:
                    unrouted_confirmation_hash = context_hash
                    consent_basis = (
                        CONSENT_BASIS_CONFIRMED
                        if requires_confirmation
                        else CONSENT_BASIS_PREAPPROVED
                    )
                    # Persist the same projection that was hashed.
                    unrouted_confirmation_quote = context.quote.model_dump(mode="json")
    elif explicit_resume_scope is not None:
        # Gate only the rows added by this backfill. Keep the estimate separate
        # from downstream accounting, and retain the resolution a consent binds.
        resume_outcome = resolve_for_action(
            project,
            spec,
            recipe,
            composition=composition,
            persistence=persistence,
            scope_row_ids=row_ids,
        )
        backfill_result = _estimate_run(
            project,
            spec,
            program=recipe,
            composition=composition,
            pricing_policy=pricing_policy,
            resolution=resume_outcome,
            resume_run_id=resume_run_id,
            consent_coverage=consent_coverage,
        )
        backfill_est = backfill_result.presentation
        if recipe.consumes_resolution:
            if isinstance(resume_outcome, ResolvedExecution):
                if backfill_result.bound_resolution is None:
                    raise RuntimeError("routed estimate did not bind its consent quote")
                resume_outcome = backfill_result.bound_resolution
            claims_payload = backfill_est.get("claims")
            if claims_payload:
                set_hash = backfill_est.get("promise_set_hash")
                claims_gate = refuse_unless_exact_echo(
                    confirmed=confirmed,
                    echoed_hash=spec.get("consented_promise_set_hash"),
                    expected_hash=set_hash,
                    refuse=lambda: ClaimsGate(
                        quoted_usd(backfill_est),
                        gate_usd=float(consent_coverage.threshold_usd),
                        claims=claims_payload,
                        promise_set_hash=set_hash,
                        estimate_details=backfill_est,
                    ),
                )
                if claims_gate is not None:
                    raise claims_gate
                # Append against the exact chain head evaluated by this gate.
                resume_confirmation = _resume_confirmation(
                    project, resume_run_id, resume_outcome
                )
        else:
            backfill_est = confirmation_estimate(
                recipe, spec, backfill_est, policy=pricing_policy
            )
            backfill_quoted = quoted_usd(backfill_est)
            requires_confirmation = not replay_strict_cannot_egress and (
                backfill_quoted is None
                or backfill_quoted > float(consent_coverage.threshold_usd)
            )
            if not replay_strict_cannot_egress and (
                requires_confirmation
                or backfill_quoted != 0
                or backfill_est.get("requires_confirmation")
            ):
                context = confirmation_context(
                    project,
                    recipe,
                    spec,
                    row_ids,
                    backfill_est,
                )
                context_hash = mint_confirmation_hash(context)
                queued_resume = _queued_resume_consent_covers(
                    project,
                    recipe,
                    spec,
                    run_id=resume_run_id,
                    principal=consent_coverage.principal,
                    context_hash=context_hash,
                )
                if isinstance(spec.get("_frisket_queued_action_run"), Mapping):
                    if not queued_resume:
                        raise ClaimsGate(
                            backfill_quoted,
                            gate_usd=float(consent_coverage.threshold_usd),
                            promise_set_hash=context_hash,
                            remote_claim=bool(
                                backfill_est.get("requires_confirmation")
                            ),
                            estimate_details=backfill_est,
                        )
                else:
                    cost_gate = refuse_unless_exact_echo(
                        confirmed=confirmed,
                        echoed_hash=spec.get("consented_promise_set_hash"),
                        expected_hash=context_hash,
                        refuse=lambda: ClaimsGate(
                            backfill_quoted,
                            gate_usd=float(consent_coverage.threshold_usd),
                            promise_set_hash=context_hash,
                            remote_claim=bool(
                                backfill_est.get("requires_confirmation")
                            ),
                            estimate_details=backfill_est,
                        ),
                    )
                    if requires_confirmation and cost_gate is not None:
                        raise cost_gate
                if not recipe.consumes_resolution and not queued_resume:
                    unrouted_confirmation_hash = context_hash
                    consent_basis = (
                        CONSENT_BASIS_CONFIRMED
                        if requires_confirmation
                        else CONSENT_BASIS_PREAPPROVED
                    )
                    # Persist the same projection that was hashed.
                    unrouted_confirmation_quote = context.quote.model_dump(mode="json")

    # Keep the refusal guard for paths that do not estimate. An unavailable
    # execution must be configured before it can offer meaningful consent.
    if resolution_refusal is not None:
        raise ExecutionResolutionRefused(resolution_refusal)

    # A configured replay cache may satisfy a row before adapter lookup. Other
    # live provider effects require a key after cost confirmation.
    recipe_is_llm = recipe.is_llm(spec)
    cache_can_serve_replay = recipe_is_llm and (
        router.cache_mode == "replay" and router.cache is not None
    )
    model_name = (spec.get("model") or "") if recipe_is_llm else ""
    effect_engine = model_name
    if not recipe_is_llm:
        effect_engine = (
            resolved_execution.resolution.facts.engine
            if resolved_execution is not None
            else str(spec.get("engine") or "")
        )
    provider = effect_engine.split("/", 1)[0] if "/" in effect_engine else ""
    live_provider_effect = provider and (
        not recipe_is_llm or not model_call_cannot_go_live(router)
    )
    if live_provider_effect:
        # Spend caps read durable project state and fail closed before key
        # remediation; a replay cache does not prove every row will hit.
        assert_provider_spend_cap(project, provider)
        if (
            recipe_is_llm
            and not cache_can_serve_replay
            and router.adapter_for(provider) is None
        ):
            raise MissingProviderKey(provider)

    return _ValidatedSpec(
        consent_coverage=consent_coverage,
        recipe=recipe,
        sheet_id=sheet_id,
        columns=columns,
        col_map=col_map,
        row_ids=row_ids,
        output_fields=output_fields,
        est=est,
        has_stored_run_scope=has_stored_run_scope,
        stored_run_scope_count=stored_run_scope_count,
        explicit_resume_scope=explicit_resume_scope,
        resolved_execution=resolved_execution,
        consent_required=consent_required,
        consent_basis=consent_basis,
        unrouted_confirmation_hash=unrouted_confirmation_hash,
        unrouted_confirmation_quote=unrouted_confirmation_quote,
        resume_confirmation=resume_confirmation,
    )


def _resume_confirmation(
    project: Project,
    resume_run_id: int,
    outcome: ResolvedExecution | Refusal | None,
) -> ResumeClaimConfirmation | None:
    """The head a confirmed resume must append its successor onto (§3.1).

    ``None`` — leaving the prior behavior in place — in exactly two cases,
    both of which the worker's own fence already answers: the resolution
    REFUSED (there is nothing to bind, and the dispatch fails
    ``no_live_target``), or the run has no persisted route chain at all (the
    dispatch fails ``consent_missing``; growing a chain from nothing is the
    fresh-launch writer's job, not a resume's).
    """
    from frisket.engine.store.execution_routes import RouteStore

    if not isinstance(outcome, ResolvedExecution):
        return None
    head = RouteStore.for_run(project, resume_run_id).head()
    if head is None:
        return None
    head_route, head_promise_set = head
    return ResumeClaimConfirmation(
        resolved=outcome,
        head_route_id=head_route.id,
        head_promise_set_id=head_promise_set.id,
    )


def persist_resolved_execution(
    project: Project,
    spec: dict,
    validated: _ValidatedSpec,
    run_id: int | None,
    *,
    receipt_id: str | None = None,
    txn: Any | None = None,
) -> None:
    """Persist the invocation's route artifacts.

    Two shapes, one seat — this is the ONE place ``validate_spec``'s decisions
    become durable consent:

    - a fresh run or preview receipt: consent (iff a consent event happened) + promise
      set + route at an EMPTY chain, inside ``txn`` when the caller supplies
      its run-creation transaction;
    - a resume whose re-gated claims were confirmed: the same
      three rows appended as SUCCESSORS of the head the gate compared
      against, so the worker admits against what the user just confirmed
      rather than against the head that predated it.

    No-op for non-resolution recipes and for resumes that re-asked nothing;
    ephemeral objects are refused by both writers' type checks. Accounted
    previews use a fresh receipt owner, never a resume or a synthetic run.
    """
    if run_id is not None and receipt_id is not None:
        raise ValueError("execution resolution requires exactly one owner")
    if run_id is None and receipt_id is None:
        return
    if (
        validated.has_stored_run_scope
        and validated.unrouted_confirmation_hash is None
        and validated.resume_confirmation is None
        and validated.resolved_execution is None
    ):
        return
    coverage = effective_consent_coverage(project, validated.consent_coverage)
    if txn is None:
        owns_transaction = not project.db.in_transaction
        if owns_transaction:
            project.db.execute("BEGIN IMMEDIATE")
        try:
            persist_resolved_execution(
                project, spec, validated, run_id, receipt_id=receipt_id, txn=project.db
            )
        except BaseException:
            if owns_transaction:
                project.db.rollback()
            raise
        if owns_transaction:
            project.db.commit()
        return
    if run_id is not None:
        project.db.execute(
            "UPDATE runs SET consent_principal=? WHERE id=?",
            (coverage.principal, run_id),
        )
    if validated.unrouted_confirmation_hash is not None:
        from frisket.engine.store.execution_routes import (
            RouteStore,
        )
        from frisket.execution.attempt_authority import attempt_identity

        store = (
            RouteStore.for_receipt(project, receipt_id)
            if receipt_id is not None
            else RouteStore.for_run(project, run_id)
        )
        store.record_consent(
            action_identity_hash=attempt_identity(validated.recipe, spec),
            promise_set_hash=validated.unrouted_confirmation_hash,
            actor=coverage.principal,
            grant_basis=validated.consent_basis,
            # Dispatch needs the rated projection to reconstruct the digest.
            quote=validated.unrouted_confirmation_quote,
            txn=txn,
        )
        return
    confirmation = validated.resume_confirmation
    if confirmation is not None:
        if receipt_id is not None:
            raise ValueError("preview receipts cannot resume")
        from frisket.execution.resolve_for_action import confirm_changed_claims

        confirm_changed_claims(
            project,
            run_id,
            spec,
            confirmation.resolved,
            # Resolve before the writer opens its transaction.
            actor=coverage.principal,
            head_route_id=confirmation.head_route_id,
            head_promise_set_id=confirmation.head_promise_set_id,
            txn=txn,
        )
        return
    if validated.resolved_execution is None:
        return
    from frisket.execution.resolve_for_action import record_resolved_execution

    record_resolved_execution(
        project,
        run_id,
        validated.resolved_execution,
        receipt_id=receipt_id,
        spec=spec,
        consent_required=validated.consent_required,
        consent_basis=validated.consent_basis,
        consent_coverage=coverage,
        txn=txn,
    )
