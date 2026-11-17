"""Typed local plugin manifest loading."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from frisket.actions.core import ActionCategory, action
from frisket.actions.types import (
    ActionParams,
    PluginManifestLoader,
    PluginManifestRecord,
)
from frisket.contracts.actions.schemas._validators import nonblank


class PluginManifestSource(ActionParams):
    kind: Literal["local_file"]
    path: Annotated[str, nonblank("path must be non-empty", strip=False)] = Field(
        min_length=1
    )


class PluginLoadParams(ActionParams):
    manifest: PluginManifestSource


def load_plugin(
    params: PluginLoadParams, manifests: PluginManifestLoader
) -> PluginManifestRecord:
    return manifests.load(params.manifest.path)


LOAD = action(
    examples=(
        PluginLoadParams(
            manifest=PluginManifestSource(
                kind="local_file", path="examples/plugin.json"
            )
        ),
    ),
    name="load",
    title="Load plugin manifest",
    description="Validate a local plugin package and record a receipt without activating it.",
    category=ActionCategory.SOURCES,
    run=load_plugin,
    form="plugin_manifest_load",
)
