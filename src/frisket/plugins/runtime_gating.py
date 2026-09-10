from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from frisket.contracts.plugin import PluginManifest


class RuntimeHealthProbe(Protocol):
    """Health seam. The real probe consults the managed-runtime service; tests
    inject a fake with the same shape."""

    def state(self, runtime_name: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ManagedRuntimeDoctorEntry:
    """One auto-derived per-plugin doctor check for a declared managed runtime.
    ``summary`` reports the truthful state (healthy / degraded / missing)."""

    plugin_id: str
    runtime_name: str
    installed: bool
    healthy: bool
    summary: str


def _summarize(runtime_name: str, state: dict[str, Any]) -> str:
    installed = bool(state.get("installed"))
    healthy = bool(state.get("healthy"))
    if healthy:
        return f"managed runtime {runtime_name} installed + healthy"
    if installed:
        return f"managed runtime {runtime_name} installed but unhealthy"
    return f"managed runtime {runtime_name} missing (not installed)"


def managed_runtime_doctor_entries(
    manifest: PluginManifest, *, probe: RuntimeHealthProbe
) -> list[ManagedRuntimeDoctorEntry]:
    """Exactly one doctor check per declared managed runtime, or none for a
    rung-1 manifest. Derived from the declaration alone."""
    declared = getattr(manifest, "managed_runtime", None)
    if declared is None:
        return []
    state = probe.state(declared.name)
    return [
        ManagedRuntimeDoctorEntry(
            plugin_id=manifest.id,
            runtime_name=declared.name,
            installed=bool(state.get("installed")),
            healthy=bool(state.get("healthy")),
            summary=_summarize(declared.name, state),
        )
    ]


def apply_managed_runtime_catalog_gating(
    manifest: PluginManifest,
    actions: list[dict[str, Any]],
    *,
    probe: RuntimeHealthProbe,
) -> None:
    """Disable-with-reason the plugin's actions when its declared managed runtime
    is absent / unhealthy; leave them enabled when it is present. No-op for a
    rung-1 manifest. Mutates each gated action's ``ui_hints`` in place."""
    declared = getattr(manifest, "managed_runtime", None)
    if declared is None:
        return
    state = probe.state(declared.name)
    if bool(state.get("healthy")):
        # Present + healthy: the op stays runnable, no gate added.
        return

    reason = (
        f"managed runtime {declared.name} is not available "
        f"({_summarize(declared.name, state)})"
    )
    gate = {
        "disabled": True,
        "runtime_name": declared.name,
        "reason": reason,
    }
    owned = set(manifest.contributes.actions)
    for entry in actions:
        if entry.get("kind") not in owned:
            continue
        entry["ui_hints"] = {
            **(entry.get("ui_hints") or {}),
            "managed_runtime": gate,
        }
