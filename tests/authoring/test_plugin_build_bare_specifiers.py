from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from frisket.cli import plugin_cmd as plugin_cli
from frisket.authoring.plugin_build import (
    _is_bare_specifier,
    _is_runtime_only_scheme_specifier,
    plugin_build,
)

FRONTEND_PLUGIN_JS = "frontend/plugin.js"
FRONTEND_PLUGIN_TS = "frontend/plugin.ts"

# A real, node- AND esbuild-resolvable CommonJS package stub. CJS (not
# `"type": "module"`) on purpose: it proves the rejection is about the
# EMITTED specifier text, not about whether the package itself is
# ESM-shaped or import()-able.
_STUB_PACKAGE_JSON = '{"name": "left-pad", "version": "1.0.0", "main": "index.js"}\n'
_STUB_PACKAGE_MARKER = "STUB_LEFT_PAD_BODY"
_STUB_PACKAGE_INDEX = (
    "module.exports = function leftPad(str) {\n"
    f'  return "{_STUB_PACKAGE_MARKER}" + str;\n'
    "};\n"
)


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


def _write_stub_left_pad(root: Path) -> None:
    """A node_modules/left-pad package node COULD resolve — proving the
    build's rejection does not depend on node-resolvability."""
    pkg_dir = root / "node_modules" / "left-pad"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "package.json").write_text(_STUB_PACKAGE_JSON, encoding="utf-8")
    (pkg_dir / "index.js").write_text(_STUB_PACKAGE_INDEX, encoding="utf-8")


# ---------------------------------------------------------------------------
# (a) plain-.js entry, bare npm specifier -> build fails
# ---------------------------------------------------------------------------


def test_plain_js_entry_bare_npm_import_fails_build_naming_specifier(
    tmp_path: Path,
) -> None:
    plugin_id = "demo.barejs"
    root = _init(tmp_path, plugin_id, "view")
    _write_stub_left_pad(root)

    frontend_path = root / FRONTEND_PLUGIN_JS
    source = frontend_path.read_text(encoding="utf-8")
    frontend_path.write_text(
        "import leftPad from 'left-pad';\n" + source, encoding="utf-8"
    )

    # Sanity: node itself CAN resolve this import (the stub is real and
    # node-resolvable) — the build must still reject it. If this assertion
    # ever fails, the fixture stopped proving what it claims to prove.
    import subprocess
    import sys as _sys

    node_check = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            "import('file://' + process.argv[1]).then(() => process.exit(0),"
            " () => process.exit(1))",
            str(frontend_path),
        ],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert node_check.returncode == 0, (
        "fixture setup is broken: node could not resolve the stub left-pad "
        f"package: {node_check.stderr.decode(errors='replace')}"
    )
    del _sys

    code, _out, err = _build(root)
    assert code != 0
    assert "left-pad" in err
    assert "plugin.ts" in err, err


def test_plain_js_entry_relative_import_still_builds(tmp_path: Path) -> None:
    plugin_id = "demo.barejsrelative"
    root = _init(tmp_path, plugin_id, "view")

    (root / "frontend" / "helper.js").write_text(
        "export const helperValue = 42;\n", encoding="utf-8"
    )
    frontend_path = root / FRONTEND_PLUGIN_JS
    source = frontend_path.read_text(encoding="utf-8")
    frontend_path.write_text(
        "import { helperValue } from './helper.js';\n"
        f"globalThis.__pluginHelperValue = helperValue;\n{source}",
        encoding="utf-8",
    )

    code, _out, err = _build(root)
    assert code == 0, err


# ---------------------------------------------------------------------------
# (b) scheme'd specifiers: server-runtime builtins rejected, genuine URLs
# unaffected (re-review B1, plugin-build-bare-specifier-rejection-v1)
# ---------------------------------------------------------------------------


def test_plain_js_entry_node_scheme_import_fails_build_as_runtime_builtin(
    tmp_path: Path,
) -> None:
    """`node:fs` is scheme'd, so it passes `_is_bare_specifier`'s URL test —
    but it is not a resource the browser's plain `import()` can fetch, and
    node's own module resolution (the export check below) happily imports
    it, which is exactly the false-green shape this task exists to kill,
    just narrowed to server-runtime builtins. No node_modules stub needed:
    `node:fs` is a real node builtin, so this also proves the rejection is
    not merely "unresolvable", it is a deliberate scheme blocklist."""
    plugin_id = "demo.barejsnodescheme"
    root = _init(tmp_path, plugin_id, "view")

    frontend_path = root / FRONTEND_PLUGIN_JS
    source = frontend_path.read_text(encoding="utf-8")
    frontend_path.write_text("import fs from 'node:fs';\n" + source, encoding="utf-8")

    code, _out, err = _build(root)
    assert code != 0
    assert "node:fs" in err
    assert "server-runtime builtin" in err, err
    assert "browser cannot load" in err, err


def test_https_url_specifier_classification_is_unchanged(tmp_path: Path) -> None:
    """A genuine `https:` URL import must keep classifying as legal — the
    new server-runtime-scheme blocklist targets `node:`/`bun:`/`deno:`
    only, not URLs the browser's dynamic `import()` can really fetch (the packaging rollout
    in the bare-specifier re-review). Exercised directly against the
    classification functions, not a full build: a static `https:` import
    already fails downstream at the pre-existing export check for
    unrelated reasons (node cannot load `https:` modules — filed as F8 on
    plugin-authoring-docs-v2), which this fix must not change."""
    del tmp_path
    specifier = "https://esm.sh/left-pad@1.3.0"
    assert _is_bare_specifier(specifier) is False
    assert _is_runtime_only_scheme_specifier(specifier) is False


def test_bun_and_deno_scheme_specifiers_are_classified_as_runtime_only() -> None:
    for specifier in ("bun:sqlite", "deno:runtime"):
        assert _is_bare_specifier(specifier) is False, specifier
        assert _is_runtime_only_scheme_specifier(specifier) is True, specifier


# ---------------------------------------------------------------------------
# (c) compiled .ts entry, npm import -> builds green, dependency inlined
# ---------------------------------------------------------------------------

_TS_WITH_NPM_IMPORT = """\
import leftPad from 'left-pad';

export const MainView = ({ React }: { React: any }) => {
  return React.createElement(
    'section',
    { 'data-testid': 'plugin-main-view' },
    leftPad('hi'),
  );
};
"""


def test_compiled_ts_entry_npm_import_builds_green_and_inlines_dependency(
    tmp_path: Path,
) -> None:
    plugin_id = "demo.barets"
    root = _init(tmp_path, plugin_id, "view")
    _write_stub_left_pad(root)

    (root / FRONTEND_PLUGIN_JS).unlink(missing_ok=True)
    (root / FRONTEND_PLUGIN_TS).write_text(_TS_WITH_NPM_IMPORT, encoding="utf-8")

    code, _out, err = _build(root)
    assert code == 0, err

    compiled = (root / FRONTEND_PLUGIN_JS).read_text(encoding="utf-8")
    # The dependency's actual body is present (real inlining, not a stub).
    assert _STUB_PACKAGE_MARKER in compiled
    # No bare specifier for it survives in the emitted module.
    assert "from 'left-pad'" not in compiled
    assert 'from "left-pad"' not in compiled


# ---------------------------------------------------------------------------
# (d) a failed bare-specifier build restores prior artifacts
# ---------------------------------------------------------------------------


def test_failed_bare_specifier_build_restores_prior_artifacts(tmp_path: Path) -> None:
    """A bare specifier can only survive into the EMITTED module via the
    plain-.js passthrough — a compiled .ts/.tsx/.jsx entry either inlines a
    resolvable package (test c) or fails at the esbuild compile step for an
    unresolvable one (a different, already-covered restore path), so this is
    the vehicle that actually exercises the new check's own restore.

    frontend/plugin.js IS the author's source in the passthrough path (no
    separate compile step), so mutating it directly also mutates what the
    next build call treats as "prior" — same caveat as
    test_plugin_build_export_check.py's passthrough restore test. What IS
    meaningfully exercised: plugin.json/workbench-descriptors.json are
    unconditionally rewritten every build call (before the frontend stage
    runs) and must come back byte-identical to their last-good content
    rather than some half-written state, even though the config didn't
    change and so would rewrite identically anyway.
    """
    plugin_id = "demo.barejsrestore"
    root = _init(tmp_path, plugin_id, "view")

    code, _out, err = _build(root)
    assert code == 0, err
    manifest_snapshot = (root / "plugin.json").read_bytes()
    descriptors_snapshot = (root / "workbench-descriptors.json").read_bytes()

    frontend_path = root / FRONTEND_PLUGIN_JS
    source = frontend_path.read_text(encoding="utf-8")
    frontend_path.write_text(
        "import leftPad from 'left-pad';\n" + source, encoding="utf-8"
    )

    code, _out, err = _build(root)
    assert code != 0
    assert "left-pad" in err

    assert (root / "plugin.json").read_bytes() == manifest_snapshot
    assert (root / "workbench-descriptors.json").read_bytes() == descriptors_snapshot


def test_failed_compiled_ts_bare_specifier_build_restores_compiled_output(
    tmp_path: Path,
) -> None:
    """The compiled-entry counterpart: a .ts entry that later imports a
    genuinely unresolvable package fails at the (pre-existing) esbuild
    compile step, and frontend/plugin.js — the ONE artifact with a real
    prior/next distinction in the compiled path — is restored byte-for-byte
    to the last GREEN compiled output."""
    plugin_id = "demo.baretsrestore"
    root = _init(tmp_path, plugin_id, "view")
    (root / FRONTEND_PLUGIN_JS).unlink(missing_ok=True)
    (root / FRONTEND_PLUGIN_TS).write_text(
        "export const MainView = ({ React }: { React: any }) =>"
        " React.createElement('section', {}, 'ok');\n",
        encoding="utf-8",
    )

    code, _out, err = _build(root)
    assert code == 0, err
    good_snapshot = _artifact_bytes(root)
    assert good_snapshot[FRONTEND_PLUGIN_JS] is not None

    (root / FRONTEND_PLUGIN_TS).write_text(
        "import leftPad from 'unresolvable-left-pad-package';\n"
        "export const MainView = ({ React }: { React: any }) =>"
        " React.createElement('section', {}, leftPad('x'));\n",
        encoding="utf-8",
    )

    code, _out, err = _build(root)
    assert code != 0

    after = _artifact_bytes(root)
    assert after == good_snapshot
