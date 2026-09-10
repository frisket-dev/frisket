"""``AttemptAuthority`` — the ONE construction of an ``AttemptCommitment``.

This replaces ``build_route_verification_check``, a callback that
raised-or-returned-``None``, with an object that
returns the artifact. The four sites that wired that callback
(``executor/actions`` ×2, ``server/mcp/backends``, ``engine/jobs/runs``)
inherit the seat unchanged — the authority is built by the composition root
and handed to ``MapRunner`` as a **required** argument, so the recurring
"optional wiring" failure (a capability passed as an optional parameter that
some call site forgets) is not representable here: every ``MapRunner``
construction must supply the authority explicitly.

**One head read.** ``mint`` reads the route/promise-set head ONCE, derefs the
binding ONCE from that head, and evaluates ONCE. Everything downstream reads
the returned commitment. That retires the second head read, ``bound_route_id``
and the three-argument ``run_start_check`` type, and it is why the queued
handler no longer pre-loads the binding — the typed
``RouteBindingUnavailable`` -> ``no_live_target`` mapping lives HERE now.

**Two variants.** :class:`AttemptAuthority` can admit routed work.
:class:`UnroutedOnlyAuthority` cannot: its ``mint`` refuses when the recipe
consumes resolution. That is the type-level half of dependent-choice
enforcement; the runtime half is the single ``isinstance`` check in
``MapRunner.run`` immediately after the mint. Together they replace
``UnverifiedRoutedRunRefusal`` and its effect-site presence check.
"""

from __future__ import annotations
from frisket.execution.consent_coverage import (
    ConsentCoverage,
    effective_consent_coverage,
)

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from frisket.execution.attempt import (
    AttemptCommitment,
    RoutedAdmission,
    UnroutedAdmission,
    abandon_stale_dispatching_attempts,
    admit_attempt,
    cost_basis_from_promises,
    open_attempt,
    set_attempt_state,
    _attempt_owner,
)
from frisket.execution.provider import (
    ExecutionComposition,
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.execution.pricing_policy import default_pricing_policy
from frisket.execution.runtime_binding import (
    ExecutionRouteVerificationFailed,
    RouteBindingUnavailable,
)
from frisket.execution.resolver import CandidateBinding


class DependentChoiceRefusal(RuntimeError):
    """A surface that must never execute routed work was asked to.

    The successor of ``UnverifiedRoutedRunRefusal``: same fail-closed
    behavior, same ``execution_wiring_error`` code (deliberately NOT
    ``consent_missing`` — that code sends the operator to confirmation, and no
    confirmation repairs missing wiring), but reached from a decided type
    rather than from the ABSENCE of a callback."""

    code = "execution_wiring_error"

    def __init__(self, remedy: str) -> None:
        super().__init__(remedy)
        self.remedy = remedy


#: Recorded in ``execution_attempts.action_identity_hash`` when a canonical
#: action identity is not projectable for this dispatch. Identity projection
#: needs a registered action declaration whose ``runner_spec_extra`` keys are
#: enumerated (``execution/action_identity``), which plugin-generated kinds
#: and several ops with a spec-extra hook deliberately do not have. It is
#: REQUIRED on the routed path — consent binds to it — and simply does not
#: exist for unrouted dispatch, so the row says so instead of fabricating a
#: hash nothing could compare against.
UNPROJECTABLE_IDENTITY = "unprojectable"


def attempt_identity(recipe: Any, spec: dict[str, Any]) -> str:
    from frisket.execution.action_identity import action_identity_hash

    try:
        return action_identity_hash(spec)
    except Exception:
        if recipe.consumes_resolution:
            # Fail closed: a routed attempt whose identity cannot be
            # projected cannot be matched against any consent, so refusing
            # here is the same answer the coverage check would give.
            raise
        return UNPROJECTABLE_IDENTITY


@dataclass(frozen=True)
class ConsentSurvey:
    """What one dispatch's candidate consents proved, and why the rest did not.

    Two outcomes travel together because the caller's refusal depends on both:
    an empty ``proving`` with a skewed candidate is a DIAGNOSABLE refusal
    (the deployment re-priced), while an empty ``proving`` with none is the
    ordinary "your work changed". Returning the bare list left the second
    message standing in for the first, which is the failure the paid-plugin
    lane's skew fence already names on its side of the seam.
    """

    proving: list[Any]
    skewed_policy_ids: tuple[str, ...]
    installed_policy_id: str


@dataclass(frozen=True)
class AttemptAuthority:
    """Mints the commitment that authorizes one dispatch."""

    project: Any
    composition: ExecutionComposition
    consent_coverage: ConsentCoverage | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.composition, ExecutionComposition):
            raise TypeError("AttemptAuthority requires a full ExecutionComposition")

    def mint(
        self,
        *,
        recipe: Any,
        spec: dict[str, Any],
        run_id: int | None = None,
        receipt_id: str | None = None,
        scope: tuple[int, ...],
        commit: bool = True,
        prepared_binding: CandidateBinding | None = None,
        prepared_work_scope: Mapping[str, Any] | None = None,
    ) -> AttemptCommitment:
        """``created`` then ``admitted`` (§1.5), in that order.

        Infrastructure/decode failures normalize to the DURABLE
        ``consent_missing`` failure — consent that cannot be verified is
        absent proof — and never escape as a generic job retry. A
        ``RecipeInvocationHalt`` from a consented-fact authorization check
        propagates untouched; the halt-aware caller converts it to a
        resumable cancelled run.
        """
        from frisket.ops.base import RecipeInvocationHalt

        # Recovery before admission (§1.5): a `dispatching` row left behind by
        # a crashed process is closed EXPLICITLY here, so it cannot block a
        # later retry or resume forever.
        if not commit and not self.project.db.in_transaction:
            raise RuntimeError(
                "AttemptAuthority.mint(commit=False) requires "
                "a caller-owned transaction"
            )
        _attempt_owner(run_id, receipt_id)
        if receipt_id is not None:
            if any(type(row_id) is not int for row_id in scope) or len(
                set(scope)
            ) != len(scope):
                raise ValueError(
                    "receipt attempt scope requires distinct integer row ids"
                )
            owner = self.project.db.execute(
                "SELECT 1 FROM receipts WHERE id=? AND run_id IS NULL AND status='running'",
                (receipt_id,),
            ).fetchone()
            if owner is None:
                raise ExecutionRouteVerificationFailed(
                    "stale_head", "receipt attempt requires its running reservation"
                )
        else:
            abandon_stale_dispatching_attempts(
                self.project,
                run_id,
                commit=commit,
            )

        identity = attempt_identity(recipe, spec)
        attempt_id, seq = open_attempt(
            self.project,
            run_id=run_id,
            receipt_id=receipt_id,
            identity=identity,
            scope=scope,
            commit=commit,
        )
        try:
            admission = self._admit(
                recipe,
                spec,
                run_id,
                identity,
                scope,
                prepared_binding=prepared_binding,
                prepared_work_scope=prepared_work_scope,
                receipt_id=receipt_id,
            )
        except RecipeInvocationHalt:
            # §1.5's `-> halted` transition. This consented-fact refusal
            # occurred before effect; terminalize the just-opened attempt
            # before propagating it to the halt-aware caller.
            set_attempt_state(
                self.project,
                attempt_id,
                "halted",
                commit=commit,
            )
            raise
        if isinstance(admission, RoutedAdmission):
            cost_basis = cost_basis_from_promises(admission.promise_set.promises)
        else:
            cost_basis = cost_basis_from_promises(())
        from frisket.execution.promise_compiler import PricedCostBasis

        terms_version = (
            cost_basis.terms_version
            if isinstance(cost_basis, PricedCostBasis)
            else None
        )
        commitment = AttemptCommitment(
            attempt_id=attempt_id,
            run_id=run_id,
            receipt_id=receipt_id,
            seq=seq,
            identity=identity,
            scope=scope,
            admission=admission,
            cost_basis=cost_basis,
            price_card_version=terms_version,
        )
        admit_attempt(self.project, commitment, commit=commit)
        return commitment

    def load_admitted(
        self,
        *,
        attempt_id: str,
        recipe: Any,
        spec: dict[str, Any],
        run_id: int | None = None,
        receipt_id: str | None = None,
    ) -> AttemptCommitment:
        """Reconstruct one exact persisted admitted attempt for dispatch."""

        _attempt_owner(run_id, receipt_id)
        row = self.project.db.execute(
            "SELECT * FROM execution_attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        if (
            row is None
            or row["run_id"] != run_id
            or row["receipt_id"] != receipt_id
            or str(row["state"]) != "admitted"
        ):
            raise ExecutionRouteVerificationFailed(
                "stale_head",
                "the prepared receipt does not name an admitted attempt for this run",
            )
        try:
            raw_scope = json.loads(row["scope_json"])
            if not isinstance(raw_scope, list) or any(
                isinstance(value, bool) for value in raw_scope
            ):
                raise ValueError("attempt row scope must be a JSON list of row ids")
            scope = tuple(int(value) for value in raw_scope)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ExecutionRouteVerificationFailed(
                "stale_head",
                "the prepared attempt carries a malformed row scope",
            ) from exc
        identity = attempt_identity(recipe, spec)
        if str(row["action_identity_hash"]) != identity:
            raise ExecutionRouteVerificationFailed(
                "stale_head",
                "the prepared attempt no longer matches this action identity",
            )
        admission = self._admit(
            recipe,
            spec,
            run_id,
            identity,
            scope,
            receipt_id=receipt_id,
        )
        if isinstance(admission, RoutedAdmission):
            if (
                row["head_route_id"] != admission.head_route_id
                or row["head_promise_set_id"] != admission.head_promise_set_id
            ):
                raise ExecutionRouteVerificationFailed(
                    "stale_head",
                    "the prepared attempt no longer names the admitted route head",
                )
            cost_basis = cost_basis_from_promises(
                admission.promise_set.promises,
            )
        else:
            cost_basis = cost_basis_from_promises(())
        from frisket.execution.promise_compiler import PricedCostBasis

        terms_version = (
            cost_basis.terms_version
            if isinstance(cost_basis, PricedCostBasis)
            else None
        )
        if row["price_card_version"] != terms_version:
            raise ExecutionRouteVerificationFailed(
                "stale_head",
                "the prepared attempt no longer carries the pinned terms version",
            )
        return AttemptCommitment(
            attempt_id=str(row["id"]),
            run_id=run_id,
            receipt_id=receipt_id,
            seq=int(row["seq"]),
            identity=identity,
            scope=scope,
            admission=admission,
            cost_basis=cost_basis,
            price_card_version=terms_version,
        )

    # ---------- admission ----------

    def _proving_consent(
        self,
        consents: Any,
        *,
        recipe: Any,
        spec: dict[str, Any],
        live_facts: dict[str, Any],
        authorized_scope: Any,
        identity: str,
        principal: str,
        echoed_hash: Any,
    ) -> ConsentSurvey:
        """The consents whose recorded hash this dispatch can reproduce.

        **The split, exactly.** For each candidate consent the envelope is
        rebuilt from:

        * live, recomputed NEUTRAL facts — ``live_facts`` (this invocation's
          fresh ``estimate_run``: cost, rows, cost_source, pricing_key,
          engine), the freshly derived egress verdict, the authorized row
          scope, and the canonical work-scope binding recomputed inside
          ``confirmation_context_hash``. Every one of those is re-checked, and
          a change in any of them is what makes the hash stop matching. This
          is the whole verification and it is unchanged;
        * the RATED projection read off THAT consent — ``billed_cost`` and
          ``policy_id``, and nothing else (``persisted_rating``). Those two cannot be
          recomputed here: the policy that produced them belongs to the
          deployment and is deliberately not wired into dispatch, because
          rating at dispatch would let a tariff change silently re-price an
          approved run.

        Reading the rated half per-consent is why this is a loop rather than
        one hash compared against many rows: two consents on one run may carry
        two different quotes, and each must be tested against ITS own figure.
        It is not circular — a consent supplies only the two fields it is
        trusted for, and a row whose ``cost``, ``rows``, ``pricing_key`` or
        scope no longer match the live facts fails exactly as before, no
        matter what its persisted quote says.

        **Why the rated read needs a fence beside it.** Rebuilding the hash
        from a consent's OWN persisted rating means that consent always
        re-verifies its rated half, whatever the deployment now bills — so
        the tariff swap this design refuses to perform at dispatch would
        instead be performed by the SETTLEMENT, against a figure the user
        never saw. The explicit comparison below is the fence: a candidate
        quoted under a policy this process does not install can never prove,
        no matter how well its hash reproduces. It is a separate check
        precisely so the hash stays stable — the rating is still read back,
        not recomputed.

        A candidate whose ``quote_json`` is missing or does not decode proves
        nothing and is skipped; if that leaves nothing proving, the caller
        raises the modeled ``consent_missing``. There is no default rating and
        no fallback to an unrated hash — an unverifiable consent authorizes
        nothing.
        """
        from frisket.engine.runner.confirmation_context import (
            BILLED_COST_KEY,
            POLICY_ID_KEY,
            ConsentQuote,
            ConsentQuoteRefused,
            persisted_rating,
        )
        from frisket.engine.runner.validation import (
            confirmation_context_hash,
            consented_confirmation_estimate,
        )

        installed_policy_id = default_pricing_policy().policy_id
        proving: list[Any] = []
        skewed: dict[str, None] = {}
        for consent in consents:
            if consent.action_identity_hash != identity or consent.actor != principal:
                continue
            if echoed_hash is not None and consent.promise_set_hash != str(echoed_hash):
                continue
            if consent.quote_json is None:
                # No persisted quote: nothing to reconstruct the rated half
                # from. Fails closed by proving nothing.
                continue
            try:
                persisted = ConsentQuote.model_validate(json.loads(consent.quote_json))
                if persisted.policy_id != installed_policy_id:
                    # The price list moved under an approved run. Skipped
                    # PER CANDIDATE, like every other unusable record here, so
                    # a sibling consent minted under the current policy still
                    # gets its turn; the id is kept so the caller's refusal can
                    # name both books instead of reporting a changed price as a
                    # changed scope.
                    skewed[persisted.policy_id] = None
                    continue
                # The hash is minted INSIDE the guard, not after it. A record
                # can decode as JSON, validate as a quote, and still refuse at
                # the money projection — ``billed_cost: -1`` is accepted by
                # pydantic and refused by ``_quote_micros``. Minted outside,
                # that ``ConsentQuoteRefused`` escapes the loop and one
                # poisoned row denies a run whose valid sibling consent would
                # have authorized it.
                candidate_hash = confirmation_context_hash(
                    self.project,
                    recipe,
                    spec,
                    authorized_scope,
                    consented_confirmation_estimate(
                        recipe, spec, live_facts, rating=persisted_rating(persisted)
                    ),
                )
            except (json.JSONDecodeError, ValidationError):
                # A blob that is not a well-formed quote proves nothing — and
                # it does so PER CANDIDATE, so one corrupt row cannot stop a
                # sibling consent on the same run from authorizing the
                # dispatch, and cannot escape as a raw decode error either.
                # Narrow on purpose: an infrastructure failure reading the row
                # must stay an infrastructure error (the caller's normalizer
                # re-raises anything outside the value/lookup family) rather
                # than being reported to the operator as absent consent.
                continue
            except ConsentQuoteRefused as exc:
                # The projection refused — but WHICH half? The merged envelope
                # carries live neutral facts and this record's rated fields,
                # and the two failures are not the same event:
                #
                #   * a rated key (``billed_cost``/``policy_id``) is THIS
                #     record's problem, so it proves nothing and a sibling
                #     consent still gets its turn;
                #   * anything else is the LIVE estimate refusing to project
                #     (a recipe now emitting a non-finite cost). That is
                #     run-fatal and must escape to the caller, which turns it
                #     into the ``consent_missing`` that NAMES the offending
                #     key — swallowing it here would replace a precise
                #     diagnosis with a generic "no consent matched".
                if exc.key not in (BILLED_COST_KEY, POLICY_ID_KEY):
                    raise
                continue
            if consent.promise_set_hash == candidate_hash:
                proving.append(consent)
        return ConsentSurvey(
            proving=proving,
            skewed_policy_ids=tuple(skewed),
            installed_policy_id=installed_policy_id,
        )

    def _admit(
        self,
        recipe: Any,
        spec: dict[str, Any],
        run_id: int | None,
        identity: str,
        scope: tuple[int, ...],
        *,
        prepared_binding: CandidateBinding | None = None,
        prepared_work_scope: Mapping[str, Any] | None = None,
        receipt_id: str | None = None,
    ) -> RoutedAdmission | UnroutedAdmission:
        from frisket.ops.base import RecipeInvocationHalt

        if not recipe.consumes_resolution:
            from frisket.engine.store.execution_routes import (
                RouteStore,
            )

            store = (
                RouteStore.for_receipt(self.project, receipt_id)
                if receipt_id is not None
                else RouteStore.for_run(self.project, run_id)
            )
            consents = store.consents()
            if not consents:
                return UnroutedAdmission()
            principal = effective_consent_coverage(
                self.project, self.consent_coverage
            ).principal
            echoed_hash = spec.get("consented_promise_set_hash")
            from frisket.engine.runner.confirmation_context import (
                ConsentQuoteRefused,
            )
            from frisket.engine.runner.validation import (
                estimate_run,
                target_rows,
            )
            from frisket.engine.store.runs import RunResultStore

            run_store = RunResultStore(self.project)
            authorized_scope = (
                run_store.run_row_scope(run_id)
                if run_id is not None and run_store.has_run_row_scope(run_id)
                else list(scope)
            )
            if not set(scope).issubset(set(authorized_scope)):
                raise ExecutionRouteVerificationFailed(
                    "consent_missing",
                    "this attempt includes rows outside the scope recorded for "
                    "its confirmation; re-run the action to confirm the current "
                    "work",
                )
            # The durable run scope is the authorization envelope, but a
            # backfill's quote is for this invocation's explicit delta.  The
            # validation gate hashes exactly that pairing: full merged scope
            # (so no row can be smuggled into the run) plus an estimate over
            # spec.row_ids (so completed historical rows are not quoted
            # again). Recomputing both from ``authorized_scope`` here made a
            # valid exact-echo backfill impossible: validation persisted the
            # one-row quote, while admission reconstructed an N-row quote and
            # rejected its own consent as ``consent_missing``.
            quoted_scope = (
                target_rows(self.project, spec)
                if spec.get("row_ids") is not None
                else authorized_scope
            )
            # Recomputing the live quote is the whole verification, and it
            # runs recipe code (estimate(), the work-scope binding, the
            # canonical encoder). Validation can refuse a bad estimate
            # outright, because a 400 is something a user can act on; here
            # the ONLY modeled outcome is refuse-and-re-confirm, and an
            # escaping exception is strictly worse than a mismatch — the
            # same estimate throws on every retry, so a consented run
            # becomes permanently unrunnable instead of re-confirmable.
            #
            # So the whole block takes the same normalization the routed
            # branch below uses, with the same exclusions: a halt or an
            # already-typed verification failure passes through, and
            # anything outside the recognized value/lookup family (OSError,
            # sqlite failures, MemoryError) stays an infra error rather than
            # being reported as absent consent.
            try:
                live_facts = estimate_run(
                    self.project,
                    spec,
                    program=recipe,
                    scope_row_ids=quoted_scope,
                )
                # ONE live recomputation of the neutral facts, then ONE
                # candidate hash per consent — because the rated half is read
                # from the consent being tested, not computed here (see
                # ``_proving_consent``).
                survey = self._proving_consent(
                    consents,
                    recipe=recipe,
                    spec=spec,
                    live_facts=live_facts,
                    authorized_scope=authorized_scope,
                    identity=identity,
                    principal=principal,
                    echoed_hash=echoed_hash,
                )
            except (ExecutionRouteVerificationFailed, RecipeInvocationHalt):
                raise
            except ConsentQuoteRefused as exc:
                # The one case that can name the offending fact.
                raise ExecutionRouteVerificationFailed(
                    "consent_missing",
                    "this run's quote can no longer be projected onto the "
                    f"money facts its confirmation bound ({exc.key}); re-run "
                    "the action to confirm the current work",
                ) from exc
            except Exception as exc:  # noqa: BLE001 — normalized durable failure
                if not isinstance(exc, (ValueError, KeyError, TypeError)):
                    raise
                raise ExecutionRouteVerificationFailed(
                    "consent_missing",
                    "this run's quote could not be recomputed "
                    f"({type(exc).__name__}: {exc}); consent that cannot be "
                    "verified authorizes nothing — re-run the action to "
                    "confirm the current work",
                ) from exc
            if not survey.proving:
                if survey.skewed_policy_ids:
                    # Named separately because "your work changed" is not what
                    # happened and re-running the same work would not fix it:
                    # the deployment re-priced between the confirmation and
                    # this dispatch. Same shape as the paid-plugin lane's
                    # ``plugin_action_pricing_policy_skew`` — both ids, nothing
                    # spent, and the remedy is a fresh quote.
                    quoted = ", ".join(
                        repr(policy_id) for policy_id in survey.skewed_policy_ids
                    )
                    raise ExecutionRouteVerificationFailed(
                        "consent_missing",
                        f"this run was quoted under pricing policy {quoted}, but "
                        "the deployment dispatching it prices with "
                        f"{survey.installed_policy_id!r}; nothing was spent. A "
                        "confirmation binds the figure it quoted, so re-run the "
                        "action to get a current quote and confirm it",
                    )
                raise ExecutionRouteVerificationFailed(
                    "consent_missing",
                    "this run's live rows, source revisions, or quote no longer "
                    "match its persisted confirmation; re-run the action to "
                    "confirm the current work",
                )
            # Consent is not a consumable token.  Every row in ``proving`` is
            # already subject-scoped to THIS run and exact-matched on action
            # identity, actor, live scope/content, and quote.  A same-run
            # retry may therefore name the same durable authority; filtering
            # ids used by earlier attempts would still allow the effect while
            # erasing the receipt's answer to "what authorized it?".  The
            # subject + identity + context predicates above retain the
            # one-action boundary that prevents cross-action reuse.
            return UnroutedAdmission(admitted_by_consent_id=survey.proving[-1].id)
        try:
            return admit_routed(
                self.project,
                run_id,
                spec,
                composition=self.composition,
                consent_coverage=self.consent_coverage,
                recipe=recipe,
                scope=scope,
                prepared_binding=prepared_binding,
                prepared_work_scope=prepared_work_scope,
                receipt_id=receipt_id,
            )
        except (ExecutionRouteVerificationFailed, RecipeInvocationHalt):
            raise
        except RouteBindingUnavailable as exc:
            # §1.6: the three RouteBindingUnavailable catch sites collapse into
            # the authority. A persisted route whose pinned target no longer
            # derefs is the DURABLE `no_live_target` refusal — never a
            # route-without-binding dict riding into an adapter, never a
            # generic retry.
            raise ExecutionRouteVerificationFailed(
                "no_live_target", exc.remedy
            ) from exc
        except Exception as exc:  # noqa: BLE001 — normalized durable failure
            from frisket.engine.store.execution_routes import RouteStoreError

            if not isinstance(exc, (RouteStoreError, ValueError, KeyError, TypeError)):
                raise
            raise ExecutionRouteVerificationFailed(
                "consent_missing",
                "this run's execution-route verification could not complete "
                f"({type(exc).__name__}: {exc}); consent that cannot be "
                "verified authorizes nothing — re-run the action to record a "
                "current execution route",
            ) from exc


@dataclass(frozen=True)
class _DirectActionRecipe:
    """Recipe-shaped identity for a direct (non-MapRunner) action family.

    The reduce/embedding executors call providers directly, outside any
    recipe dispatch; the authority still needs the two recipe facts it
    consults (``name`` for messages, ``consumes_resolution`` for the
    dependent-choice question).  Direct families never consume execution
    resolution — routed work only dispatches through MapRunner — so the
    answer is a constant, not a per-call claim."""

    name: str
    consumes_resolution: bool = False


def mint_direct_effect_attempt(
    project: Any,
    *,
    action_kind: str,
    run_id: int,
    scope: tuple[int, ...],
) -> AttemptCommitment:
    """Mint AND claim the attempt that authorizes one direct family's egress.

    The reduce and embedding-refresh executors make
    live paid provider calls with no attempt in scope, so their facts carried
    NULL ``attempt_id`` (settlement-invisible).  This is their one honest
    mint: the attempt is opened against the family's own durable run at
    execution start — BEFORE the first provider call — claimed through the
    same one-dispatch-per-run CAS as MapRunner work, and its immutable id is
    what the family pins onto every checkpoint and fact write.  Reading
    ``runs.current_attempt_id`` at fact-write time instead is exactly the
    mutable-pointer pattern ``_attempt_for_fact_write`` exists to refuse.

    Raises :class:`frisket.execution.attempt.AttemptClaimRefused` when the
    run already has a live ``dispatching`` attempt (a crashed process inside
    the stale window); nothing was dispatched and nothing was spent."""

    from frisket.execution.attempt import claim

    commitment = AttemptAuthority(
        project,
        composition=open_execution_composition(
            project, None, ExecutionCompositionContext.direct()
        ),
    ).mint(
        recipe=_DirectActionRecipe(name=action_kind),
        spec={"action_kind": action_kind},
        run_id=run_id,
        scope=scope,
    )
    claim(project, commitment, claimless_direct_effect=True)
    return commitment


@dataclass(frozen=True)
class UnroutedOnlyAuthority:
    """The authority for surfaces that must never execute routed work.

    Previews, replay, derive, semantic-join and the enqueue-time probes have
    no persisted route and no consent chain of their own; formerly each
    of them was simply a ``MapRunner`` that forgot a keyword, and the
    effect-site fence existed to notice. None of the seven is a live
    near-miss; what this buys is CHECKABILITY — the impossible state is now
    named by a type instead of inferred from an absent callback.
    """

    project: Any = None

    def mint(
        self,
        *,
        recipe: Any,
        spec: dict[str, Any],
        run_id: int,
        scope: tuple[int, ...],
        commit: bool = True,
    ) -> AttemptCommitment:
        """Refuse routed work by TYPE; mint ordinary unrouted work normally.

        The refusal is the whole point of the variant, but it is scoped to
        the one question the variant answers: does this recipe consume
        execution resolution? Everything else — every local recipe these
        surfaces legitimately run — mints exactly as it would anywhere else.
        """
        if recipe.consumes_resolution:
            raise DependentChoiceRefusal(
                f"recipe '{getattr(recipe, 'name', recipe)}' consumes execution "
                "resolution, but this dispatch surface is wired with an "
                "unrouted-only attempt authority and can never authorize a "
                "routed run; routed dispatch never executes unverified"
            )
        return AttemptAuthority(
            self.project,
            composition=open_execution_composition(
                self.project, None, ExecutionCompositionContext.direct()
            ),
        ).mint(
            recipe=recipe,
            spec=spec,
            run_id=run_id,
            scope=scope,
            commit=commit,
        )


# ---------------------------------------------------------------------------
# The admission itself — ONE head read, ONE deref, ONE evaluation
# ---------------------------------------------------------------------------


def admit_routed(
    project: Any,
    run_id: int | None,
    spec: dict[str, Any],
    *,
    composition: ExecutionComposition,
    recipe: Any | None = None,
    scope: tuple[int, ...] | None = None,
    prepared_binding: CandidateBinding | None = None,
    prepared_work_scope: Mapping[str, Any] | None = None,
    receipt_id: str | None = None,
    consent_coverage: ConsentCoverage | None = None,
) -> RoutedAdmission:
    """Claim-time admission for one routed project run.

    Formerly ``engine.jobs.runs._verify_execution_route``, moved here whole so
    the head it verifies is the head the dispatch binds — one read, one
    object, no ``bound_route_id`` reconciliation.

    - A resolution-consuming run WITHOUT persisted route artifacts is ALWAYS
      the durable ``consent_missing`` failure — no archaeology.
    - Coverage uses persisted artifacts only, and it is exact: a consent on
      this subject must match the head promise-set hash, canonical action
      identity, and actor, or admission returns ``consent_missing``. Foreign
      consents from a restored bundle therefore fail closed. Component-wise
      coverage (standing
      thresholds, the high-water envelope, verbatim recurrence) lived here
      until cleanup; the reachability proof
      showed the exact match is TRUE in every reachable state that carries
      user claims, because a promise set and the consent that authorizes it
      are written from the same object in one transaction and a cost claim
      never occurs without the categorical egress claim beside it. The
      component-wise read stays where it IS reachable: the validation gate
      (``project_uncovered_user_claims``), which asks about a DIFFERENT
      subject's consents.
    - ``candidate_binding`` dereferences the target for connection and
      liveness only, never a re-choice; refusal becomes ``no_live_target``.
    - ``evaluate_set`` over the candidate facts records every non-satisfied
      row in ``route_violations`` (never silent) and raises
      ``RecipeInvocationHalt`` before effect. The promise-set hash is the
      confirmation identity, so a live fact that cannot satisfy any row in
      that set cannot execute under the old confirmation. Availability and
      support facts that do not compile into the set remain outside this
      consent decision.
    """
    from frisket.engine.store.execution_routes import RouteStore
    from frisket.execution.promises import SATISFIED, Promise, evaluate_set
    from frisket.execution.price_book import (
        funding_for_cost_posture,
        live_cost_fact,
        offering_matches_cost_basis,
    )
    from frisket.execution.resolve_for_action import (
        action_identity_hash,
    )
    from frisket.execution.resolver import (
        Refusal,
        candidate_binding,
        route_row_facts_from_row,
    )
    from frisket.ops.base import RecipeInvocationHalt

    if not isinstance(composition, ExecutionComposition):
        raise TypeError("admit_routed requires a full ExecutionComposition")

    _attempt_owner(run_id, receipt_id)
    store = (
        RouteStore.for_receipt(project, receipt_id)
        if receipt_id is not None
        else RouteStore.for_run(project, run_id)
    )
    head = store.head()
    if head is None:
        raise ExecutionRouteVerificationFailed(
            "consent_missing",
            "this run has no persisted execution route artifacts, so its "
            "consent cannot be verified; re-run the action to resolve (and "
            "consent to) a current execution route",
        )
    route, promise_set_row = head
    promises = tuple(Promise.from_row(row) for row in promise_set_row.promises)

    # The consent row that authorized THIS dispatch, when the admission is a
    # single row rather than an envelope: the receipt reads its `grant_basis`
    # to say whether the user confirmed directly or the gate derived it from
    # an identical already-consented action (B6).
    admitting_consent_id: str | None = None
    user_claims = [p for p in promises if p.audience == "user_claim"]
    if user_claims:
        expected_identity = action_identity_hash(spec)
        principal = effective_consent_coverage(project, consent_coverage).principal
        exact = next(
            (
                consent
                for consent in store.consents()
                if consent.promise_set_hash == promise_set_row.promise_set_hash
                and consent.action_identity_hash == expected_identity
                and consent.actor == principal
            ),
            None,
        )
        if exact is None:
            fields = ", ".join(sorted({claim.field for claim in user_claims}))
            raise ExecutionRouteVerificationFailed(
                "consent_missing",
                "no persisted consent covers this run's user claims "
                f"({fields}); re-run the action and confirm the claims "
                "to record consent",
            )
        admitting_consent_id = exact.id

    # The project is the provider's secrets source: the Datalab target's
    # key can live in the project's own store, and an admission that could not
    # see it would refuse a run whose validation admitted it.
    try:
        # ONE projection from the persisted row: the binding this admission
        # derefs IS the binding dispatch uses, so the two readers of a route
        # row can never disagree about its facts.
        facts = route_row_facts_from_row(route)
    except ValueError as exc:
        raise ExecutionRouteVerificationFailed(
            "consent_missing",
            f"route {route.id} on this run records unusable execution facts "
            f"({exc}), so its consent cannot be verified; re-run the action "
            "to resolve (and consent to) a current execution route",
        ) from exc
    if prepared_binding is not None:
        # Atomic queue publication just resolved and persisted this exact
        # route in the caller-owned transaction. Reusing that resolution's
        # already-probed connection avoids a second configuration read before
        # the job is even visible. The persisted worker loads the admitted
        # attempt later without this argument and performs the ordinary fresh
        # candidate dereference immediately before effect.
        if prepared_binding.facts != facts:
            raise ExecutionRouteVerificationFailed(
                "stale_head",
                "the prepared route no longer matches the resolution admitted "
                "in this publication transaction",
            )
        binding = prepared_binding
    else:
        binding = candidate_binding(facts, composition.provider)
        if isinstance(binding, Refusal):
            raise ExecutionRouteVerificationFailed("no_live_target", binding.remedy)

    # Candidate facts: what the live deref independently observes (E-1).
    # ``cost_posture`` stays OUT entirely: it is a route-row policy with no
    # live counterpart at all, and no compiled promise reads it — supplying it
    # would only be the route row scored against itself.
    #
    # ``credential_source`` is not here either, and no longer compiles into
    # the promise set at all: on a zero-tariff transport supplying
    # it scored the route row against itself, and on a tariffed transport the
    # provenance does not exist until the model call. The credential question
    # is answered by the PRE-EFFECT use constraint at the adapter
    # (``frisket.execution.credential_use``) and by the post-effect epoch
    # observation, not by a run-start promise.
    observed = {
        "operator": binding.target.operator,
        "egress_class": binding.target.egress_class,
        "region": binding.target.region,
    }
    work_scope_snapshot: dict[str, Any] | None = None
    if any(
        isinstance((promise.basis or {}).get("work_scope"), Mapping)
        for promise in promises
    ):
        from frisket.engine.store.runs import RunResultStore
        from frisket.execution.scope_identity import canonical_work_scope_binding

        if recipe is None:
            from frisket.engine.runner.validation import recipe_for_spec

            recipe = recipe_for_spec(spec)
        run_store = RunResultStore(project)
        authorized_scope = (
            run_store.run_row_scope(run_id)
            if run_id is not None and run_store.has_run_row_scope(run_id)
            else list(scope or ())
        )
        work_scope_snapshot = {}
        live_scope = (
            dict(prepared_work_scope)
            if prepared_work_scope is not None
            else canonical_work_scope_binding(
                project,
                recipe,
                spec,
                authorized_scope,
                snapshot=work_scope_snapshot,
            )
        )
        if scope is not None and not set(scope).issubset(set(authorized_scope)):
            live_scope = {
                **live_scope,
                "identity": "attempt_scope_outside_authorized_scope",
            }
        observed["work_scope"] = live_scope
    # The live cost fact comes from the current exact offering (or the neutral
    # provider-direct source), never from ``ConnectionConfig.extra``.  Dispatch
    # configuration therefore cannot impersonate commercial terms.
    from frisket.execution.targets import validated_target_snapshot

    snapshot = validated_target_snapshot(route.target_snapshot)
    current_offering = composition.offering_for(
        target_id=str(snapshot["target_id"]),
        capability=str(snapshot["capability"]),
        engine=route.engine,
    )
    current_match_exists = composition.supplies_resolution_match(
        target_id=str(snapshot["target_id"]),
        capability=str(snapshot["capability"]),
        engine=route.engine,
    )
    current_match_included = composition.includes_execution_match(
        target_id=str(snapshot["target_id"]),
        capability=str(snapshot["capability"]),
        engine=route.engine,
    )
    pinned_cost_basis = cost_basis_from_promises(
        promises,
    )
    from frisket.execution.promise_compiler import PricedCostBasis
    from frisket.execution.commercial import commercial_offering_allowed

    offering_refusal: str | None = None
    if current_offering is not None:
        if not isinstance(pinned_cost_basis, PricedCostBasis):
            offering_refusal = (
                "the composition now supplies an offer, but the pinned route "
                "does not carry that offer's complete priced basis"
            )
        elif not offering_matches_cost_basis(current_offering, pinned_cost_basis):
            offering_refusal = (
                "the composition's current offer no longer exactly matches "
                "the pinned quote and settlement terms"
            )
    elif (
        isinstance(pinned_cost_basis, PricedCostBasis)
        and pinned_cost_basis.terms_version is not None
    ):
        offering_refusal = "the composition no longer supplies the pinned offer"
    elif (
        route.cost_posture == "platform_metered"
        and not current_match_included
        and commercial_offering_allowed(
            target_id=str(snapshot["target_id"]),
            capability=str(snapshot["capability"]),
            engine=route.engine,
        )
    ):
        # A funding marker is never enough to authorize a commercial effect.
        # This also closes historical funding-first routes whose old promise
        # happened to carry a zero or unpriceable basis instead of the offer
        # they should have pinned.
        offering_refusal = "the composition supplies no exact offer for this route"
    elif not current_match_exists:
        offering_refusal = (
            "the composition no longer supplies the pinned target, "
            "capability, and engine"
        )
    cost_fact = live_cost_fact(
        # The capability comes off the route's own target snapshot: the
        # persisted route is what this fence reads for every other fact, and
        # a SKU is capability-keyed now — the same venue meters an OCR page
        # and an audio second differently. The exact current snapshot schema
        # requires the capability; old rows refuse rather than default.
        capability=str(snapshot["capability"]),
        target_id=str(snapshot["target_id"]),
        engine=route.engine,
        funding=funding_for_cost_posture(route.cost_posture),
        offering=current_offering,
        hardware_class=str(binding.connection.extra.get("gpu") or "") or None,
    )
    evaluation = evaluate_set(promises, {**observed, "cost": cost_fact})
    consent_refusals: list[str] = []
    if offering_refusal is not None:
        consent_refusals.append(f"cost: {offering_refusal}")
    for index, (promise, result) in enumerate(evaluation.results):
        if result.status == SATISFIED:
            continue
        # Keep the diagnostic ledger, but never confuse recording with
        # authorization: this exact row contributed to the persisted promise
        # set that authorizes dispatch (and, for claim-bearing runs, to the
        # exact hash the user confirmed), so a violated OR unevaluable live
        # result requires current authorization before any effect.
        store.record_violation(
            route_id=route.id,
            promise_set_id=promise_set_row.id,
            promise=promises[index].to_row(),
            observed=observed,
        )
        consent_refusals.append(f"{promise.field}: {result.reason or result.status}")
    if consent_refusals:
        raise RecipeInvocationHalt(
            "promise_violation",
            "the live execution terms or authorized work scope no longer "
            "satisfy this attempt's authorized promise set "
            f"({'; '.join(consent_refusals)}); review and confirm the "
            "current terms and work scope before resuming",
        )

    return RoutedAdmission(
        head_route_id=route.id,
        head_promise_set_id=promise_set_row.id,
        route=route,
        promise_set=promise_set_row,
        binding=binding,
        evaluation=evaluation,
        admitted_by_consent_id=admitting_consent_id,
        work_scope_snapshot=work_scope_snapshot,
    )
