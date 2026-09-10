from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from importlib.util import find_spec
from typing import Any

from frisket.contracts.plugin import PluginManifest, PluginManifestContributes

GATING_MODULE = "frisket.plugins.runtime_gating"


# ---------------------------------------------------------------------------
# Defensive accessors (frozen names the implementer must supply).
# ---------------------------------------------------------------------------


def _gating() -> Any:
    assert find_spec(GATING_MODULE) is not None, (
        f"missing {GATING_MODULE}: narrow managed-runtime gating auto-derived "
        "from the manifest `managed_runtime` declaration: one doctor check + "
        "catalog disabled-with-reason, with no bespoke per-operation branch"
    )
    module = importlib.import_module(GATING_MODULE)
    required = [
        "managed_runtime_doctor_entries",
        "apply_managed_runtime_catalog_gating",
    ]
    missing = [name for name in required if not hasattr(module, name)]
    assert not missing, f"{GATING_MODULE} lacks frozen exports: {missing}"
    return module


def _manifest_with_managed_runtime() -> PluginManifest:
    """The lazy author declares ONLY `managed_runtime` (no `requires`, no extra
    gating config) alongside their action contribution."""
    assert "managed_runtime" in PluginManifest.model_fields, (
        "PluginManifest must gain an OPTIONAL top-level `managed_runtime` field "
        "(this exact name; NOT a `runtime` sub-field — `runtime` is reserved for "
        "handler bindings)"
    )
    return PluginManifest(
        id="acme.media",
        version="1.0.0",
        contributes=PluginManifestContributes(actions=["acme.media.download"]),
        managed_runtime={
            "name": "yt-dlp",
            "version": "2025.01.01",
            "artifact_url": "https://example.invalid/yt-dlp/2025.01.01/yt-dlp",
            "checksum_url": "https://example.invalid/yt-dlp/2025.01.01/SHA256SUMS",
            "checksum_entry": "yt-dlp",
        },
    )


def _rung_one_manifest() -> PluginManifest:
    return PluginManifest(
        id="acme.plain",
        version="1.0.0",
        contributes=PluginManifestContributes(actions=["acme.plain.thing"]),
    )


# ---------------------------------------------------------------------------
# Injected runtime-health probe (the gating seam this suite freezes).
# ---------------------------------------------------------------------------


@dataclass
class _FakeRuntimeProbe:
    installed: bool
    healthy: bool
    seen: list[str] = field(default_factory=list)

    def state(self, runtime_name: str) -> dict[str, Any]:
        self.seen.append(runtime_name)
        return {
            "runtime_name": runtime_name,
            "installed": self.installed,
            "healthy": self.installed and self.healthy,
        }


def _actions_payload() -> list[dict[str, Any]]:
    # Shape mirrors the catalog action-catalog entries the hint layer decorates
    # (server/action_catalog_hints.py: entries carry `kind` + `ui_hints`).
    return [{"kind": "acme.media.download", "ui_hints": {}}]


# ---------------------------------------------------------------------------
# 1. The exact manifest field name — `managed_runtime`, optional, distinct.
# ---------------------------------------------------------------------------


def test_manifest_declares_managed_runtime_as_optional_top_level_field():
    assert "managed_runtime" in PluginManifest.model_fields, (
        "PluginManifest must expose an optional `managed_runtime` field"
    )
    # A dedicated contract type carries the declaration.
    assert find_spec("frisket.contracts.plugin") is not None
    contracts = importlib.import_module("frisket.contracts.plugin")
    assert hasattr(contracts, "PluginManifestManagedRuntime"), (
        "the `managed_runtime` field must be a dedicated "
        "PluginManifestManagedRuntime contract, not overloaded onto "
        "PluginManifestRuntime (reserved for handler bindings)"
    )
    # It must be a single-file runtime declaration carried at top level.
    manifest = _manifest_with_managed_runtime()
    assert manifest.managed_runtime is not None
    assert getattr(manifest.managed_runtime, "name", None) == "yt-dlp"


# ---------------------------------------------------------------------------
# 2. A manifest without `managed_runtime` still validates unchanged and is not
#    gated: the declaration is optional and absent by default.
# ---------------------------------------------------------------------------


def test_rung_one_manifest_without_managed_runtime_is_not_gated():
    module = _gating()
    manifest = _rung_one_manifest()
    assert getattr(manifest, "managed_runtime", None) is None

    probe = _FakeRuntimeProbe(installed=False, healthy=False)
    entries = module.managed_runtime_doctor_entries(manifest, probe=probe)
    assert list(entries) == [], (
        "a plugin with no managed runtime gets NO managed-runtime doctor entry"
    )

    actions = _actions_payload()
    module.apply_managed_runtime_catalog_gating(manifest, actions, probe=probe)
    assert actions[0]["ui_hints"] == {}, (
        "a plugin with no managed runtime is never runtime-gated"
    )


# ---------------------------------------------------------------------------
# 3. Auto-derived doctor check: "managed runtime <name> installed + healthy".
# ---------------------------------------------------------------------------


def test_doctor_check_is_auto_derived_from_the_declaration_alone():
    module = _gating()
    manifest = _manifest_with_managed_runtime()

    healthy_probe = _FakeRuntimeProbe(installed=True, healthy=True)
    entries = list(module.managed_runtime_doctor_entries(manifest, probe=healthy_probe))
    assert len(entries) == 1, (
        "exactly one auto-derived doctor check per managed runtime"
    )
    text = repr(entries[0]).lower()
    assert "yt-dlp" in text and ("healthy" in text or "ok" in text or "install" in text)
    assert "yt-dlp" in healthy_probe.seen, "the doctor check must probe the runtime"

    unhealthy_probe = _FakeRuntimeProbe(installed=False, healthy=False)
    down = list(module.managed_runtime_doctor_entries(manifest, probe=unhealthy_probe))
    assert len(down) == 1
    down_text = repr(down[0]).lower()
    assert "yt-dlp" in down_text
    # Degraded is reported truthfully, never a false "ready".
    assert any(
        t in down_text
        for t in ("unhealth", "missing", "absent", "not install", "fail", "down")
    )


# ---------------------------------------------------------------------------
# 4. Auto-derived catalog gate: disabled-with-reason when absent, enabled when
#    present — the lazy-author invariant (declaration ALONE is enough).
# ---------------------------------------------------------------------------


def test_catalog_gate_is_disabled_with_reason_when_runtime_absent():
    module = _gating()
    manifest = _manifest_with_managed_runtime()
    actions = _actions_payload()

    probe = _FakeRuntimeProbe(installed=False, healthy=False)
    module.apply_managed_runtime_catalog_gating(manifest, actions, probe=probe)

    hints = actions[0]["ui_hints"]
    assert hints, "an absent managed runtime must gate the plugin's actions"
    # disabled-with-reason: a machine-readable disabled flag + a human reason
    # that names the runtime (mirrors ui_hints.missing_credentials).
    blob = repr(hints).lower()
    assert "yt-dlp" in blob, "the gate reason must name the missing runtime"
    assert any(
        key in hints
        for key in (
            "disabled",
            "unavailable",
            "disabled_reason",
            "missing_runtime",
            "managed_runtime",
        )
    ), f"catalog gate must expose a disabled-with-reason hint, got {hints!r}"


def test_catalog_gate_enables_action_when_runtime_present():
    module = _gating()
    manifest = _manifest_with_managed_runtime()
    actions = _actions_payload()

    probe = _FakeRuntimeProbe(installed=True, healthy=True)
    module.apply_managed_runtime_catalog_gating(manifest, actions, probe=probe)

    hints = actions[0]["ui_hints"]
    # When the runtime is present + healthy the action is NOT disabled.
    for key in ("disabled", "unavailable", "disabled_reason", "missing_runtime"):
        assert not hints.get(key), (
            f"a present+healthy managed runtime must leave the action enabled; "
            f"unexpected {key!r} in {hints!r}"
        )


def test_lazy_author_gets_gating_for_free_from_declaration_alone():
    """An author who provides only the `managed_runtime` declaration — no
    extra gating config — gets both the doctor check and the
    catalog gate derived automatically."""
    module = _gating()
    manifest = _manifest_with_managed_runtime()
    probe = _FakeRuntimeProbe(installed=False, healthy=False)

    # Both surfaces derive from the same lone declaration, no author gating input.
    entries = list(module.managed_runtime_doctor_entries(manifest, probe=probe))
    actions = _actions_payload()
    module.apply_managed_runtime_catalog_gating(manifest, actions, probe=probe)

    assert len(entries) == 1, "doctor check derived for free"
    assert actions[0]["ui_hints"], "catalog gate derived for free"
