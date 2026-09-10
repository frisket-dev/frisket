"""Fence closure: every paid producer operates under effect-checkpoint authority.

Lane 2.4's invariant (hardening spec, Lane 2): **one predicate decides "this
call spends money"; every call site that predicate marks paid operates under
checkpoint authority; this file makes silently leaving the set impossible.**

The row-grain predicate is ``row_effect_spends_or_meters`` (remote capability
resolves OR the recipe's cost class is not free).  This file applies the same
two facts at catalog grain — ``cost_policy.kind != "none"`` OR an
``external:*`` required capability — and asserts the resulting paid set is
partitioned, with nothing left over, into:

- **row_effect** — kinds that execute per-row through MapRunner's ONE effect
  site (``map_runner.one_row``), where the predicate gates a durable
  reserve→returned→consume checkpoint.  Behavior pinned by
  ``test_map_row_effect_checkpoint.py`` (paid row reserves; free row writes
  zero checkpoints).
- **shared-store families** — inline/queued executors that reserve on the
  ``EffectCheckpointStore`` (``reduce_group_summary``,
  ``embedding_index_refresh``, and ``model_call`` for clustering). Behavior
  pinned by ``test_reduce_effect_checkpoint``, ``test_embedding_effect_checkpoint``,
  and ``test_typed_cluster_action``.
- **named exclusions** — kinds genuinely outside checkpoint authority, each
  with the concrete reason it cannot silently double-buy.

Enumeration is mechanical (the typed action registry plus the remaining
catalog definitions), never a hand list: a NEW paid catalog kind, or a paid
program reached through the batch branch (which bypasses the per-row fence),
turns this file red with an instructive message until someone either fences
it or records its exclusion here with a reason.  The red path itself is
exercised below by registering a synthetic unfenced paid producer.

Surfaces outside the catalog registry, recorded here so they are named
rather than silent (hardening spec "Recorded declines"):

- **scratch compare/store-reader previews** — ephemeral paid egress with no
  run behind it is refused outright; that closure has its own mechanical file
  (``tests/preview/test_preview_billable_closure.py``) walking the scratch
  ``frisket.preview`` package and its store-reader service.
- **action preview** — MapRunner classifies the canonical action with the same
  row-effect predicate as durable execution, but refuses effectful work that
  lacks a durable run checkpoint. That admission owner and its no-dispatch
  boundary are pinned by
  ``tests/preview/test_action_preview_effect_fence.py``.
"""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from frisket.actions.core import (
    MapBatch,
    MapRows,
    ModelRows,
    RegisteredAction,
    _ProjectAction,
)
from frisket.actions.cluster_types import ValueClusterer
from frisket.actions.registry import ACTION_REGISTRY
from frisket.contracts.action import ActionCatalogEntry


def _catalog(definition) -> ActionCatalogEntry:
    if isinstance(definition, RegisteredAction):
        return ActionCatalogEntry.model_validate(definition.catalog_entry())
    return definition.catalog


def _catalog_marks_paid(definition) -> bool:
    """Catalog-grain paidness: the same two server-owned facts
    ``row_effect_spends_or_meters`` derives from — a non-free cost policy or
    an ``external:*`` capability tag (both fail-closed: ``unknown`` cost is
    paid)."""

    catalog = _catalog(definition)
    if catalog.cost_policy.kind != "none":
        return True
    return any(
        str(capability).startswith("external:")
        for capability in catalog.required_capabilities or []
    )


#: Paid kinds whose provider calls happen under a NON-row-effect checkpoint
#: authority.  Values name the durable mechanism and the suite that pins it.
CHECKPOINT_AUTHORITIES: dict[str, str] = {
    # Dimension discovery is bounded, but can still be paid. The host persists
    # receipt pre-egress reservation and returned probe checkpoint before index
    # publication; ambiguous outcomes refuse re-egress.
    "embedding.index_create": (
        "receipt embedding_dimension_probe_egress_reservation and "
        "embedding_dimension_probe_checkpoint"
    ),
    # Prepared clustering executes an internal batch. Its embedding adapter
    # reserves before egress, checkpoints returned vectors/accounting, and
    # consumes the checkpoint only with atomic canonical publication.
    "cluster.values": "EffectCheckpointStore family model_call",
    # embeddings._refresh_index reserves per provider batch on the shared
    # store, family "embedding_index_refresh" (test_embedding_effect_checkpoint).
    "embedding.index_refresh": "EffectCheckpointStore family embedding_index_refresh",
    # reduces._complete_reduce_group_summaries reserves per group on the
    # shared store, family "reduce_group_summary" (test_reduce_effect_checkpoint).
    "reduce.group_summary": "EffectCheckpointStore family reduce_group_summary",
    # exports._run_export_google_sheets reserves before client.export_tabs on
    # the shared store, family "export_google_sheets_egress"
    # (test_export_google_sheets_effect_checkpoint). $0 cost, but
    # destination.mode="new_spreadsheet" calls the provider's
    # create-spreadsheet endpoint on every egress: a crash-then-stale-clear
    # retry with no fence created a SECOND spreadsheet, not an idempotent
    # overwrite -- the EXCLUSIONS reasoning this replaced ("re-run overwrites
    # the same sheet") only ever held for mode="update_existing".
    "export.google_sheets": "EffectCheckpointStore family export_google_sheets_egress",
}

#: Paid kinds that spend only by resuming an existing run through MapRunner's
#: one effect site — the row_effect fence applies there, not in their own
#: executor.
DELEGATES_TO_ROW_EFFECT = {
    # runs._run_backfill re-enters MapRunner.run(resume_run_id=...); every
    # provider call it can cause is a row execution under the same fence.
    "run.backfill",
}

#: Paid kinds genuinely OUTSIDE checkpoint authority.  Each line carries the
#: concrete reason a replay cannot silently double-buy.  A kind may live here
#: only while its reason stays true; the rot checks below force this map to
#: shrink when a kind stops being paid or leaves the catalog.
EXCLUSIONS: dict[str, str] = {
    # The one metered batch recipe.  execute_batch recipes take MapRunner's
    # whole-run batch branch, which bypasses the per-row checkpoint fence —
    # sanctioned ONLY because the US Census API bills nothing per call ($0
    # today, hardening spec "Recorded declines: batch-recipe/preview effect
    # protocol").  test_only_census_is_a_paid_batch_recipe goes red the day a
    # second paid batch recipe appears.
    "enrich.census_demographics": (
        "$0 batch recipe: whole-run execute_batch branch, Census API has no "
        "per-call price; a replay re-fetches free data"
    ),
    # Plain downloads of caller-named public URLs.  No provider credential
    # resolves, no provider fact or meter row is written; a retry re-fetches
    # at no provider charge and the receipt idempotency replay stops a
    # double-append.
    "import.urls": (
        "unpriced public-URL download (pre-txn fetch, no provider "
        "credential/fact); receipt idempotency replays the completed import"
    ),
    "media.enclosure_materialize": (
        "unpriced enclosure download of an already-known URL; no provider "
        "credential/fact; blob store dedupes by content hash"
    ),
    # map.find is a runless generic action job enqueued with max_attempts=1.
    # Its queued/running receipt plus idempotency key prevents an exact manual
    # replay from dispatching again; a lost worker attempt terminalizes rather
    # than automatically buying the scan twice. If Find becomes retryable, it
    # must move to a returned-response checkpoint before that change lands.
    "map.find": (
        "single-attempt queued action with a durable idempotency receipt; "
        "failed/expired work is terminalized and never automatically re-egressed"
    ),
    # HTTP poll of the user's own registered feed URL.  No provider
    # credential; polling repeatedly is the product behavior, and a failed
    # poll persists as a durable source_run trace, not a retryable charge.
    "source.poll": (
        "feed poll of the user's registered URL; no provider credential; "
        "re-polling is the intended behavior, not a purchase"
    ),
    # Local browser capture of a caller-named page; cost_policy is "none" —
    # no provider invoice exists.
    "web.capture_page": (
        "local page capture (external:url_capture, cost none): no provider "
        "invoice; re-capture costs nothing at any provider"
    ),
}


def _paid_catalog_kinds() -> dict[str, object]:
    return {
        action.action_id: action
        for action in ACTION_REGISTRY.actions
        if _catalog_marks_paid(action)
    }


def _typed_action(kind: str) -> RegisteredAction | None:
    return next(
        (action for action in ACTION_REGISTRY.actions if action.action_id == kind), None
    )


def _runs_per_row_under_map_runner(kind: str) -> bool:
    """True when the kind executes per-row through MapRunner's fenced effect
    site.  Mechanical: the typed registry maps the kind to a per-row terminal
    (``MapRows`` / ``ModelRows``); a ``MapBatch`` terminal takes the whole-run
    branch, which never reaches the per-row checkpoint fence."""

    typed = _typed_action(kind)
    if typed is None:
        return False
    terminal = typed.definition.run
    # MapBatch subclasses MapRows: a batch terminal is the whole-run branch,
    # never the fenced per-row site.
    return isinstance(terminal, (MapRows, ModelRows)) and not isinstance(
        terminal, MapBatch
    )


def _runs_batch_under_map_runner(kind: str) -> bool:
    typed = _typed_action(kind)
    if typed is None:
        return False
    terminal = typed.definition.run
    return isinstance(terminal, MapBatch) or (
        isinstance(terminal, _ProjectAction)
        and terminal.capabilities == (ValueClusterer,)
    )


def unfenced_paid_producers() -> list[str]:
    """Paid catalog kinds with NO checkpoint authority and NO recorded
    exclusion — the set this file exists to keep empty.  Each entry is an
    instructive sentence naming the kind and what to do about it."""

    problems: list[str] = []
    for kind, definition in sorted(_paid_catalog_kinds().items()):
        catalog = _catalog(definition)
        if kind in CHECKPOINT_AUTHORITIES or kind in DELEGATES_TO_ROW_EFFECT:
            continue
        if kind in EXCLUSIONS:
            continue
        if _runs_per_row_under_map_runner(kind):
            # Fenced at MapRunner's one effect site: row_effect_spends_or_meters
            # gates a durable reserve before any possible egress.
            continue
        if _runs_batch_under_map_runner(kind):
            problems.append(
                f"{kind} is paid ({catalog.cost_policy.kind}) and its "
                "recipe executes through MapRunner's BATCH branch, which "
                "bypasses the per-row effect-checkpoint fence. A paid batch "
                "recipe can buy the same provider work twice across a crash. "
                "Give it checkpoint authority (reserve on EffectCheckpointStore "
                "before egress) or record a reasoned exclusion in "
                "EXCLUSIONS in tests/engine/test_paid_effect_fence_closure.py."
            )
        else:
            problems.append(
                f"{kind} is paid ({catalog.cost_policy.kind}"
                f"{', external capability' if catalog.cost_policy.kind == 'none' else ''}) "
                "but operates under NO effect-checkpoint authority: it is not a "
                "per-row MapRunner kind, holds no shared-store family, and has "
                "no recorded exclusion. A crash between its provider return and "
                "its result commit buys the same work twice. Reserve on "
                "EffectCheckpointStore at its effect site (see reduces.py / "
                "embeddings.py for the ported shape) or record a reasoned "
                "exclusion in tests/engine/test_paid_effect_fence_closure.py."
            )
    return problems


# --------------------------------------------------------------------------- #
# The closure itself


def test_every_paid_catalog_kind_operates_under_checkpoint_authority() -> None:
    assert unfenced_paid_producers() == []


def test_the_paid_set_is_not_empty() -> None:
    """Guards the guard: if the paidness projection ever returned nothing,
    every closure assertion above would pass vacuously."""

    paid = _paid_catalog_kinds()
    assert "map.ask" in paid
    assert "enrich.geocode" in paid
    assert len(paid) >= 20


def test_core_llm_kinds_are_fenced_at_the_row_effect_site() -> None:
    """The load-bearing positive half: the money kinds really do classify as
    per-row MapRunner work (where test_map_row_effect_checkpoint pins the
    fence's behavior), rather than falling through to an exclusion."""

    for kind in (
        "map.ask",
        "map.classify",
        "map.extract",
        "map.judge",
        "map.ner",
        "map.summarize",
        "map.translate",
        "enrich.geocode",
        "join.semantic",
        "media.ocr",
        "media.transcribe",
        "media.to_markdown",
        "research.answer",
        "research.web_search",
        "map.api_call",
        "web.capture_screenshot",
        "media.fetch_url",
        "media.ytdlp_download",
    ):
        assert _runs_per_row_under_map_runner(kind), (
            f"{kind} no longer executes per-row under MapRunner's fenced "
            "effect site; its checkpoint authority must be re-established "
            "and this file updated"
        )


def test_only_census_is_a_paid_batch_recipe() -> None:
    """The batch branch bypasses the per-row fence, so the set of PAID kinds
    reaching it is pinned to exactly the sanctioned $0 case.  Red here means
    a new paid batch recipe appeared: it needs checkpoint authority before
    it ships, not an exclusion by silence."""

    paid_batch = sorted(
        kind
        for kind in _paid_catalog_kinds()
        if _runs_batch_under_map_runner(kind) and kind not in CHECKPOINT_AUTHORITIES
    )
    assert paid_batch == ["enrich.census_demographics"], (
        "paid kinds now execute through MapRunner's unfenced batch branch: "
        f"{paid_batch}. Only census_demographics ($0 per call) is sanctioned; "
        "any other paid batch recipe must reserve on EffectCheckpointStore "
        "before egress."
    )


def test_exclusion_and_authority_maps_do_not_rot() -> None:
    """Every mapped kind must still exist in the catalog and still be paid.
    A kind that went free or disappeared must be REMOVED from these maps, so
    the maps never absorb strangers by stale name."""

    paid = set(_paid_catalog_kinds())
    for name, mapping in (
        ("CHECKPOINT_AUTHORITIES", set(CHECKPOINT_AUTHORITIES)),
        ("DELEGATES_TO_ROW_EFFECT", set(DELEGATES_TO_ROW_EFFECT)),
        ("EXCLUSIONS", set(EXCLUSIONS)),
    ):
        stale = sorted(mapping - paid)
        assert not stale, (
            f"{name} names kinds that are no longer paid catalog kinds: "
            f"{stale}. Remove them (the exclusion reason no longer applies)."
        )
    # The maps must also stay disjoint: one kind, one classification.
    assert not (set(CHECKPOINT_AUTHORITIES) & set(EXCLUSIONS))
    assert not (set(DELEGATES_TO_ROW_EFFECT) & set(EXCLUSIONS))
    assert not (set(CHECKPOINT_AUTHORITIES) & set(DELEGATES_TO_ROW_EFFECT))


def test_shared_store_family_names_match_the_executors() -> None:
    """The authority map's family claims are read back from the executor
    modules, not trusted as prose."""

    from frisket.engine.executor.action_families.embeddings import (
        _EMBEDDING_REFRESH_EFFECT_FAMILY,
    )
    from frisket.engine.executor.action_families.exports import (
        _EXPORT_GOOGLE_SHEETS_EFFECT_FAMILY,
    )
    from frisket.engine.executor.group_summary_runtime import (
        _REDUCE_EFFECT_FAMILY,
    )
    from frisket.engine.store.runs import MODEL_CALL_CHECKPOINT_FAMILY

    assert _REDUCE_EFFECT_FAMILY == "reduce_group_summary"
    assert _EMBEDDING_REFRESH_EFFECT_FAMILY == "embedding_index_refresh"
    assert _EXPORT_GOOGLE_SHEETS_EFFECT_FAMILY == "export_google_sheets_egress"
    assert MODEL_CALL_CHECKPOINT_FAMILY == "model_call"
    assert MODEL_CALL_CHECKPOINT_FAMILY in CHECKPOINT_AUTHORITIES["cluster.values"]
    assert _REDUCE_EFFECT_FAMILY in CHECKPOINT_AUTHORITIES["reduce.group_summary"]
    assert (
        _EMBEDDING_REFRESH_EFFECT_FAMILY
        in CHECKPOINT_AUTHORITIES["embedding.index_refresh"]
    )
    assert (
        _EXPORT_GOOGLE_SHEETS_EFFECT_FAMILY
        in CHECKPOINT_AUTHORITIES["export.google_sheets"]
    )


# --------------------------------------------------------------------------- #
# Red-proof: disable the fence once and show the test notices — every fence
# is disabled once in a test to prove its tripwire fires.


def test_a_new_paid_kind_without_authority_turns_this_file_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate the exact drop this migration risks: a NEW paid producer that
    never took checkpoint authority.  The closure must go red and say so."""

    # A model-metered project action (the reduce family's terminal) registered
    # under a new id: paid, not per-row, no shared-store authority recorded.
    probe = RegisteredAction(
        "reduce.synthetic_paid_probe",
        ACTION_REGISTRY.get("reduce.group_summary").definition,
    )
    assert _catalog(probe).cost_policy.kind == "model_metered"
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, probe.action_id: probe},
    )
    problems = unfenced_paid_producers()
    assert len(problems) == 1
    message = problems[0]
    assert "reduce.synthetic_paid_probe" in message
    assert "NO effect-checkpoint authority" in message
    assert "EffectCheckpointStore" in message


def test_a_typed_paid_probe_without_its_authority_turns_this_file_red(monkeypatch):
    monkeypatch.delitem(CHECKPOINT_AUTHORITIES, "embedding.index_create")
    problems = unfenced_paid_producers()
    assert len(problems) == 1
    assert "embedding.index_create is paid (model_metered)" in problems[0]
    assert "NO effect-checkpoint authority" in problems[0]


def test_cluster_batch_without_its_authority_turns_this_file_red(monkeypatch):
    monkeypatch.delitem(CHECKPOINT_AUTHORITIES, "cluster.values")
    problems = unfenced_paid_producers()
    assert len(problems) == 1
    assert "cluster.values is paid" in problems[0]
    assert "BATCH branch" in problems[0]


def test_a_paid_recipe_on_the_batch_branch_turns_this_file_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other escape shape: a paid per-row kind whose terminal becomes a
    ``MapBatch`` slides off the per-row fence onto the unfenced batch branch.
    The closure must name that mechanism."""

    extract = ACTION_REGISTRY.get("map.extract")
    assert _runs_per_row_under_map_runner("map.extract")
    batch_terminal = ACTION_REGISTRY.get("enrich.census_demographics").definition.run
    assert isinstance(batch_terminal, MapBatch)
    # Swap the terminal underneath the registered model kind without
    # re-deriving its examples: the probe keeps map.extract's identity and
    # catalog, only its execution branch changes.
    probe = copy.copy(extract)
    object.__setattr__(probe, "action_id", "map.synthetic_batch_probe")
    object.__setattr__(
        probe, "definition", replace(extract.definition, run=batch_terminal)
    )
    assert _catalog_marks_paid(probe)
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, probe.action_id: probe},
    )
    assert not _runs_per_row_under_map_runner(probe.action_id)
    problems = unfenced_paid_producers()
    assert len(problems) == 1
    message = problems[0]
    assert "map.synthetic_batch_probe" in message
    assert "BATCH branch" in message
    assert "bypasses the per-row effect-checkpoint fence" in message


def test_a_typed_paid_batch_without_authority_turns_this_file_red(monkeypatch):
    census = ACTION_REGISTRY.get("enrich.census_demographics")
    probe = RegisteredAction("enrich.synthetic_batch_probe", census.definition)
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        {**ACTION_REGISTRY._actions, probe.action_id: probe},
    )
    problems = unfenced_paid_producers()
    assert len(problems) == 1
    assert "enrich.synthetic_batch_probe" in problems[0]
    assert "BATCH branch" in problems[0]
    assert "bypasses the per-row effect-checkpoint fence" in problems[0]
