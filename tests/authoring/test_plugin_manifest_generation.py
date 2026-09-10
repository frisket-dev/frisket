"""Declaration-sourced plugin.json generation (backend-only build path).

The bundled ftm/opencorporates/transliterate manifests are GENERATED output:
plugin.py's `Plugin(id=..., version=..., actions=(...))` declaration is the
single source of truth, and the drift guard is regeneration + byte-equality —
these tests execute the generator (never read source as text) and diff its
output against the committed artifacts.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from pydantic import BaseModel

from frisket.plugins.manifest_generate import (
    PluginManifestGenerationError,
    generate_manifest_text,
    manifest_dict_from_plugin,
)
from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult
from frisket.plugins.sdk import Plugin
from helpers import run_cli

ROOT = Path(__file__).resolve().parents[2]
BUNDLED = ROOT / "src" / "frisket" / "authoring" / "bundled_plugins"
GENERATED_BUNDLED_IDS = (
    "frisket.ftm",
    "frisket.opencorporates",
    "frisket.transliterate",
)


_cli = run_cli


class _EchoParams(ActionParams):
    source: ColumnRef[str]


class _EchoOutput(BaseModel):
    echoed: str | None


def _echo(params: _EchoParams, row: Row) -> RowResult[_EchoOutput]:  # pragma: no cover
    return RowResult(output=_EchoOutput(echoed=params.source.read(row)))


def _echo_action():
    return action(
        name="echo",
        title="Echo",
        description="Echo one column.",
        category=ActionCategory.TEXT,
        run=map_rows(_echo),
    )


@pytest.mark.parametrize("plugin_id", GENERATED_BUNDLED_IDS)
def test_bundled_manifest_is_the_byte_exact_regeneration(plugin_id: str) -> None:
    """The committed plugin.json must equal the generator's output byte for
    byte — this replaces hand-sync as the drift guard for bundled plugins."""
    plugin_root = BUNDLED / plugin_id
    generated = generate_manifest_text(plugin_root)
    assert generated is not None, f"{plugin_id} declares no generation source"
    # rule19: two-sources: committed manifest diffed against live regeneration
    committed = (plugin_root / "plugin.json").read_text(encoding="utf-8")
    assert generated == committed, (
        f"{plugin_id}/plugin.json is not the regeneration of its plugin.py "
        "declarations — run `frisket plugin build` on the package and commit "
        "the result"
    )


def test_generation_is_deterministic_across_runs(tmp_path: Path) -> None:
    """Two independent builds (separate processes, fresh module loads) emit
    byte-identical plugin.json."""
    workspace = tmp_path / "acme_backend"
    init = _cli(
        "plugin",
        "init",
        "--id",
        "acme.demo",
        "--output",
        str(workspace),
        "--with",
        "backend",
    )
    assert init.returncode == 0, init.stderr

    first = _cli("plugin", "build", str(workspace))
    assert first.returncode == 0, first.stderr
    first_bytes = (workspace / "plugin.json").read_bytes()

    second = _cli("plugin", "build", str(workspace))
    assert second.returncode == 0, second.stderr
    assert (workspace / "plugin.json").read_bytes() == first_bytes


def test_backend_only_scaffold_emits_python_source_only(tmp_path: Path) -> None:
    workspace = tmp_path / "acme_backend"
    init = _cli(
        "plugin",
        "init",
        "--id",
        "acme.demo",
        "--output",
        str(workspace),
        "--with",
        "backend",
    )
    assert init.returncode == 0, init.stderr
    emitted = sorted(p.name for p in workspace.iterdir())
    assert emitted == ["README.md", "plugin.py"]

    build = _cli("plugin", "build", str(workspace))
    assert build.returncode == 0, build.stderr
    report = json.loads(build.stdout)
    assert report["valid"] is True
    assert report["plugin_id"] == "acme.demo"
    manifest = json.loads((workspace / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["contributes"]["actions"] == ["acme.demo.echo"]
    assert manifest["requires"]["capabilities"] == ["plugin:trusted_local_backend"]

    validate = _cli("plugin", "validate", str(workspace))
    assert validate.returncode == 0, validate.stderr


def test_backend_scaffold_rejects_frontend_feature_combination(
    tmp_path: Path,
) -> None:
    init = _cli(
        "plugin",
        "init",
        "--id",
        "acme.demo",
        "--output",
        str(tmp_path / "ws"),
        "--with",
        "backend",
        "--with",
        "view",
    )
    assert init.returncode == 1
    assert "backend-only Action workspace" in init.stderr


def _copied_bundled_workspace(tmp_path: Path, plugin_id: str) -> Path:
    workspace = tmp_path / plugin_id.replace(".", "_")
    shutil.copytree(BUNDLED / plugin_id, workspace)
    return workspace


def test_mutated_decorator_is_detected_as_drift(tmp_path: Path) -> None:
    """Editing a decorator field without rebuilding fails validate closed —
    the byte-equality check catches title/description/... divergence the old
    handler/scope spot check ignored."""
    workspace = _copied_bundled_workspace(tmp_path, "frisket.transliterate")
    module = workspace / "plugin.py"
    source = module.read_text(encoding="utf-8")
    mutated = source.replace(
        'title="Transliterate to Latin script"',
        'title="Transliterate (renamed)"',
    )
    assert mutated != source
    module.write_text(mutated, encoding="utf-8")

    validate = _cli("plugin", "validate", str(workspace))
    assert validate.returncode == 1
    payload = json.loads(validate.stdout)
    assert payload["code"] == "plugin_manifest_generated_drift"


def test_hand_edited_generated_manifest_is_detected_as_drift(
    tmp_path: Path,
) -> None:
    workspace = _copied_bundled_workspace(tmp_path, "frisket.transliterate")
    manifest_path = workspace / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Edit a field a typed binding is ALLOWED to carry, so the byte-equality
    # drift check is what refuses it. Adding a retired key like `title` would
    # be caught earlier and more bluntly by the manifest contract, which
    # proves a different gate.
    manifest["runtime"]["actions"][0]["catalog_entry"]["title"] = "Edited by hand"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    validate = _cli("plugin", "validate", str(workspace))
    assert validate.returncode == 1
    payload = json.loads(validate.stdout)
    assert payload["code"] == "plugin_manifest_generated_drift"


def test_hand_added_legacy_binding_field_is_rejected_by_the_contract(
    tmp_path: Path,
) -> None:
    """The blunter gate in front of drift: a typed binding may not restate an
    Action's execution metadata at all, so reintroducing one of the retired
    keys fails manifest load rather than reaching the drift comparison."""
    workspace = _copied_bundled_workspace(tmp_path, "frisket.transliterate")
    manifest_path = workspace / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["runtime"]["actions"][0]["title"] = "Edited by hand"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    validate = _cli("plugin", "validate", str(workspace))
    assert validate.returncode == 1
    payload = json.loads(validate.stdout)
    assert payload["code"] == "invalid_plugin_manifest"


def test_rebuild_after_decorator_change_goes_green(tmp_path: Path) -> None:
    """The remedy the drift error names actually works: build regenerates
    the manifest from the mutated decorators and validate passes again."""
    workspace = _copied_bundled_workspace(tmp_path, "frisket.transliterate")
    module = workspace / "plugin.py"
    module.write_text(
        module.read_text(encoding="utf-8").replace(
            'title="Transliterate to Latin script"',
            'title="Transliterate (renamed)"',
        ),
        encoding="utf-8",
    )
    build = _cli("plugin", "build", str(workspace))
    assert build.returncode == 0, build.stderr
    manifest = json.loads((workspace / "plugin.json").read_text(encoding="utf-8"))
    assert (
        manifest["runtime"]["actions"][0]["catalog_entry"]["title"]
        == "Transliterate (renamed)"
    )
    validate = _cli("plugin", "validate", str(workspace))
    assert validate.returncode == 0, validate.stderr


def test_generation_requires_declared_meta() -> None:
    plugin = Plugin(actions=())

    with pytest.raises(PluginManifestGenerationError) as excinfo:
        manifest_dict_from_plugin(plugin)
    assert excinfo.value.code == "plugin_manifest_generation_no_meta"


def test_generation_requires_at_least_one_action() -> None:
    plugin = Plugin(id="acme.demo", version="1.0.0")

    with pytest.raises(PluginManifestGenerationError) as excinfo:
        manifest_dict_from_plugin(plugin)
    assert excinfo.value.code == "plugin_manifest_generation_no_actions"


def test_generation_refuses_non_action_surfaces() -> None:
    """importer/operator/projection contributions still belong to the
    config.mjs build path, and generation says so instead of dropping them."""
    plugin = Plugin(id="acme.demo", version="1.0.0", actions=(_echo_action(),))

    @plugin.projection(kind="acme.demo.projection.map", handler_key="map")
    def project(ctx, params):  # pragma: no cover - never dispatched
        return {}

    with pytest.raises(PluginManifestGenerationError) as excinfo:
        manifest_dict_from_plugin(plugin)
    assert excinfo.value.code == "plugin_manifest_generation_unsupported_surface"


def test_action_ids_are_namespaced_under_the_validated_plugin_id() -> None:
    """Namespacing is enforced at registration, so generation cannot be handed
    an Action that escapes its plugin's namespace in the first place."""
    registered = Plugin(id="acme.demo", version="1.0.0", actions=(_echo_action(),))
    assert [item.action_id for item in registered.actions] == ["acme.demo.echo"]

    with pytest.raises(ValueError, match="validated namespaced plugin ID"):
        Plugin(id="Not A Plugin Id", version="1.0.0", actions=(_echo_action(),))
    with pytest.raises(ValueError, match="duplicate plugin action name"):
        Plugin(
            id="acme.demo",
            version="1.0.0",
            actions=(_echo_action(), _echo_action()),
        )


def test_generation_rereads_edited_source_within_one_process(tmp_path: Path) -> None:
    """`frisket plugin dev` watches the source and rebuilds in a LONG-LIVED
    process. The action loader gives each admitted package ordinary
    sys.modules lifetime keyed by root, which is right for dispatch and wrong
    here: without a content-derived package identity the second build would
    regenerate from the module imported before the author's edit."""
    from frisket.plugins.manifest_generate import generate_manifest_text

    workspace = tmp_path / "acme_reread"
    init = _cli(
        "plugin",
        "init",
        "--id",
        "acme.reread",
        "--output",
        str(workspace),
        "--with",
        "backend",
    )
    assert init.returncode == 0, init.stderr

    module = workspace / "plugin.py"
    first = generate_manifest_text(workspace)
    assert "Echo input column" in first

    module.write_text(
        module.read_text(encoding="utf-8").replace(
            'title="Echo input column"', 'title="Renamed in place"'
        ),
        encoding="utf-8",
    )
    second = generate_manifest_text(workspace)
    assert "Renamed in place" in second, (
        "generation reused the module imported before the edit"
    )
    assert "Echo input column" not in second
