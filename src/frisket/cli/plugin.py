"""`frisket plugin ...` — trusted-local plugin author tools.

Commands: init/validate/build/test-backend/dev. `frisket plugin build`
(frisket.authoring.plugin_build) and its watch loop both import
_plugin_validate directly from this module.
"""

import argparse
import json
import sys
from pathlib import Path


def plugin(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="frisket plugin")
    subcommands = parser.add_subparsers(dest="command", required=True)
    init_parser = subcommands.add_parser("init")
    init_parser.add_argument("--id", required=True, dest="plugin_id")
    init_parser.add_argument("--output", required=True)
    init_parser.add_argument(
        "--with",
        dest="features",
        action="append",
        default=[],
        choices=("action", "backend", "view", "panel", "dockTab", "command"),
    )
    validate_parser = subcommands.add_parser("validate")
    validate_parser.add_argument("path")
    build_parser = subcommands.add_parser("build")
    build_parser.add_argument("path")
    test_backend_parser = subcommands.add_parser("test-backend")
    test_backend_parser.add_argument("path")
    test_backend_parser.add_argument(
        "--action",
        required=True,
        dest="action_id",
        help="the Action's namespaced id, or its local name",
    )
    test_backend_parser.add_argument("--input", required=True, dest="input_arg")
    dev_parser = subcommands.add_parser("dev")
    dev_parser.add_argument("path")
    dev_parser.add_argument("--project", required=True, dest="project_id")
    dev_parser.add_argument("--server", default="http://127.0.0.1:8000")
    dev_parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "init":
        return _plugin_init(
            plugin_id=args.plugin_id,
            output=Path(args.output),
            features=set(args.features),
        )
    if args.command == "validate":
        return _plugin_validate(Path(args.path))
    if args.command == "build":
        from frisket.authoring.plugin_build import plugin_build

        return plugin_build(Path(args.path))
    if args.command == "test-backend":
        from frisket.authoring.plugin_test_backend import plugin_test_backend

        return plugin_test_backend(
            Path(args.path),
            action=args.action_id,
            input_arg=args.input_arg,
        )
    if args.command == "dev":
        from frisket.authoring.plugin_dev import plugin_dev

        return plugin_dev(
            Path(args.path),
            project_id=args.project_id,
            server=args.server,
            once=args.once,
        )
    parser.error("unknown plugin command")
    return 2


def _plugin_init(*, plugin_id: str, output: Path, features: set[str]) -> int:
    """Emit a plugin workspace, SOURCE ONLY (plugin-sdk-build-pipeline-v1):
    no plugin.json or workbench-descriptors.json are written — `frisket
    plugin build` produces the artifacts the loader trusts and validates them.

    Two shapes, because Actions and workbench contributions are declared in
    different places:

    - `--with action` (the default; `--with backend` is the same workspace):
      plugin.py is the ONLY source file. Its `Plugin(id=..., version=...,
      actions=(...))` declaration generates plugin.json at build time, so
      there is no plugin.config.mjs and no vendored SDK runtime to emit. An
      Action here is an ordinary Action, admitted to the same native hosts as
      a builtin.
    - `--with view/panel/dockTab/command`: the SDK-shaped workspace, where
      plugin.config.mjs declares the workbench contributions and the frontend
      module implements them.
    """
    from frisket.authoring.plugin_build import vendor_sdk_runtime

    if output.exists() and any(output.iterdir()):
        print(f"error: output directory is not empty: {output}", file=sys.stderr)
        return 1
    features = set(features) or {"action"}
    if features & {"action", "backend"}:
        if features - {"action", "backend"}:
            print(
                "error: --with action is the backend-only Action workspace "
                "and cannot combine with workbench features; declare the "
                "Action in its own package, or add a custom form to it and "
                "build the frontend module alongside plugin.py",
                file=sys.stderr,
            )
            return 1
        output.mkdir(parents=True, exist_ok=True)
        (output / "plugin.py").write_text(
            _plugin_init_backend_python(plugin_id=plugin_id),
            encoding="utf-8",
        )
        (output / "README.md").write_text(
            _plugin_init_backend_readme(plugin_id=plugin_id),
            encoding="utf-8",
        )
        print(f"initialized backend-only plugin workspace at {output}")
        print(
            "next: `frisket plugin build "
            + str(output)
            + "` generates plugin.json from plugin.py and validates it"
        )
        return 0
    output.mkdir(parents=True, exist_ok=True)
    vendor_sdk_runtime(output)
    (output / "plugin.config.mjs").write_text(
        _plugin_init_config(plugin_id=plugin_id, features=features),
        encoding="utf-8",
    )
    if {"view", "panel", "dockTab", "command"} & features:
        (output / "frontend").mkdir(exist_ok=True)
        (output / "frontend" / "plugin.js").write_text(
            _plugin_init_frontend(),
            encoding="utf-8",
        )
    (output / "README.md").write_text(
        _plugin_init_readme(plugin_id=plugin_id),
        encoding="utf-8",
    )
    print(f"initialized SDK plugin workspace at {output}")
    print(
        "next: `frisket plugin build "
        + str(output)
        + "` emits and validates the artifacts"
    )
    return 0


def _plugin_init_config(*, plugin_id: str, features: set[str]) -> str:
    """The SDK-shaped authoring entry point (plugin.config.mjs)."""
    sections: list[str] = []
    if "view" in features:
        sections.append(
            """  views: [
    defineView({
      key: 'main',
      title: 'Plugin Main View',
      component: 'MainView',
      placements: [{ host: 'mainView', mode: 'pane', default: true, order: 100 }],
      requires: [capability('sheet.rows.read')],
      needs: [needs.activeSheet()],
    }),
  ],"""
        )
    panels: list[str] = []
    if "panel" in features:
        panels.append(
            """    definePanel({
      key: 'inspector',
      title: 'Plugin Inspector',
      component: 'InspectorPanel',
      placements: [{ host: 'rightInspector', mode: 'panel', order: 100 }],
      requires: [capability('selection.rows')],
      needs: [needs.activeSheet()],
    }),"""
        )
    if "dockTab" in features:
        panels.append(
            """    definePanel({
      key: 'dock',
      title: 'Plugin Dock Tab',
      shortTitle: 'Dock',
      component: 'DockTab',
      placements: [{ host: 'bottomDock', mode: 'tab', order: 100 }],
      requires: [capability('sheet.active')],
      needs: [needs.activeSheet()],
    }),"""
        )
    if panels:
        sections.append("  panels: [\n" + "\n".join(panels) + "\n  ],")
    if "command" in features:
        sections.append(
            """  commands: [
    defineCommand({
      key: 'hello',
      title: 'Hello Command',
      component: 'HelloCommand',
    }),
  ],"""
        )
    capabilities = "  capabilities: [],"
    body = "\n".join(sections)
    return f"""// SDK-shaped plugin definition — `frisket plugin build .` compiles this
// into plugin.json + workbench-descriptors.json and validates the output.
import {{
  capability,
  defineCommand,
  definePanel,
  definePlugin,
  defineView,
  needs,
}} from './.frisket-sdk/index.mjs';

export default definePlugin({{
  id: '{plugin_id}',
  version: '0.1.0',
{body}
{capabilities}
}});
"""


def _plugin_init_backend_python(*, plugin_id: str) -> str:
    return f'''from __future__ import annotations

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, map_rows
from frisket.actions.types import ActionParams, ColumnRef, Row, RowResult
from frisket.plugins.sdk import Plugin


class EchoParams(ActionParams):
    """What the user picks in the action form."""

    source: ColumnRef[str]


class EchoOutput(BaseModel):
    """One column per field; the host reserves and writes them."""

    plugin_echo: str | None


def echo(params: EchoParams, row: Row) -> RowResult[EchoOutput]:
    value = params.source.read(row)
    return RowResult(
        output=EchoOutput(plugin_echo=None if value is None else value.upper())
    )


ECHO = action(
    name="echo",
    title="Echo input column",
    description="Writes an uppercased copy of one text column.",
    category=ActionCategory.TEXT,
    run=map_rows(echo),
)

# Single source of truth: `frisket plugin build .` generates plugin.json from
# this declaration — plugin.py is the only file you write. The Action runs on
# the same native host a built-in Action does; the plugin id namespaces it.
plugin = Plugin(
    id={plugin_id!r},
    version="0.1.0",
    capabilities=["plugin:trusted_local_backend"],
    actions=(ECHO,),
)
'''


def _plugin_init_backend_readme(*, plugin_id: str) -> str:
    return f"""# {plugin_id}

Backend-only plugin: `plugin.py` is the only file you write. Its
`Plugin(id=..., version=..., actions=(...))` declaration is the single source
of truth — the build generates `plugin.json` from it and validates the output:

```bash
frisket plugin build .
```

`plugin.json` is generated output; never edit it by hand (validate fails
closed on any manifest that is not the byte-exact regeneration of
`plugin.py`'s declarations).

Install and activate through the public workbench lifecycle:

- `POST /api/projects/{{project_id}}/workbench/plugins/{plugin_id}/install-local`
- `POST /api/projects/{{project_id}}/workbench/plugins/{plugin_id}/activate`
- `POST /api/projects/{{project_id}}/workbench/plugins/{plugin_id}/backend/activate`

Or collapse the loop with `frisket plugin dev`:

```bash
frisket plugin dev . --project <project-id> --once   # one cycle
frisket plugin dev . --project <project-id>           # watch and re-run
```

To run one row through an Action directly, without the full host:

```bash
frisket plugin test-backend . --action echo \\
  --input '{{"row": {{"Name": "Alice"}}, "params": {{"source": "Name"}}}}'
```
"""


def _plugin_init_frontend() -> str:
    return """export const MainView = ({ React, ctx }) => {
  return React.createElement('section', {
    'data-testid': 'plugin-main-view',
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-contribution-id': ctx?.contributionId ?? 'missing',
    'data-selected-count': String(ctx?.selection?.selectedCount ?? -1),
    'data-selected-row-ids': (ctx?.selection?.selectedRowIds ?? []).join(','),
  }, ctx?.sheet?.name ?? 'No active sheet');
};

export const InspectorPanel = ({ React, ctx }) => {
  return React.createElement('aside', {
    'data-testid': 'plugin-inspector-panel',
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-contribution-id': ctx?.contributionId ?? 'missing',
  }, String(ctx?.selection?.selectedCount ?? 0));
};

export const DockTab = ({ React, ctx }) => {
  return React.createElement('section', {
    'data-testid': 'plugin-dock-tab',
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-contribution-id': ctx?.contributionId ?? 'missing',
    'data-dock-active': String(ctx?.dock?.isActiveTab ?? false),
  }, ctx?.sheet?.name ?? 'No active sheet');
};

// Palette command handler: invoked with an invocation-scoped context snapshot
// only when the user clicks the command in the palette.
export const HelloCommand = ({ ctx }) => {
  document.body.dataset.pluginCommandRan = ctx?.commandId ?? 'missing';
};
"""


def _plugin_init_readme(*, plugin_id: str) -> str:
    return f"""# {plugin_id}

Edit `plugin.config.mjs` (the SDK-shaped definition) and the sources, then
build — the build emits `plugin.json` + `workbench-descriptors.json`, compiles
the frontend module, and validates its own output:

```bash
frisket plugin build .
```

Install and activate it through the public workbench lifecycle:

- `POST /api/projects/{{project_id}}/workbench/plugins/{plugin_id}/install-local`
- `POST /api/projects/{{project_id}}/workbench/plugins/{plugin_id}/activate`
- `POST /api/projects/{{project_id}}/workbench/plugins/{plugin_id}/backend/activate`
- `POST /api/projects/{{project_id}}/actions/v1/run`

To iterate, use `frisket plugin dev` instead of repeating those calls by
hand — it collapses build -> install-local -> activate (and, for
`plugin:trusted_local_backend`, backend/activate) into one cycle, so every
run reloads the freshly built code with no separate manual reload step, and
can watch the source and re-run on every change:

```bash
frisket plugin dev . --project <project-id> --once   # one cycle
frisket plugin dev . --project <project-id>           # watch and re-run
```

`--server` defaults to `http://127.0.0.1:8000`, matching the default port of
`frisket <workspace>` with no PORT argument; pass `--server http://host:port`
if you're serving elsewhere.

Actions live in their own backend-only workspace (`frisket plugin init --id
<id> --output <dir>`); this workspace declares workbench contributions.
"""


def _plugin_validate(path: Path) -> int:
    from frisket.contracts.plugin import (
        PluginManifestLoadError,
        load_plugin_manifest_file,
    )
    from frisket.plugins.load_evidence import (
        _validate_workbench_descriptor_package_for_manifest,
    )
    from frisket.plugins.frontend_modules import (
        PluginFrontendModuleError,
        validate_frontend_component_modules,
    )
    from frisket.plugins.manifest_drift import (
        PluginManifestDriftError,
        check_action_handlers_against_backend,
    )
    from frisket.plugins.package_identity import (
        PluginPackageIdentityError,
        build_plugin_package_identity,
    )
    from frisket.authoring.workbench.contracts import (
        WorkbenchDescriptorPackageLoadError,
        load_workbench_descriptor_package_file,
    )

    manifest_path = path / "plugin.json" if path.is_dir() else path
    descriptor_path = manifest_path.parent / "workbench-descriptors.json"
    try:
        loaded = load_plugin_manifest_file(manifest_path)
        # Every runtime.actions[] handler_key must resolve to a real
        # Action declaration in the plugin's own backend module.
        # frisket plugin build's self-validate call routes through here too
        # (frisket.plugin_build.plugin_build -> _plugin_validate), so the
        # check fires on every build, not just an explicit `validate`.
        check_action_handlers_against_backend(loaded, manifest_path.parent)
        if descriptor_path.exists():
            descriptor_package = load_workbench_descriptor_package_file(descriptor_path)
            _validate_workbench_descriptor_package_for_manifest(
                loaded,
                descriptor_package,
            )
        frontend_components = validate_frontend_component_modules(
            loaded,
            manifest_path.parent,
        )
        identity = build_plugin_package_identity(
            loaded,
            manifest_path,
            descriptor_path=descriptor_path,
        )
    except (
        PluginManifestLoadError,
        WorkbenchDescriptorPackageLoadError,
        PluginFrontendModuleError,
        PluginManifestDriftError,
        PluginPackageIdentityError,
        ValueError,
    ) as exc:
        message = getattr(exc, "message", str(exc))
        code = getattr(exc, "code", "invalid_plugin_package")
        print(
            json.dumps(
                {"valid": False, "code": code, "message": message},
                sort_keys=True,
            )
        )
        print(f"error: {code}: {message}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "valid": True,
                "plugin_id": loaded.manifest.id,
                "manifest_sha256": loaded.sha256,
                "package_sha256": identity.package_sha256,
                "files": [item.to_ref() for item in identity.files],
                "frontend_components": frontend_components,
            },
            sort_keys=True,
        )
    )
    return 0
