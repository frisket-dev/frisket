"""Typed local plugin manifest loading."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from frisket.actions.core import ActionCategory, action
from frisket.actions.types import (
    ActionParams,
    PluginManifestLoader,
    PluginManifestRecord,
)


class PluginManifestSource(ActionParams):
    kind: Literal["local_file"]
    path: str = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _nonblank_path(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path must be non-empty")
        return value


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
    description="Validate a local plugin package and record durable evidence without activating it.",
    category=ActionCategory.SOURCES,
    run=load_plugin,
    form="plugin_manifest_load",
)
