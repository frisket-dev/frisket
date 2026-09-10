"""Live plugin recipe lookup and shipped runner declarations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from frisket.ops.base import Recipe
from frisket.ops.builtin import get_recipe


@dataclass
class _PluginRecipe(Recipe):
    consumes_resolution = False
    cost_class = "free"
    name: str = "configured_runner"
    llm: bool = False
    cache: dict[str, Any] = field(default_factory=dict)


def test_plugin_lookup_preserves_configured_instance_and_replacement(monkeypatch):
    from frisket.authoring import plugin_registry

    registry = plugin_registry.PluginRegistry()
    monkeypatch.setattr(plugin_registry, "_DEFAULT", registry)
    configured = _PluginRecipe(cache={"configured": True})
    registry.register_recipe(configured, action_kind="demo.configured")
    assert get_recipe("demo.configured") is configured
    assert get_recipe("demo.configured").cache == {"configured": True}
    replacement = _PluginRecipe(cache={"replacement": True})
    registry.register_recipe(replacement, action_kind="demo.configured", replace=True)
    assert get_recipe("demo.configured") is replacement
    registry.unregister_recipe("demo.configured")
    with pytest.raises(ValueError, match="unknown action kind"):
        get_recipe("demo.configured")


@pytest.mark.parametrize(
    "kind", ["configured_runner", " demo.configured", "demo.configured "]
)
def test_recipe_lookup_refuses_noncanonical_keys(kind):
    with pytest.raises(ValueError, match="canonical"):
        get_recipe(kind)


def _shipped_recipe_classes() -> list[type[Recipe]]:
    """Every ``Recipe`` subclass frisket ships (test doubles excluded)."""
    # The shipped Recipe classes live behind lazy imports; load them so the
    # subclass walk is a closure over the product, not over whatever an
    # earlier test happened to import.
    import frisket.engine.executor.map_rows_action  # noqa: F401

    def walk(klass: type[Recipe]):
        for sub in klass.__subclasses__():
            if sub.__module__.startswith("frisket."):
                yield sub
            yield from walk(sub)

    return sorted(
        set(walk(Recipe)), key=lambda klass: f"{klass.__module__}.{klass.__qualname__}"
    )


def test_every_registered_recipe_declares_consumes_resolution() -> None:
    """The marker is a required declaration, not a default (E-3).

    ``Recipe.consumes_resolution`` is an annotation with no value, so an
    undeclared recipe raises at its first consumer rather than silently
    reading as "this recipe does not compose with the execution seam" — the
    dormant-wiring shape six ``getattr(..., False)`` sites used to permit.
    Built-ins are typed actions in ``ACTION_REGISTRY`` and register no legacy
    recipe through the SDK seam, so the closure is over the ``Recipe`` classes
    frisket still ships (the typed program adapter and the plugin recipes):
    each declares the marker on the class itself.
    """
    from frisket.actions.registry import ACTION_REGISTRY

    assert ACTION_REGISTRY.action_ids

    shipped = _shipped_recipe_classes()
    assert shipped
    undeclared = []
    for klass in shipped:
        declared = any("consumes_resolution" in vars(base) for base in klass.__mro__)
        if not declared:
            undeclared.append(klass.__qualname__)
        else:
            assert isinstance(klass.consumes_resolution, bool), klass.__qualname__
    assert undeclared == [], (
        "these shipped recipes never declared consumes_resolution; add "
        "``consumes_resolution = True/False`` to the class (see "
        "frisket.ops.base.Recipe)"
    )

    # And the declaration is meaningful: no shipped class answers True at the
    # class level. Routed media/geocoders are typed programs reconstructed by
    # request, which switch the marker per instance (checked below).
    routed = sorted(
        klass.__qualname__ for klass in shipped if klass.consumes_resolution
    )
    assert routed == []

    # A recipe that consumes resolution must also say WHICH capability it
    # resolves for (routed OCR): the answer picks the roster, the candidate target
    # rows, the authored option set and the meter, and no default could be
    # right for a third capability. Recipes that consume no resolution have
    # no route and honestly declare nothing.
    from frisket.execution.targets import EXECUTION_CAPABILITIES

    assert all(klass.execution_capability is None for klass in shipped)

    from frisket.engine.executor.queued_actions import queued_v1_action_request

    for kind, capability in (
        ("enrich.geocode", "geocode"),
        ("enrich.census_demographics", "census_demographics"),
        ("media.to_markdown", "document.convert"),
        ("media.ocr", "ocr"),
        ("media.transcribe", "transcribe"),
    ):
        queued = queued_v1_action_request(
            {
                "action_id": kind,
                "scope": {"kind": "sheet_rows", "sheet_id": 1},
                "params": {"source": "source"},
                "idempotency_key": f"routed-owner:{kind}",
            }
        )
        assert queued is not None and queued.program is not None
        assert queued.program.consumes_resolution is True
        assert queued.program.execution_capability == capability
        assert capability in EXECUTION_CAPABILITIES
