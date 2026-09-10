from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from frisket.cli import plugin_cmd as plugin_cli
from frisket.authoring.plugin_build import plugin_build

FRONTEND_PLUGIN_JS = "frontend/plugin.js"
FRONTEND_PLUGIN_TS = "frontend/plugin.ts"


def _init(tmp_path: Path, plugin_id: str, *features: str) -> Path:
    root = tmp_path / plugin_id.replace(".", "_")
    argv = ["init", "--id", plugin_id, "--output", str(root)]
    for feature in features:
        argv += ["--with", feature]
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = plugin_cli(argv)
    assert code == 0, err.getvalue()
    return root


def _build(root: Path) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = plugin_build(root)
    return code, out.getvalue(), err.getvalue()


def _artifact_bytes(root: Path) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for rel in ("plugin.json", "workbench-descriptors.json", FRONTEND_PLUGIN_JS):
        path = root / rel
        snapshot[rel] = path.read_bytes() if path.exists() else None
    return snapshot


# ---------------------------------------------------------------------------
# Plain .js passthrough
# ---------------------------------------------------------------------------


def test_plain_js_passthrough_builds_green_when_exports_match(
    tmp_path: Path,
) -> None:
    root = _init(tmp_path, "demo.exportcheckjs", "view")
    code, out, err = _build(root)
    assert code == 0, err
    manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
    bindings = manifest["runtime"]["workbench_components"]
    assert (
        bindings
        and bindings[0]["component_key"] == "demo.exportcheckjs.components.MainView"
    )


def test_plain_js_passthrough_build_fails_on_renamed_export(tmp_path: Path) -> None:
    plugin_id = "demo.exportcheckjsneg"
    root = _init(tmp_path, plugin_id, "view")
    code, _out, err = _build(root)
    assert code == 0, err

    manifest_snapshot = (root / "plugin.json").read_bytes()
    descriptors_snapshot = (root / "workbench-descriptors.json").read_bytes()

    frontend_path = root / FRONTEND_PLUGIN_JS
    source = frontend_path.read_text(encoding="utf-8")
    assert "export const MainView" in source
    frontend_path.write_text(
        source.replace("export const MainView", "export const MainViewRenamed"),
        encoding="utf-8",
    )

    code, _out, err = _build(root)
    assert code != 0
    # (a) names the missing component_key
    assert f"{plugin_id}.components.MainView" in err
    # (b) lists the exports actually found
    assert "MainViewRenamed" in err
    assert "exports found:" in err

    # Prior artifacts the build unconditionally rewrites (plugin.json,
    # workbench-descriptors.json) are restored byte-identically — the
    # config didn't change, so restore must reproduce it exactly rather
    # than leave a half-written or drifted manifest behind.
    assert (root / "plugin.json").read_bytes() == manifest_snapshot
    assert (root / "workbench-descriptors.json").read_bytes() == descriptors_snapshot
    # frontend/plugin.js IS the author's source in the passthrough path (no
    # separate compile step), so mutating it directly also mutates what the
    # next build call treats as "prior" — the restore path is exercised
    # (nothing further corrupts the file) but there is no separate compiled
    # artifact to roll back to. The compiled-.ts variant below covers that.


def test_plain_js_passthrough_default_export_is_not_a_false_positive(
    tmp_path: Path,
) -> None:
    """review probe: a component_key whose named export was renamed still
    resolves at runtime via a `default` export fallback
    (trustedLocalModule.ts `loadTrustedLocalPluginExport`) — the build check
    must not flag this as missing."""
    root = _init(tmp_path, "demo.exportcheckdefault", "view")
    code, _out, err = _build(root)
    assert code == 0, err

    frontend_path = root / FRONTEND_PLUGIN_JS
    source = frontend_path.read_text(encoding="utf-8")
    source = source.replace("export const MainView", "export const MainViewRenamed")
    source += "\nexport default MainViewRenamed;\n"
    frontend_path.write_text(source, encoding="utf-8")

    code, _out, err = _build(root)
    assert code == 0, err


def test_plain_js_passthrough_import_throw_fails_build_with_node_stderr(
    tmp_path: Path,
) -> None:
    """CAVEAT: import()-ing the module executes it. A module whose import
    throws is a build error, reported with node's stderr — not silently
    treated as "no exports"."""
    root = _init(tmp_path, "demo.exportcheckthrow", "view")
    code, _out, err = _build(root)
    assert code == 0, err

    manifest_snapshot = (root / "plugin.json").read_bytes()
    descriptors_snapshot = (root / "workbench-descriptors.json").read_bytes()

    frontend_path = root / FRONTEND_PLUGIN_JS
    frontend_path.write_text(
        "throw new Error('boom-import-throw');\n"
        + frontend_path.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    code, _out, err = _build(root)
    assert code != 0
    assert "boom-import-throw" in err
    assert (root / "plugin.json").read_bytes() == manifest_snapshot
    assert (root / "workbench-descriptors.json").read_bytes() == descriptors_snapshot


# ---------------------------------------------------------------------------
# Compiled .ts entry (esbuild)
# ---------------------------------------------------------------------------

_TS_MAIN_VIEW = """\
type Ctx = { schemaVersion?: string; contributionId?: string; sheet?: { name?: string } };

export const MainView = ({ React, ctx }: { React: any; ctx?: Ctx }) => {
  return React.createElement(
    'section',
    { 'data-testid': 'plugin-main-view' },
    ctx?.sheet?.name ?? 'No active sheet',
  );
};
"""

_TS_MAIN_VIEW_RENAMED = _TS_MAIN_VIEW.replace(
    "export const MainView", "export const MainViewRenamed"
)


def _swap_to_ts_entry(root: Path, source: str) -> None:
    js_entry = root / FRONTEND_PLUGIN_JS
    js_entry.unlink(missing_ok=True)
    (root / FRONTEND_PLUGIN_TS).write_text(source, encoding="utf-8")


def test_compiled_ts_entry_builds_green_when_exports_match(tmp_path: Path) -> None:
    plugin_id = "demo.exportcheckts"
    root = _init(tmp_path, plugin_id, "view")
    _swap_to_ts_entry(root, _TS_MAIN_VIEW)

    code, _out, err = _build(root)
    assert code == 0, err
    assert (root / FRONTEND_PLUGIN_JS).is_file()
    compiled = (root / FRONTEND_PLUGIN_JS).read_text(encoding="utf-8")
    assert "MainView" in compiled


def test_compiled_ts_entry_build_fails_on_renamed_export_and_restores_bytes(
    tmp_path: Path,
) -> None:
    plugin_id = "demo.exportcheckstneg"
    root = _init(tmp_path, plugin_id, "view")
    _swap_to_ts_entry(root, _TS_MAIN_VIEW)

    code, _out, err = _build(root)
    assert code == 0, err
    good_snapshot = _artifact_bytes(root)
    assert good_snapshot[FRONTEND_PLUGIN_JS] is not None

    # Mutate the SOURCE (not the compiled output) — a real re-compile must
    # run and produce genuinely different output bytes before the export
    # check catches the mismatch.
    (root / FRONTEND_PLUGIN_TS).write_text(_TS_MAIN_VIEW_RENAMED, encoding="utf-8")

    code, _out, err = _build(root)
    assert code != 0
    assert f"{plugin_id}.components.MainView" in err
    assert "MainViewRenamed" in err
    assert "exports found:" in err

    # Prior artifacts (including the freshly-recompiled frontend/plugin.js)
    # restored byte-identically to the last GREEN build's output.
    after = _artifact_bytes(root)
    assert after == good_snapshot


def test_compiled_ts_entry_handles_reexport_shape(tmp_path: Path) -> None:
    """review probe: a re-exported binding (`export { Inner as MainView }
    from './inner'`) is resolved via esbuild bundling + real node import, not
    static source parsing — the emitted module's Object.keys() reflects the
    bundled name regardless of where the implementation actually lives."""
    plugin_id = "demo.exportcheckreexport"
    root = _init(tmp_path, plugin_id, "view")
    js_entry = root / FRONTEND_PLUGIN_JS
    js_entry.unlink(missing_ok=True)
    (root / "frontend" / "inner.ts").write_text(
        "export const Inner = ({ React }: { React: any }) =>"
        " React.createElement('section', {}, 'inner');\n",
        encoding="utf-8",
    )
    (root / FRONTEND_PLUGIN_TS).write_text(
        "export { Inner as MainView } from './inner';\n", encoding="utf-8"
    )

    code, _out, err = _build(root)
    assert code == 0, err


def test_compiled_ts_entry_import_throw_fails_build_with_node_stderr(
    tmp_path: Path,
) -> None:
    plugin_id = "demo.exportcheckstthrow"
    root = _init(tmp_path, plugin_id, "view")
    _swap_to_ts_entry(root, _TS_MAIN_VIEW)
    code, _out, err = _build(root)
    assert code == 0, err
    good_snapshot = _artifact_bytes(root)

    (root / FRONTEND_PLUGIN_TS).write_text(
        "throw new Error('boom-ts-import-throw');\n" + _TS_MAIN_VIEW,
        encoding="utf-8",
    )

    code, _out, err = _build(root)
    assert code != 0
    assert "boom-ts-import-throw" in err
    after = _artifact_bytes(root)
    assert after == good_snapshot
