"""V1 plugin manifest contract."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic import model_validator

PLUGIN_MANIFEST_SCHEMA_VERSION = "frisket.plugin.v1"
MAX_PLUGIN_MANIFEST_BYTES = 1_000_000
PLUGIN_ID_SEGMENT = r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?"
PLUGIN_ID_RE = re.compile(rf"^{PLUGIN_ID_SEGMENT}(?:\.{PLUGIN_ID_SEGMENT})*$")

# Plugin ids whose contribution-id namespaces collide with first-party
# descriptors: the bare "frisket" id prefix-matches EVERY frisket.* contribution
# id, and "frisket.core" is the ownerPluginId of first-party workbench
# descriptors. Rejected at manifest load (validate, install-local, serve).
# frisket.geo remains legal — it is the sanctioned first-party runtime plugin.
RESERVED_PLUGIN_IDS = frozenset({"frisket", "frisket.core"})
SEMVER_NUMERIC_IDENTIFIER = r"(?:0|[1-9]\d*)"
SEMVER_PRERELEASE_IDENTIFIER = r"(?:0|[1-9]\d*|[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_BUILD_IDENTIFIER = r"[0-9A-Za-z-]+"
PLUGIN_VERSION_RE = re.compile(
    rf"^{SEMVER_NUMERIC_IDENTIFIER}\.{SEMVER_NUMERIC_IDENTIFIER}\.{SEMVER_NUMERIC_IDENTIFIER}"
    rf"(?:-{SEMVER_PRERELEASE_IDENTIFIER}(?:\.{SEMVER_PRERELEASE_IDENTIFIER})*)?"
    rf"(?:\+{SEMVER_BUILD_IDENTIFIER}(?:\.{SEMVER_BUILD_IDENTIFIER})*)?$"
)


class PluginContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PluginManifestContributes(PluginContractModel):
    workbench_views: list[str] = Field(default_factory=list)
    workbench_panels: list[str] = Field(default_factory=list)
    workbench_commands: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    importers: list[str] = Field(default_factory=list)
    operators: list[str] = Field(default_factory=list)
    projections: list[str] = Field(default_factory=list)
    column_types: list[str] = Field(default_factory=list)
    job_handlers: list[str] = Field(default_factory=list)
    # Declared matcher ids. A matcher is a routing hint, not an executable
    # surface, so it never satisfies the at-least-one-contribution rule on
    # its own; it pairs with a handler action (contributed or core).
    matchers: list[str] = Field(default_factory=list)

    @field_validator(
        "workbench_views",
        "workbench_panels",
        "workbench_commands",
        "actions",
        "importers",
        "operators",
        "projections",
        "column_types",
        "job_handlers",
        "matchers",
    )
    @classmethod
    def _non_empty_unique(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("contribution names must be non-empty strings")
            normalized = value.strip()
            if any(ord(ch) < 32 for ch in normalized):
                raise ValueError(
                    "contribution names must not include control characters"
                )
            if normalized in seen:
                raise ValueError("duplicate contribution name")
            seen.add(normalized)
            cleaned.append(normalized)
        return cleaned

    @model_validator(mode="after")
    def _at_least_one_contribution(self) -> "PluginManifestContributes":
        if not (
            self.workbench_views
            or self.workbench_panels
            or self.workbench_commands
            or self.actions
            or self.importers
            or self.operators
            or self.projections
            or self.column_types
            or self.job_handlers
        ):
            raise ValueError("plugin manifest must declare at least one contribution")
        return self


class PluginManifestRequires(PluginContractModel):
    capabilities: list[str] = Field(default_factory=list)
    secrets: list[str] = Field(default_factory=list)

    @field_validator("capabilities", "secrets")
    @classmethod
    def _non_empty_unique(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("requirement names must be non-empty strings")
            if value in seen:
                raise ValueError("duplicate requirement name")
            seen.add(value)
            cleaned.append(value)
        return cleaned


class PluginManifestRuntimeBindingWrite(PluginContractModel):
    """A declared importer or projection output."""

    name: str = Field(min_length=1)
    type: str = Field(min_length=1)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    schema_ref: str | None = Field(default=None, min_length=1, alias="schema")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("name", "type", "description", "schema_ref")
    @classmethod
    def _clean_write_value(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value != value.strip():
            raise ValueError(
                "runtime binding write values must not include surrounding whitespace"
            )
        if any(ord(ch) < 32 for ch in value):
            raise ValueError(
                "runtime binding write values must not include control characters"
            )
        return value


class PluginManifestRuntimeBinding(PluginContractModel):
    kind: str = Field(min_length=1)
    handler_key: str | None = Field(default=None, min_length=1)
    handler_api: (
        Literal[
            "typed_action",
            "plugin_importer",
            "plugin_operator",
            "plugin_projection",
        ]
        | None
    ) = None
    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, min_length=1, max_length=500)
    module_path: str | None = Field(default=None, min_length=1, max_length=256)
    params: list[dict[str, Any]] = Field(default_factory=list)
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    writes: list[PluginManifestRuntimeBindingWrite] = Field(default_factory=list)
    execution: dict[str, Any] = Field(default_factory=dict)
    catalog_entry: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _typed_action_single_authority(self) -> "PluginManifestRuntimeBinding":
        if self.handler_api == "typed_action":
            if (
                self.catalog_entry is None
                or self.catalog_entry.get("kind") != self.kind
            ):
                raise ValueError("typed action requires its canonical catalog entry")
            if (
                self.params
                or self.inputs
                or self.writes
                or self.execution
                or self.title is not None
                or self.description is not None
            ):
                raise ValueError(
                    "typed action metadata must come from its canonical catalog entry"
                )
            from frisket.contracts.action import ActionCatalogEntry

            ActionCatalogEntry.model_validate(self.catalog_entry)
        elif self.catalog_entry is not None:
            raise ValueError("catalog_entry is only valid for typed actions")
        return self

    @field_validator("writes")
    @classmethod
    def _unique_write_names(
        cls, value: list[PluginManifestRuntimeBindingWrite]
    ) -> list[PluginManifestRuntimeBindingWrite]:
        seen: set[str] = set()
        for item in value:
            if item.name in seen:
                raise ValueError("runtime binding writes must have unique names")
            seen.add(item.name)
        return value

    @field_validator("kind", "handler_key", "title", "description")
    @classmethod
    def _clean_binding_value(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value != value.strip():
            raise ValueError(
                "runtime binding values must not include surrounding whitespace"
            )
        if any(ord(ch) < 32 for ch in value):
            raise ValueError(
                "runtime binding values must not include control characters"
            )
        return value

    @field_validator("module_path")
    @classmethod
    def _clean_runtime_module_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value != value.strip():
            raise ValueError(
                "runtime binding module_path must not include surrounding whitespace"
            )
        if any(ord(ch) < 32 for ch in value):
            raise ValueError(
                "runtime binding module_path must not include control characters"
            )
        if "\\" in value:
            raise ValueError("runtime binding module_path must use '/' separators")
        path = Path(value)
        if path.is_absolute():
            raise ValueError("runtime binding module_path must be relative")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError(
                "runtime binding module_path must not contain empty, '.', or '..' segments"
            )
        if path.suffix != ".py":
            raise ValueError("runtime binding module_path must point to a .py file")
        return value


class PluginManifestWorkbenchComponentBinding(PluginContractModel):
    contribution_id: str = Field(min_length=1)
    module_key: str = Field(min_length=1)
    component_key: str = Field(min_length=1)
    module_path: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("contribution_id", "module_key", "component_key")
    @classmethod
    def _clean_component_binding_value(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError(
                "workbench component binding values must not include surrounding whitespace"
            )
        if any(ord(ch) < 32 for ch in value):
            raise ValueError(
                "workbench component binding values must not include control characters"
            )
        return value

    @field_validator("module_path")
    @classmethod
    def _clean_component_module_path(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value != value.strip():
            raise ValueError(
                "workbench component module_path must not include surrounding whitespace"
            )
        if any(ord(ch) < 32 for ch in value):
            raise ValueError(
                "workbench component module_path must not include control characters"
            )
        if "\\" in value:
            raise ValueError("workbench component module_path must use '/' separators")
        path = Path(value)
        if path.is_absolute():
            raise ValueError("workbench component module_path must be relative")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError(
                "workbench component module_path must not contain empty, '.', or '..' segments"
            )
        if path.suffix not in {".js", ".mjs"}:
            raise ValueError(
                "workbench component module_path must point to a .js or .mjs file"
            )
        return value


# First-party matchers reserve priority >= this ceiling; a plugin matcher
# declaring priority >= ceiling is refused at manifest load. Kept in sync
# with frisket.url_classification.matchers.PLUGIN_PRIORITY_CEILING (asserted
# by tests/test_plugin_url_matchers.py); duplicated here to keep the
# contracts layer free of a url_classification import at module load.
PLUGIN_MATCHER_PRIORITY_CEILING = 1000
# Anchored plugin path_regex cap; mirrors
# frisket.url_classification.matchers.MAX_REGEX_PATTERN_LENGTH.
MAX_PLUGIN_MATCHER_REGEX_LENGTH = 512
MAX_PLUGIN_MATCHER_REGEX_QUANTIFIERS = 16
# Core action kinds a plugin matcher's handler_action_kind MAY reference in
# addition to the plugin's own contributed actions. The collection-expand
# child-sheet action and the SSRF-guarded page fetch are the two permitted
# explicitly.
_CORE_MATCHER_HANDLER_KINDS = frozenset(
    {
        "derive.collection_expand",
        "media.fetch_url",
        "media.ytdlp_download",
    }
)
_MATCHER_KINDS = frozenset({"media", "collection", "scraper", "page"})
# eTLD+1-ish registered domain shape. Deliberately loose (host validation is
# not the security boundary — parsed-parts matching is); this only rejects
# obvious junk (schemes, paths, whitespace) in a declared domain.
_MATCHER_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9-]+)+$")


class PluginManifestMatcher(PluginContractModel):
    """A plugin-contributed URL matcher.

    The host evaluates these predicates host-side against a normalized URL (cheap
    string work, no plugin code); the declared ``handler_action_kind`` runs later
    on the native action host with its declared capabilities. Registered-domain
    match is the primary key; path/query predicates and the sandboxed
    ``path_regex`` refine it.
    """

    matcher_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    domains: list[str] = Field(min_length=1)
    kind: Literal["media", "collection", "scraper", "page"]
    path_prefix: list[str] = Field(default_factory=list)
    query_requires: list[str] = Field(default_factory=list)
    query_forbids: list[str] = Field(default_factory=list)
    path_regex: str | None = None
    handler_action_kind: str = Field(min_length=1)
    params_hints: dict[str, Any] = Field(default_factory=dict)
    expansion: dict[str, Any] | None = None
    priority: int = 0

    @field_validator("matcher_id", "provider", "handler_action_kind")
    @classmethod
    def _clean_matcher_value(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("matcher values must not include surrounding whitespace")
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("matcher values must not include control characters")
        return value

    @field_validator("matcher_id")
    @classmethod
    def _matcher_id_not_first_party(cls, value: str) -> str:
        # The firstparty.* namespace is reserved so a plugin cannot register
        # a matcher id that shadows a first-party matcher.
        if value.startswith("firstparty."):
            raise ValueError(
                "plugin matcher_id must not use the reserved 'firstparty.' namespace"
            )
        return value

    @field_validator("domains")
    @classmethod
    def _clean_domains(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("matcher domains must be non-empty strings")
            normalized = value.strip().lower().rstrip(".")
            if not _MATCHER_DOMAIN_RE.fullmatch(normalized):
                raise ValueError(
                    f"matcher domain {value!r} is not a bare registered-domain host"
                )
            if normalized in seen:
                raise ValueError("duplicate matcher domain")
            seen.add(normalized)
            cleaned.append(normalized)
        return cleaned

    @field_validator("priority")
    @classmethod
    def _priority_below_first_party_ceiling(cls, value: int) -> int:
        if value >= PLUGIN_MATCHER_PRIORITY_CEILING:
            raise ValueError(
                "plugin matcher priority must be strictly below the first-party "
                f"reserved ceiling ({PLUGIN_MATCHER_PRIORITY_CEILING})"
            )
        return value

    @field_validator("path_regex")
    @classmethod
    def _validate_regex_caps(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if len(value) > MAX_PLUGIN_MATCHER_REGEX_LENGTH:
            raise ValueError(
                "matcher path_regex exceeds the "
                f"{MAX_PLUGIN_MATCHER_REGEX_LENGTH}-char cap"
            )
        quantifiers = sum(value.count(ch) for ch in "*+")
        if quantifiers > MAX_PLUGIN_MATCHER_REGEX_QUANTIFIERS:
            raise ValueError(
                "matcher path_regex has too many quantifiers "
                f"(> {MAX_PLUGIN_MATCHER_REGEX_QUANTIFIERS})"
            )
        try:
            re.compile(r"\A(?:" + value + r")\Z")
        except re.error as exc:
            raise ValueError(f"matcher path_regex does not compile: {exc}") from exc
        return value

    @model_validator(mode="after")
    def _validate_kind_expansion_pairing(self) -> "PluginManifestMatcher":
        if self.kind == "collection":
            expansion = self.expansion
            if not isinstance(expansion, dict):
                raise ValueError(
                    "collection matcher requires an expansion block "
                    "(unit/enumerator/default_cap/hard_cap)"
                )
            enumerator = expansion.get("enumerator")
            if not isinstance(enumerator, str) or not enumerator.strip():
                raise ValueError(
                    "collection matcher expansion requires a non-empty enumerator"
                )
            for cap_key in ("default_cap", "hard_cap"):
                cap = expansion.get(cap_key)
                if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
                    raise ValueError(
                        f"collection matcher expansion {cap_key} must be a positive int"
                    )
        elif self.expansion is not None:
            raise ValueError("expansion is only valid on a collection-kind matcher")
        return self


class PluginManifestRuntime(PluginContractModel):
    actions: list[PluginManifestRuntimeBinding] = Field(default_factory=list)
    importers: list[PluginManifestRuntimeBinding] = Field(default_factory=list)
    operators: list[PluginManifestRuntimeBinding] = Field(default_factory=list)
    projections: list[PluginManifestRuntimeBinding] = Field(default_factory=list)
    job_handlers: list[PluginManifestRuntimeBinding] = Field(default_factory=list)
    matchers: list[PluginManifestMatcher] = Field(default_factory=list)
    workbench_components: list[PluginManifestWorkbenchComponentBinding] = Field(
        default_factory=list
    )

    @field_validator("matchers")
    @classmethod
    def _unique_matcher_ids(
        cls, values: list[PluginManifestMatcher]
    ) -> list[PluginManifestMatcher]:
        seen: set[str] = set()
        for value in values:
            if value.matcher_id in seen:
                raise ValueError("duplicate runtime matcher_id")
            seen.add(value.matcher_id)
        return values

    @field_validator("actions", "importers", "operators", "projections", "job_handlers")
    @classmethod
    def _unique_runtime_bindings(
        cls, values: list[PluginManifestRuntimeBinding]
    ) -> list[PluginManifestRuntimeBinding]:
        seen_kinds: set[str] = set()
        seen_handler_keys: set[str] = set()
        for value in values:
            if value.kind in seen_kinds:
                raise ValueError("duplicate runtime binding kind")
            if value.handler_key is not None and value.handler_key in seen_handler_keys:
                raise ValueError("duplicate runtime binding handler_key")
            seen_kinds.add(value.kind)
            if value.handler_key is not None:
                seen_handler_keys.add(value.handler_key)
        return values

    @field_validator("workbench_components")
    @classmethod
    def _unique_workbench_components(
        cls, values: list[PluginManifestWorkbenchComponentBinding]
    ) -> list[PluginManifestWorkbenchComponentBinding]:
        seen_contributions: set[str] = set()
        for value in values:
            if value.contribution_id in seen_contributions:
                raise ValueError("duplicate workbench component contribution_id")
            seen_contributions.add(value.contribution_id)
        return values


class PluginManifestSetting(PluginContractModel):
    id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=160)
    type: Literal["boolean", "string", "number", "enum"]
    default: Any = None
    enum: list[str] | None = None
    min: float | None = None
    max: float | None = None
    description: str | None = Field(default=None, max_length=500)

    @field_validator("id", "title", "description")
    @classmethod
    def _clean_setting_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value != value.strip():
            raise ValueError(
                "plugin setting text must not include surrounding whitespace"
            )
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("plugin setting text must not include control characters")
        return value

    @field_validator("enum")
    @classmethod
    def _clean_enum(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return values
        if not values:
            raise ValueError("enum settings must provide at least one option")
        seen: set[str] = set()
        cleaned: list[str] = []
        for value in values:
            if value != value.strip() or any(ord(ch) < 32 for ch in value):
                raise ValueError("enum options must be clean strings")
            if value in seen:
                raise ValueError("duplicate enum option")
            seen.add(value)
            cleaned.append(value)
        return cleaned

    @model_validator(mode="after")
    def _validate_default(self) -> "PluginManifestSetting":
        if self.type == "boolean" and not isinstance(self.default, bool):
            raise ValueError("boolean setting default must be a boolean")
        if self.type == "string" and not isinstance(self.default, str):
            raise ValueError("string setting default must be a string")
        if self.type == "number" and not isinstance(self.default, (int, float)):
            raise ValueError("number setting default must be numeric")
        if self.type == "enum":
            if self.enum is None:
                raise ValueError("enum setting must declare enum options")
            if self.default not in self.enum:
                raise ValueError("enum setting default must be one of the options")
        if self.type != "enum" and self.enum is not None:
            raise ValueError("only enum settings may declare enum options")
        if self.type != "number" and (self.min is not None or self.max is not None):
            raise ValueError("only number settings may declare min/max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("setting min must not exceed max")
        return self


class PluginManifestManagedRuntime(PluginContractModel):
    """A declared SINGLE-FILE executable managed runtime
    (plugin-managed-runtime-service-v1, program doc §11). Core fetches /
    checksum-verifies / caches / versions the artifact; from this declaration
    ALONE the runtime gets a doctor check + catalog disabled-with-reason gating
    for free (frisket.plugins.runtime_gating). This is a dedicated contract, NOT
    overloaded onto PluginManifestRuntime — `runtime` stays reserved for handler
    bindings."""

    name: str = Field(min_length=1, max_length=160)
    version: str = Field(min_length=1, max_length=160)
    artifact_url: str = Field(min_length=1)
    checksum_url: str = Field(min_length=1)
    checksum_entry: str = Field(min_length=1, max_length=256)


class PluginManifest(PluginContractModel):
    schema_version: Literal[PLUGIN_MANIFEST_SCHEMA_VERSION] = (
        PLUGIN_MANIFEST_SCHEMA_VERSION
    )
    id: str
    version: str
    contributes: PluginManifestContributes
    requires: PluginManifestRequires = Field(default_factory=PluginManifestRequires)
    runtime: PluginManifestRuntime = Field(default_factory=PluginManifestRuntime)
    settings: list[PluginManifestSetting] = Field(default_factory=list)
    # Optional and absent by default so a rung-1 manifest-only plugin is
    # unaffected.
    managed_runtime: PluginManifestManagedRuntime | None = None
    # A bundled plugin some operators do not want should be shippable
    # DISABLED — "a way to most likely disable it (or just never enable
    # it)". Bootstrap seeds every packaged bundled plugin and auto-enables it
    # for out-of-box parity (plugin_runtime.bootstrap_project_bundled_plugins).
    # A manifest MAY opt out of that auto-enable by declaring
    # ``auto_enable: false``; the plugin is still installed+loaded into the
    # project (so it is discoverable and one lifecycle activation away) but
    # rests un-enabled until an operator turns it on. Default True keeps a
    # bundled manifest that says nothing (e.g. frisket.geo) auto-enabled with
    # no manifest edit.
    auto_enable: bool = True

    @field_validator("id")
    @classmethod
    def _canonical_plugin_id(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError(
                "plugin id must not include leading or trailing whitespace"
            )
        if not PLUGIN_ID_RE.fullmatch(value):
            raise ValueError(
                "plugin id must use dot-separated segments that start and end with "
                "lowercase ASCII letters or digits and may contain '_' or '-' internally"
            )
        if value in RESERVED_PLUGIN_IDS:
            raise ValueError(
                f"plugin id {value!r} is reserved: it collides with the first-party "
                "contribution namespace (frisket.core.*); pick your own namespace"
            )
        return value

    @model_validator(mode="after")
    def _validate_component_binding_namespace(self) -> "PluginManifest":
        # A frontend module binding always serves the plugin's OWN code: its
        # contribution_id must live in the plugin's namespace. (Manifest
        # `contributes` lists MAY reference core surface ids — the
        # cross-surface participation pattern, e.g. frisket.geo declaring
        # frisket.core.panel.projection_status — but bindings never can.)
        prefix = f"{self.id}."
        for binding in self.runtime.workbench_components:
            if not binding.contribution_id.startswith(prefix):
                raise ValueError(
                    "workbench component binding contribution_id "
                    f"{binding.contribution_id!r} must be namespaced under the "
                    f"plugin id {self.id!r}"
                )
        return self

    @model_validator(mode="after")
    def _validate_matcher_contributions(self) -> "PluginManifest":
        # Every runtime matcher must be declared in contributes.matchers, and
        # its handler_action_kind must be one the plugin contributes OR a
        # permitted core kind (the cross-surface allowance). Matcher
        # predicate/trust caps are enforced on PluginManifestMatcher itself.
        declared = set(self.contributes.matchers)
        contributed_actions = set(self.contributes.actions)
        for matcher in self.runtime.matchers:
            if matcher.matcher_id not in declared:
                raise ValueError(
                    f"runtime matcher {matcher.matcher_id!r} is not declared in "
                    "contributes.matchers"
                )
            handler = matcher.handler_action_kind
            if (
                handler not in contributed_actions
                and handler not in _CORE_MATCHER_HANDLER_KINDS
            ):
                raise ValueError(
                    f"matcher handler_action_kind {handler!r} must be a contributed "
                    "action or a permitted core kind "
                    f"({sorted(_CORE_MATCHER_HANDLER_KINDS)})"
                )
        return self

    @model_validator(mode="after")
    def _validate_settings_namespace(self) -> "PluginManifest":
        seen: set[str] = set()
        prefix = f"{self.id}."
        for setting in self.settings:
            if setting.id in seen:
                raise ValueError("duplicate plugin setting id")
            if not setting.id.startswith(prefix):
                raise ValueError("plugin setting ids must be namespaced by plugin id")
            seen.add(setting.id)
        return self

    @field_validator("version")
    @classmethod
    def _semver_like_version(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError(
                "plugin version must not include leading or trailing whitespace"
            )
        if not PLUGIN_VERSION_RE.fullmatch(value):
            raise ValueError(
                "plugin version must be SemVer 2.0.0 "
                "(MAJOR.MINOR.PATCH with optional -prerelease/+build)"
            )
        return value

    @model_validator(mode="after")
    def _derive_omitted_runtime_handler_keys(self) -> "PluginManifest":
        for bindings in _runtime_binding_groups(self.runtime):
            seen_handler_keys: set[str] = set()
            for binding in bindings:
                if binding.handler_key is None:
                    binding.handler_key = _derive_runtime_handler_key(
                        plugin_id=self.id,
                        kind=binding.kind,
                    )
                if binding.handler_key in seen_handler_keys:
                    raise ValueError("duplicate runtime binding handler_key")
                seen_handler_keys.add(binding.handler_key)
        return self


def _runtime_binding_groups(
    runtime: PluginManifestRuntime,
) -> tuple[list[PluginManifestRuntimeBinding], ...]:
    return (
        runtime.actions,
        runtime.importers,
        runtime.operators,
        runtime.projections,
        runtime.job_handlers,
    )


def _derive_runtime_handler_key(*, plugin_id: str, kind: str) -> str:
    local = kind
    plugin_prefix = f"{plugin_id}."
    if local.startswith(plugin_prefix):
        local = local[len(plugin_prefix) :]
    for prefix in ("op.", "importer.", "operator.", "projection.", "job_handler."):
        if local.startswith(prefix):
            local = local[len(prefix) :]
            break
    else:
        for marker in (
            ".op.",
            ".importer.",
            ".operator.",
            ".projection.",
            ".job_handler.",
        ):
            if marker in local:
                local = local.rsplit(marker, 1)[1]
                break
        else:
            if "." in local:
                local = local.rsplit(".", 1)[1]
    if not local:
        raise ValueError("runtime binding handler_key could not be derived from kind")
    return f"{plugin_id}:{local}"


@dataclass(frozen=True)
class LoadedPluginManifest:
    manifest: PluginManifest
    sha256: str
    byte_count: int


class PluginManifestLoadError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def load_plugin_manifest_file(path: Path) -> LoadedPluginManifest:
    try:
        if not path.is_file():
            raise PluginManifestLoadError(
                "invalid_plugin_manifest_source",
                "plugin manifest path must point to a readable file",
            )
        if path.stat().st_size > MAX_PLUGIN_MANIFEST_BYTES:
            raise PluginManifestLoadError(
                "invalid_plugin_manifest_source",
                "plugin manifest exceeds the v1 validation limit",
            )
        data = path.read_bytes()
        if len(data) > MAX_PLUGIN_MANIFEST_BYTES:
            raise PluginManifestLoadError(
                "invalid_plugin_manifest_source",
                "plugin manifest exceeds the v1 validation limit",
            )
    except OSError as exc:
        raise PluginManifestLoadError(
            "invalid_plugin_manifest_source", str(exc)
        ) from exc
    try:
        payload: Any = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginManifestLoadError(
            "invalid_plugin_manifest", f"plugin manifest is not valid JSON: {exc}"
        ) from exc
    try:
        manifest = PluginManifest.model_validate(payload)
    except ValidationError as exc:
        first_error = exc.errors(include_url=False)[0]
        loc = first_error.get("loc", ())
        location = ".".join(str(part) for part in loc)
        reason = str(first_error.get("msg", "validation error"))
        detail = f"{location}: {reason}" if location else reason
        # A bad `writes` item gets its own code so install/plugin.load
        # callers can distinguish "this plugin's declared output columns are
        # malformed" from the general manifest-shape catch-all.
        if "writes" in loc:
            code = "plugin_manifest_writes_invalid"
        else:
            code = "invalid_plugin_manifest"
        raise PluginManifestLoadError(
            code,
            f"plugin manifest does not match frisket.plugin.v1 ({detail})",
        ) from exc
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    return LoadedPluginManifest(
        manifest=manifest,
        sha256=digest,
        byte_count=len(data),
    )
