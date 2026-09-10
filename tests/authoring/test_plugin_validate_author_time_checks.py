from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from frisket.engine.store import Project
from helpers import run_cli as _cli

ROOT = Path(__file__).resolve().parents[2]
SELECTION_SUMMARY_FIXTURE = (
    ROOT / "tests" / "fixtures" / "local_plugins" / "demo_selection_summary"
)


def _init_view_package(tmp_path: Path, plugin_id: str = "demo.authorcheck") -> Path:
    package_dir = tmp_path / plugin_id.replace(".", "_")
    run = _cli(
        "plugin",
        "init",
        "--id",
        plugin_id,
        "--output",
        str(package_dir),
        "--with",
        "view",
    )
    assert run.returncode == 0, run.stderr
    # Init emits SDK-shaped SOURCE only (plugin-sdk-build-pipeline-v1); build
    # produces the plugin.json/workbench-descriptors.json these author-time
    # checks probe.
    build = _cli("plugin", "build", str(package_dir))
    assert build.returncode == 0, build.stderr
    return package_dir


def _validate(package_dir: Path) -> tuple[int, dict[str, Any]]:
    run = _cli("plugin", "validate", str(package_dir))
    payload = json.loads(run.stdout.splitlines()[-1])
    return run.returncode, payload


def _plugin_load_action(manifest_path: Path, *, idempotency_key: str) -> dict[str, Any]:
    return {
        "action_id": "plugin.load",
        "scope": {"kind": "project"},
        "params": {
            "manifest": {"kind": "local_file", "path": str(manifest_path)},
        },
        "idempotency_key": idempotency_key,
    }


def _run_plugin_load(tmp_path: Path, package_dir: Path, *, key: str):
    from frisket.engine.executor import actions as executor_actions

    project_path = tmp_path / f"{key}.frisket"
    project = Project.create(project_path, name=f"Author checks {key}")
    try:
        result = executor_actions.run_action_spec(
            project,
            _plugin_load_action(
                package_dir / "plugin.json",
                idempotency_key=f"plugin_load@sha256:{key}",
            ),
            project_id=f"author-checks-{key}",
        )
        receipt_body: dict[str, Any] | None = None
        if result.receipt_id is not None:
            row = project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()
            if row is not None:
                receipt_body = json.loads(row["body"])
        completed_receipts = int(
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE json_extract(body, '$.status') = 'completed'"
            ).fetchone()[0]
        )
    finally:
        project.close()
    return result, receipt_body, completed_receipts


def test_validate_passes_init_template_and_classifies_module_binding(
    tmp_path: Path,
) -> None:
    package_dir = _init_view_package(tmp_path)
    code, payload = _validate(package_dir)
    assert code == 0, payload
    assert payload["valid"] is True
    assert payload["frontend_components"] == [
        {
            "binding_kind": "module",
            "contribution_id": "demo.authorcheck.view.main",
            "module_path": "frontend/plugin.js",
        }
    ]


def test_validate_classifies_host_component_binding() -> None:
    code, payload = _validate(SELECTION_SUMMARY_FIXTURE)
    assert code == 0, payload
    assert payload["valid"] is True
    kinds = {
        item["contribution_id"]: item["binding_kind"]
        for item in payload["frontend_components"]
    }
    assert kinds["demo.selection_summary.panel.selection_summary"] == "host-component"


def test_validate_fails_on_symlink_escape(tmp_path: Path) -> None:
    package_dir = _init_view_package(tmp_path)
    outside = tmp_path / "outside.js"
    outside.write_text("export const MainView = () => null;\n", encoding="utf-8")
    module = package_dir / "frontend" / "plugin.js"
    module.unlink()
    module.symlink_to(outside)
    code, payload = _validate(package_dir)
    assert code == 1
    assert payload["valid"] is False
    assert payload["code"] == "plugin_frontend_module_path_escape"


def test_validate_fails_on_missing_module_file(tmp_path: Path) -> None:
    package_dir = _init_view_package(tmp_path)
    (package_dir / "frontend" / "plugin.js").unlink()
    code, payload = _validate(package_dir)
    assert code == 1
    assert payload["valid"] is False
    assert payload["code"] == "plugin_frontend_module_file_missing"


def test_validate_fails_on_oversize_module(tmp_path: Path) -> None:
    from frisket.plugins.frontend_modules import (
        MAX_TRUSTED_LOCAL_FRONTEND_MODULE_BYTES,
    )

    package_dir = _init_view_package(tmp_path)
    (package_dir / "frontend" / "plugin.js").write_text(
        "/*" + "a" * MAX_TRUSTED_LOCAL_FRONTEND_MODULE_BYTES + "*/",
        encoding="utf-8",
    )
    code, payload = _validate(package_dir)
    assert code == 1
    assert payload["valid"] is False
    assert payload["code"] == "plugin_frontend_module_too_large"


def test_validate_fails_on_non_utf8_module(tmp_path: Path) -> None:
    package_dir = _init_view_package(tmp_path)
    (package_dir / "frontend" / "plugin.js").write_bytes(b"\xff\xfe\x00broken")
    code, payload = _validate(package_dir)
    assert code == 1
    assert payload["valid"] is False
    assert payload["code"] == "plugin_frontend_module_encoding_invalid"


def test_validate_extension_check_shares_serve_vocabulary(tmp_path: Path) -> None:
    """The manifest schema rejects non-.js/.mjs paths before the helper runs;
    the helper's own check exists for defense in depth and must use the serve
    code. Exercised directly because a real manifest cannot reach it."""
    from frisket.contracts.plugin import load_plugin_manifest_file
    from frisket.plugins.frontend_modules import (
        PluginFrontendModuleError,
        validate_frontend_component_modules,
    )

    package_dir = _init_view_package(tmp_path)
    loaded = load_plugin_manifest_file(package_dir / "plugin.json")
    binding = loaded.manifest.runtime.workbench_components[0]
    object.__setattr__(binding, "module_path", "frontend/plugin.txt")
    (package_dir / "frontend" / "plugin.txt").write_text("x", encoding="utf-8")
    try:
        validate_frontend_component_modules(loaded, package_dir)
    except PluginFrontendModuleError as exc:
        assert exc.code == "plugin_frontend_module_type_invalid"
    else:
        raise AssertionError("expected plugin_frontend_module_type_invalid")


def test_plugin_load_fails_closed_on_broken_module(tmp_path: Path) -> None:
    from frisket.plugins.frontend_modules import (
        MAX_TRUSTED_LOCAL_FRONTEND_MODULE_BYTES,
    )

    package_dir = _init_view_package(tmp_path)
    (package_dir / "frontend" / "plugin.js").write_text(
        "/*" + "a" * MAX_TRUSTED_LOCAL_FRONTEND_MODULE_BYTES + "*/",
        encoding="utf-8",
    )
    result, _receipt, completed = _run_plugin_load(
        tmp_path, package_dir, key="oversize"
    )
    assert result.status == "failed"
    assert result.errors
    assert result.errors[0].code == "plugin_frontend_module_too_large"
    assert completed == 0


def test_plugin_load_fails_closed_on_symlink_escape(tmp_path: Path) -> None:
    package_dir = _init_view_package(tmp_path)
    outside = tmp_path / "outside.js"
    outside.write_text("export const MainView = () => null;\n", encoding="utf-8")
    module = package_dir / "frontend" / "plugin.js"
    module.unlink()
    module.symlink_to(outside)
    result, _receipt, completed = _run_plugin_load(tmp_path, package_dir, key="escape")
    assert result.status == "failed"
    assert result.errors
    assert result.errors[0].code == "plugin_frontend_module_path_escape"
    assert completed == 0


def test_plugin_load_fails_closed_on_non_utf8_module(tmp_path: Path) -> None:
    package_dir = _init_view_package(tmp_path)
    (package_dir / "frontend" / "plugin.js").write_bytes(b"\xff\xfe\x00broken")
    result, _receipt, completed = _run_plugin_load(
        tmp_path, package_dir, key="encoding"
    )
    assert result.status == "failed"
    assert result.errors
    assert result.errors[0].code == "plugin_frontend_module_encoding_invalid"
    assert completed == 0


def test_plugin_load_records_binding_classification(tmp_path: Path) -> None:
    package_dir = _init_view_package(tmp_path)
    result, receipt_body, completed = _run_plugin_load(
        tmp_path, package_dir, key="classify"
    )
    assert result.status == "completed"
    assert completed == 1
    assert receipt_body is not None
    manifest_ref = next(
        item["ref"]
        for item in receipt_body["evidence"]
        if item["ref"]["kind"] == "plugin_manifest"
    )
    assert manifest_ref["frontend_component_bindings"] == [
        {
            "binding_kind": "module",
            "contribution_id": "demo.authorcheck.view.main",
            "module_path": "frontend/plugin.js",
        }
    ]
