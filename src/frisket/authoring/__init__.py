"""Authoring layer: how actions, recipes, plugins, and the workbench are defined.

Holds the action/recipe registries and metadata, column types, templates,
output naming, the copilot, the workbench, plugin development tooling, and the
bundled plugin tree. The public plugin-author surface stays at
``frisket.plugins``; this layer is the first-party authoring machinery behind
it. Import members by submodule path (``frisket.authoring.actions``, ...).
"""

__all__: list[str] = []
