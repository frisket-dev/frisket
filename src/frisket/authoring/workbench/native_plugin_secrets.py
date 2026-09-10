"""Read-only native access to one installed plugin's configured secrets."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from frisket.authoring.plugin_registry import RuntimeBindingSpec
from frisket.authoring.workbench.plugin_runtime_settings import (
    _normalize_plugin_env_name,
)
from frisket.authoring.workbench.plugin_subprocess import (
    _plugin_scoped_env_encrypted,
    _required_plugin_env_names,
    is_reserved_core_secret_name,
)
from frisket.team.security.secrets import decrypt_secret


def required_plugin_secret_names(binding: RuntimeBindingSpec) -> tuple[str, ...]:
    """The installed package's normalized secret declarations."""

    return tuple(_required_plugin_env_names(binding.metadata))


def missing_native_plugin_secrets(
    project: Any, binding: RuntimeBindingSpec
) -> tuple[str, ...]:
    """Declared names absent from this plugin's project-scoped settings."""

    configured = _plugin_scoped_env_encrypted(project, plugin_id=binding.plugin)
    return tuple(
        name
        for name in required_plugin_secret_names(binding)
        if is_reserved_core_secret_name(name) or name not in configured
    )


@dataclass(frozen=True, slots=True)
class HostPluginSecrets:
    """Invocation snapshot implementing the author-facing PluginSecrets protocol."""

    _declared: tuple[str, ...]
    _values: Mapping[str, str] = field(repr=False)

    @classmethod
    def from_binding(
        cls, project: Any, binding: RuntimeBindingSpec | None
    ) -> HostPluginSecrets:
        if binding is None or binding.handler_api != "plugin_typed_action_native":
            raise TypeError("PluginSecrets requires an installed native action")
        declared = required_plugin_secret_names(binding)
        encrypted = _plugin_scoped_env_encrypted(project, plugin_id=binding.plugin)
        values = {
            name: decrypt_secret(encrypted[name])
            for name in declared
            if not is_reserved_core_secret_name(name) and name in encrypted
        }
        return cls(declared, MappingProxyType(values))

    def require(self, name: str) -> str:
        name = _normalize_plugin_env_name(name)
        if name not in self._declared:
            raise ValueError(f"plugin secret {name!r} is not declared")
        value = self._values.get(name)
        if value is None:
            raise ValueError(f"plugin secret {name!r} is not configured")
        return value
