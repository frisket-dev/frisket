"""Server-derived remoteness for the per-project network gate.

The gate's classification comes ONLY from facts the server owns —

- the static catalog ``required_capabilities`` (``external:*`` prefix) for the
  always-remote kinds (census, geocode, fetch_url, capture_page, web_search);
- the code-owned engine tier tables (``contracts/actions/schemas/_engines.py``
  plus translate's pinned roster) for engine-dependent kinds;
- the provider-kind table (``models/metadata.py``) for LLM rows.

It must NEVER read ``action.capabilities`` — that field is client-supplied
(round-trips from the request body) and bypassable in both directions.

Attacker named: a client that can send action-run/replay
request bodies (a team-edition member, a script holding a session token). It
controls every request string — capabilities, engine ids, model ids — but not
server code, the engine tables, or the project's stored policy. Anyone who
can edit server code or the bundle's sqlite directly already holds the keys;
no defense here targets them.

Tier mapping (contract's ``is_remote`` notion onto the three-tier vocabulary,
commit a0b6d24): ``hosted`` is remote and gated. ``local`` and ``sidecar``
are NOT gated — the disposition explicitly trusts the operator's sidecar and
Ollama by configuration ("not tagged, trusted by configuration"): their URLs
are operator-set and default to the operator's own infrastructure, so their
locality is a shipped-default convention the operator owns, not something
this app-level gate can certify either way. Unknown ``provider/model``-shaped
engine ids and unknown LLM providers classify remote (fail closed).
"""

from __future__ import annotations

from typing import Any

from frisket.ai.models.metadata import is_remote_provider
from frisket.contracts.actions.schemas._engines import (
    OCR_ENGINE_TABLE,
    TO_MARKDOWN_ENGINE_TABLE,
    TRANSCRIBE_ENGINE_TABLE,
    EngineDeclaration,
    find_engine,
)
from frisket.execution.targets import CAPABILITY_TO_MARKDOWN

# translate/ner never drifted into the declaration tables; translate's
# hosted roster is pinned here, mirroring the dispatch branches in
# ops/translate.py (deepl/google_translate hosted vs opus_mt/hy_mt2 local).
HOSTED_TRANSLATE_ENGINES = frozenset({"deepl", "google_translate"})

_ENGINE_TABLES: dict[str, tuple[EngineDeclaration, ...]] = {
    "media.ocr": OCR_ENGINE_TABLE,
    "media.transcribe": TRANSCRIBE_ENGINE_TABLE,
    "media.to_markdown": TO_MARKDOWN_ENGINE_TABLE,
}


def _catalog_external_capability(action_kind: str | None) -> str | None:
    """The static catalog tag for an always-remote kind, else None.

    Reads ``ActionCatalogEntry.required_capabilities`` — Python code, not
    request data. Imported lazily: the definitions registry pulls in the
    whole contracts surface, which must not become a hard import of every
    runner consumer."""
    if not action_kind:
        return None
    from frisket.authoring.action_metadata import declared_action_capabilities

    capabilities = declared_action_capabilities(action_kind)
    # This tag also participates in consent hashes. Catalog capabilities are a
    # set, so their iteration order must not change the quote across processes.
    for capability in sorted(capabilities):
        if str(capability).startswith("external:"):
            return str(capability)
    return None


def _remote_table_engine(recipe: Any, action_kind: str, engine: str) -> str | None:
    # Typed authors may name the action and its Params fields freely. The
    # server-bound capability, not those names or request claims, owns its table.
    table = (
        TO_MARKDOWN_ENGINE_TABLE
        if getattr(recipe, "execution_capability", None) == CAPABILITY_TO_MARKDOWN
        else _ENGINE_TABLES.get(action_kind)
    )
    if table is None:
        return None
    declaration = find_engine(table, engine)
    if declaration is not None:
        return f"engine:{declaration.id}" if declaration.tier == "hosted" else None
    if "/" in engine:
        # A provider/model id (remote VLM OCR, remote transcribe): the
        # provider table decides, defaulting remote for unknown providers.
        provider = engine.split("/", 1)[0]
        if is_remote_provider(provider):
            return f"provider:{provider}"
    return None


def _required_action_kind(spec: dict) -> str:
    """Return the one canonical identity used by policy classification."""
    action_kind = spec.get("action_kind")
    if (
        not isinstance(action_kind, str)
        or action_kind != action_kind.strip()
        or "." not in action_kind
    ):
        raise ValueError("egress classification requires a canonical action_kind")
    return action_kind


def row_effect_spends_or_meters(recipe: Any, spec: dict, router: Any) -> bool:
    """The paidness predicate: whether this one
    row's effect can spend provider money or book a hosted meter.

    True iff a remote capability resolves for the spec (server-owned
    classification below — catalog tags, engine tier tables, the LLM
    provider table keyed on ``spec["model"]``'s provider prefix, all
    fail-closed for unknowns) OR the recipe's own money declaration
    (``cost_class_for``) is not "free" — locally-routed METERED work is
    deliberately included because the hosted GPU meter books at checkpoint
    completion. The one carve-out: a MODEL CALL the router provably cannot
    send live (``model_call_cannot_go_live`` — ``replay_strict`` WITH a cache
    attached, so a miss raises ``CacheMiss`` before egress) can neither spend
    nor meter.

    That carve-out is scoped to the model subeffect, not to the recipe. Two
    halves, both load-bearing:

    - The mode string alone is not the proof. ``_complete_transport``'s strict
      branch is guarded on ``self.cache is not None``, so a cacheless
      ``replay_strict`` router falls through to the live adapter. Reading
      ``cache_mode`` alone here marked such a row free, and MapRunner then ran
      a metered provider call with no reservation — a crash between the
      provider's return and the result transaction left nothing durable, so
      the resume bought the row a second time.
    - An exempt model call does not make the whole effect graph free. Only the
      model-provider contribution and the recipe's (model-priced) cost class
      are suppressed; a remote capability from a NON-model source still counts.
      ``research.answer`` is the live case: its catalog tag is
      ``external:web_search`` and its agent loop performs live search/fetch
      tool calls even when the model response replays from cache, so it stays
      paid, fenced and capped.

    Statically decidable for LLM rows because the router routes strictly on
    ``provider_from_model_id(request.model)`` and every LLM row's request
    model IS ``spec["model"]`` (``row_execution.llm_row``); there is no
    provider fallback in ``ModelRouter._call_with_retry`` or the structured
    completer. The MapRunner row-effect fence, cap accrual, and the
    fence-closure test all consume this predicate.
    """
    if recipe.requires_row_effect_checkpoint(spec):
        return True

    # Lazy: keeps this module's import surface light for its many
    # server/team consumers; the runner has the router loaded anyway.
    from frisket.ai.llm.router import model_call_cannot_go_live

    if recipe.is_llm(spec) and model_call_cannot_go_live(router):
        # The model call is proven free. Anything else this row's effect
        # reaches is not, so keep classifying WITHOUT the model provider.
        return (
            remote_capability_for_spec(
                recipe,
                spec,
                include_model_provider=False,
            )
            is not None
        )
    return (
        remote_capability_for_spec(recipe, spec) is not None
        or recipe.cost_class_for(spec) != "free"
    )


def row_effect_cannot_egress(recipe: Any, spec: dict) -> bool:
    """Whether a fenced row's effect provably cannot reach a remote provider.

    Derived from the same classification as :func:`row_effect_spends_or_meters`:
    within the fenced set this is exactly "no remote capability resolves"
    (fenced solely because its cost class meters local work), minus the LLM
    fail-closed case — a fenced LLM row's halt codes prove nothing about
    egress (unknown codes normalize to ``local_session_failed``), so its
    reservations must stand (commit 07556a97).
    """
    if recipe.requires_row_effect_checkpoint(spec) or recipe.is_llm(spec):
        return False
    return remote_capability_for_spec(recipe, spec) is None


def remote_capability_for_spec(
    recipe: Any,
    spec: dict,
    *,
    include_model_provider: bool = True,
) -> str | None:
    """The server-resolved remote capability a runner spec would exercise,
    or None when the spec resolves entirely local/sidecar.

    One check covers both contract classes: always-remote kinds via their
    static catalog tag (so the replay path cannot re-run a web_search/geocode
    live under ``off``), and engine-dependent kinds via the tier tables and
    the provider table. Never consults ``capabilities`` on the spec.

    ``include_model_provider=False`` drops ONLY the final LLM
    ``spec["model"]`` provider branch, leaving the catalog and engine
    classifications intact. It answers "what else does this spec reach?" for
    a caller holding a separate proof that the model call itself cannot go
    live (:func:`row_effect_spends_or_meters`'s strict-replay carve-out), so
    that proof stays scoped to the model subeffect. It is NOT an egress
    answer: the network gate always classifies with the model provider in."""
    canonical_kind = _required_action_kind(spec)
    # Installed typed plugins have project-scoped declarations, not entries in
    # the first-party catalog. This attribute comes from the checked host program.
    if getattr(recipe, "external_capability", None) == "external:opencorporates":
        return "external:opencorporates"
    external = _catalog_external_capability(canonical_kind)
    if external is not None:
        return external

    if canonical_kind == "map.translate":
        engine = str(spec.get("engine") or "llm")
        if engine in HOSTED_TRANSLATE_ENGINES:
            return f"engine:{engine}"
    else:
        engine = spec.get("engine")
        if isinstance(engine, str) and engine:
            remote = _remote_table_engine(recipe, canonical_kind, engine)
            if remote is not None:
                return remote

    if not include_model_provider:
        return None
    try:
        recipe_is_llm = bool(recipe.is_llm(spec))
    except Exception:
        recipe_is_llm = False
    if recipe_is_llm:
        model = spec.get("model")
        if isinstance(model, str) and model:
            provider = model.split("/", 1)[0]
            if provider and is_remote_provider(provider):
                return f"provider:{provider}"
    return None
