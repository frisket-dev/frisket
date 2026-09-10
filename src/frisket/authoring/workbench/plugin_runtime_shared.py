"""Shared constants and errors for the workbench plugin runtime boundary."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any


PLUGIN_ACTIVATION_SCHEMA_VERSION = "frisket.workbench_plugin_activation.v1"
PLUGIN_BACKEND_ACTIVATION_SCHEMA_VERSION = (
    "frisket.workbench_plugin_backend_activation.v1"
)
PLUGIN_INSTALL_PLAN_EXECUTION_SCHEMA_VERSION = (
    "frisket.plugin_install_plan_execution.v1"
)
PLUGIN_ENV_SCHEMA_VERSION = "frisket.workbench_plugin_env_vars.v1"
PLUGIN_ENV_VAR_SCHEMA_VERSION = "frisket.workbench_plugin_env_var.v1"
MAX_PLUGIN_LOAD_RECEIPTS_TO_SCAN = 5000
PLUGIN_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
PLUGIN_ENV_INSTALL_STATES = {"installed", "enabled", "disabled"}

# A bundled install source's `value` is a bare package directory name, never
# a path: no '/', no '\\', no leading '.' (which also rules out '..' and
# hidden dirs). This is the primary defense against path traversal for the
# 'bundled' source kind (see _bundled_install_manifest_path).
BUNDLED_PLUGIN_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

_LOGGER = logging.getLogger(__name__)


class WorkbenchPluginActivationError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.field = field
        self.details = details or {}


class WorkbenchPluginLifecycleError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


@dataclass(frozen=True)
class PluginCompositionPolicy:
    """Deployment policy for plugin exposure and execution.

    ``allowed_bundled_ids`` is ``None`` for all reviewed bundles or an exact
    allowlist. ``nonbundled_enabled`` covers trusted-local packages. Inputs are
    frozen so callers cannot mutate an installed policy.
    """

    allowed_bundled_ids: frozenset[str] | None = None
    nonbundled_enabled: bool = True

    def __post_init__(self) -> None:
        if self.allowed_bundled_ids is not None and not isinstance(
            self.allowed_bundled_ids, frozenset
        ):
            object.__setattr__(
                self, "allowed_bundled_ids", frozenset(self.allowed_bundled_ids)
            )

    def permits(self, plugin_id: str, *, is_reviewed_bundled: bool) -> bool:
        """Return whether this package identity may be exposed."""
        if is_reviewed_bundled:
            return (
                self.allowed_bundled_ids is None
                or plugin_id in self.allowed_bundled_ids
            )
        return self.nonbundled_enabled

    @property
    def exposes_any_plugins(self) -> bool:
        """Whether this composition registers plugin routes at all."""
        if self.nonbundled_enabled:
            return True
        if self.allowed_bundled_ids is None:
            return True
        return bool(self.allowed_bundled_ids)


DEFAULT_PLUGIN_COMPOSITION_POLICY = PluginCompositionPolicy()
_INSTALLED_PLUGIN_COMPOSITION_POLICY: PluginCompositionPolicy | None = None
_PLUGIN_COMPOSITION_POLICY_LOCK = threading.Lock()


class PluginCompositionPolicyConflict(RuntimeError):
    """Two composition roots installed different process policies."""

    def __init__(
        self,
        installed: PluginCompositionPolicy,
        attempted: PluginCompositionPolicy,
    ) -> None:
        super().__init__(
            f"plugin composition policy {installed!r} is already installed in "
            f"this process and {attempted!r} tried to replace it; a deployment "
            "installs exactly one plugin composition policy at its composition "
            "root"
        )
        self.installed = installed
        self.attempted = attempted


class UnknownBundledPluginIdsError(ValueError):
    """``allowed_bundled_ids`` names a package absent from this build."""

    def __init__(self, unknown: frozenset[str], shipped: frozenset[str]) -> None:
        super().__init__(
            "plugin composition policy allowed_bundled_ids names id(s) not "
            f"shipped in this build: {sorted(unknown)!r}; shipped bundled "
            f"plugin ids are {sorted(shipped)!r}"
        )
        self.unknown = unknown
        self.shipped = shipped


def install_plugin_composition_policy(policy: PluginCompositionPolicy) -> None:
    """Install one immutable process policy, validating bundled ids at startup."""
    if not isinstance(policy, PluginCompositionPolicy):
        raise TypeError(
            "plugin composition policy must be a PluginCompositionPolicy; got "
            f"{type(policy).__name__}"
        )
    if policy.allowed_bundled_ids is not None:
        from frisket.authoring.workbench.plugin_runtime_status import (
            shipped_bundled_plugin_ids,
        )

        shipped = shipped_bundled_plugin_ids()
        unknown = policy.allowed_bundled_ids - shipped
        if unknown:
            raise UnknownBundledPluginIdsError(frozenset(unknown), shipped)
    global _INSTALLED_PLUGIN_COMPOSITION_POLICY
    with _PLUGIN_COMPOSITION_POLICY_LOCK:
        installed = _INSTALLED_PLUGIN_COMPOSITION_POLICY
        if installed is not None and installed != policy:
            raise PluginCompositionPolicyConflict(installed, policy)
        _INSTALLED_PLUGIN_COMPOSITION_POLICY = policy


def active_plugin_composition_policy() -> PluginCompositionPolicy:
    """THE policy this process exposes plugins under: installed, or permissive."""
    if _INSTALLED_PLUGIN_COMPOSITION_POLICY is None:
        return DEFAULT_PLUGIN_COMPOSITION_POLICY
    return _INSTALLED_PLUGIN_COMPOSITION_POLICY


def _reset_plugin_composition_policy_for_tests() -> None:
    """Test-only reset (the ``_reset_pricing_policy_for_tests`` convention)."""
    global _INSTALLED_PLUGIN_COMPOSITION_POLICY
    with _PLUGIN_COMPOSITION_POLICY_LOCK:
        _INSTALLED_PLUGIN_COMPOSITION_POLICY = None
