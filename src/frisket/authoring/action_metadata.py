"""Shared public action metadata for current-system run records."""

from __future__ import annotations

from dataclasses import dataclass

PRODUCT_EDITION_SNAPSHOT_KEY = "_frisket_product_edition"
SOLO_ONLY_ACTION_KINDS = frozenset({"map.mcp_extract"})


def canonical_action_kind(action_kind: object) -> str:
    """Return a canonical action kind without translating aliases."""
    raw = str(action_kind or "")
    return raw if raw == raw.strip() and "." in raw else ""


def action_available_in_edition(action_kind: object, edition: object) -> bool:
    """Whether a built-in action may be exposed or admitted by this edition."""
    kind = canonical_action_kind(action_kind)
    return kind not in SOLO_ONLY_ACTION_KINDS or str(edition).strip().lower() == "solo"


def action_edition_unavailable_message(action_kind: object, edition: object) -> str:
    kind = canonical_action_kind(action_kind) or str(action_kind or "unknown")
    return f"the '{kind}' action is available only in the Solo edition"


def product_edition_from_snapshot(snapshot: object) -> str | None:
    """Read the public worker-admission marker from an opaque edition snapshot."""
    from collections.abc import Mapping

    if not isinstance(snapshot, Mapping):
        return None
    value = snapshot.get(PRODUCT_EDITION_SNAPSHOT_KEY)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


@dataclass(frozen=True)
class GatedCapabilityRule:
    """One capability requirement the FRISKET_ALLOW_CODE_RECIPES=0 guards refuse.

    ``capabilities`` is matched as a SUBSET of what an action declares, so a
    rule naming two capabilities fires only for an action declaring both.
    ``phrase`` is operator-facing and goes into the refusal message verbatim.
    """

    capabilities: frozenset[str]
    phrase: str


# What FRISKET_ALLOW_CODE_RECIPES=0 refuses, stated as capability
# REQUIREMENTS rather than as a list of action kinds. No action kind is named
# here: the guards read each action's DECLARED ``required_capabilities`` out
# of the contract registry, so a new action declaring unsafe:local_code is
# gated the day it lands with no list for anyone to remember to update.
#
# Model-directed research can both search and fetch pages. A fixed Quick Search
# also uses a model to summarize its snippets, but never opens pages or chooses
# follow-up queries, so model completion + search alone does not imply an agent.
CODE_RECIPE_GATED_CAPABILITY_RULES: tuple[GatedCapabilityRule, ...] = (
    GatedCapabilityRule(
        capabilities=frozenset({"unsafe:local_code"}),
        phrase="the unsafe:local_code capability (un-sandboxed local code execution)",
    ),
    GatedCapabilityRule(
        capabilities=frozenset(
            {"model:complete", "external:web_search", "external:http_fetch"}
        ),
        phrase=(
            "the model:complete + external:web_search + external:http_fetch capabilities "
            "(a model-directed web agent loop)"
        ),
    ),
)


def declared_action_capabilities(action_kind: object) -> frozenset[str]:
    """The capabilities an action kind DECLARES in the contract registry.

    Noncanonical, plugin-contributed, and unknown kinds have no built-in
    declaration and return the empty set.
    """
    from frisket.actions.registry import ACTION_REGISTRY

    kind = canonical_action_kind(action_kind)
    try:
        registered = ACTION_REGISTRY.get(kind)
    except KeyError:
        return frozenset()
    return frozenset(registered.required_capabilities)


def gated_capability_phrase(action_kind: object) -> str | None:
    """The operator-facing phrase for the gated capability an action declares,
    or None when this deployment's code-action gate does not cover it.

    The env gate itself stays at each call site; this only classifies a
    canonical action from what its contract declares.
    """
    declared = declared_action_capabilities(action_kind)
    for rule in CODE_RECIPE_GATED_CAPABILITY_RULES:
        if rule.capabilities <= declared:
            return rule.phrase
    return None


def run_row_action_kind(row: object) -> str:
    """Return the required canonical ``action_kind`` from a persisted run."""
    try:
        stamped = row["action_kind"]  # type: ignore[index]
    except (KeyError, IndexError):
        stamped = None
    action_kind = canonical_action_kind(stamped)
    if not action_kind:
        raise ValueError("persisted run has no canonical action_kind")
    return action_kind


def action_metadata_for_action_kind(action_kind: object) -> dict[str, str]:
    from frisket.actions.registry import ACTION_REGISTRY

    raw = str(action_kind or "").strip()
    action_kind = canonical_action_kind(raw) or "unknown"
    try:
        title = ACTION_REGISTRY.get(action_kind).definition.title
    except KeyError:
        title = None
    return {
        "action_kind": action_kind,
        "action_name": title or "Unknown action",
    }
