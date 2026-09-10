from __future__ import annotations

import json
from pathlib import Path

import pytest

from frisket.contracts.plugin import PluginManifestContributes
from helpers import CliResult, run_cli

ROOT = Path(__file__).resolve().parents[2]
DEMO_COMMAND_FIXTURE = ROOT / "tests" / "fixtures" / "local_plugins" / "demo_command"


def test_manifest_accepts_workbench_commands() -> None:
    contributes = PluginManifestContributes.model_validate(
        {"workbench_commands": ["demo.plugin.command.hello"]}
    )
    assert contributes.workbench_commands == ["demo.plugin.command.hello"]


def test_manifest_without_workbench_commands_still_validates() -> None:
    contributes = PluginManifestContributes.model_validate(
        {"workbench_panels": ["demo.plugin.panel.main"]}
    )
    assert contributes.workbench_commands == []


def test_workbench_commands_reject_duplicates_and_empties() -> None:
    with pytest.raises(ValueError):
        PluginManifestContributes.model_validate({"workbench_commands": ["a", "a"]})
    with pytest.raises(ValueError):
        PluginManifestContributes.model_validate({"workbench_commands": [" "]})


def _validate(path: Path) -> CliResult:
    return run_cli("plugin", "validate", str(path))


def test_demo_command_fixture_validates() -> None:
    result = _validate(DEMO_COMMAND_FIXTURE)
    assert result.returncode == 0, result.stderr
    validation = json.loads(result.stdout)
    assert validation["valid"] is True
    assert validation["plugin_id"] == "demo.command"


def test_undeclared_command_descriptor_fails_package_validation(
    tmp_path: Path,
) -> None:
    package = tmp_path / "undeclared-command"
    package.mkdir()
    (package / "frontend").mkdir()
    fixture_manifest = json.loads(
        (DEMO_COMMAND_FIXTURE / "plugin.json").read_text(encoding="utf-8")
    )
    # Drop the declaration while keeping the descriptor and binding.
    fixture_manifest["contributes"]["workbench_commands"] = []
    fixture_manifest["contributes"]["workbench_panels"] = ["demo.command.panel.filler"]
    (package / "plugin.json").write_text(json.dumps(fixture_manifest), encoding="utf-8")
    (package / "workbench-descriptors.json").write_text(
        (DEMO_COMMAND_FIXTURE / "workbench-descriptors.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (package / "frontend" / "plugin.js").write_text(
        (DEMO_COMMAND_FIXTURE / "frontend" / "plugin.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    result = _validate(package)
    assert result.returncode != 0
    assert "undeclared" in (result.stdout + result.stderr)


def test_wrong_schema_version_for_declared_command_fails(tmp_path: Path) -> None:
    package = tmp_path / "wrong-schema-command"
    package.mkdir()
    (package / "frontend").mkdir()
    (package / "plugin.json").write_text(
        (DEMO_COMMAND_FIXTURE / "plugin.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    descriptors = json.loads(
        (DEMO_COMMAND_FIXTURE / "workbench-descriptors.json").read_text(
            encoding="utf-8"
        )
    )
    descriptors["descriptors"][0]["schemaVersion"] = "frisket.workbench.panel.v1"
    (package / "workbench-descriptors.json").write_text(
        json.dumps(descriptors), encoding="utf-8"
    )
    (package / "frontend" / "plugin.js").write_text(
        (DEMO_COMMAND_FIXTURE / "frontend" / "plugin.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    result = _validate(package)
    assert result.returncode != 0
    assert "schemaVersion" in (result.stdout + result.stderr)


def test_plugin_init_with_command_output_validates(tmp_path: Path) -> None:
    output = tmp_path / "init-command"
    init = run_cli(
        "plugin",
        "init",
        "--id",
        "demo.initcmd",
        "--output",
        str(output),
        "--with",
        "command",
    )
    assert init.returncode == 0, init.stderr
    # Init emits SDK source only (plugin-sdk-build-pipeline-v1); build emits
    # the artifacts this pin validates.
    build = run_cli("plugin", "build", str(output))
    assert build.returncode == 0, build.stderr
    # The emitted package passes its own validation; inspecting the JSON alone
    # would leave the contract hollow.
    validate = _validate(output)
    assert validate.returncode == 0, validate.stderr
    validation = json.loads(validate.stdout)
    assert validation["valid"] is True
    assert validation["plugin_id"] == "demo.initcmd"
    manifest = json.loads((output / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["contributes"]["workbench_commands"] == [
        "demo.initcmd.command.hello"
    ]
    descriptors = json.loads(
        (output / "workbench-descriptors.json").read_text(encoding="utf-8")
    )
    command = descriptors["descriptors"][0]
    assert command["schemaVersion"] == "frisket.command.v1"
    assert command["commandId"].startswith("demo.initcmd.")
    assert command["placements"][0]["host"] == "commandPalette"
    assert command["placements"][0]["mode"] == "command"
