"""`frisket plugin build`: compile an SDK-shaped plugin workspace into exactly
the static artifacts the loader trusts, then validate the output.

Pipeline (plugin-sdk-build-pipeline-v1):
1. vendor the packaged SDK runtime into <dir>/.frisket-sdk/index.mjs;
2. execute <dir>/plugin.config.mjs with node — the config default-exports the
   definePlugin(...) result — and write manifest -> plugin.json,
   descriptors -> workbench-descriptors.json;
3. compile the frontend entry (frontend/plugin.ts|tsx|jsx) to the single ES
   module frontend/plugin.js via esbuild (a plain .js entry is used as-is);
4. enforce the trusted-module byte/UTF-8 caps, reject any bare import
   specifier or server-runtime-only scheme (`node:`, `bun:`, `deno:`) in the
   EMITTED module (plugin-build-bare-specifier-rejection-v1 — the browser
   has no import map and cannot fetch a `node:`-style scheme, so node
   resolving either during the export check below is not evidence the
   module will load in a browser), then run the FULL validate on the
   output — build fails if its own output does not validate.

No change to load/trust/hash mechanics: the emitted artifacts remain the only
thing the host trusts.

A workspace with NO plugin.config.mjs takes the backend-only path instead
(`_plugin_build_from_declarations`): plugin.py's `Plugin(id=..., version=...,
actions=(...))` declaration generates plugin.json deterministically
(frisket.plugins.manifest_generate) and the same self-validate gate runs on
the output.

TRUST BOUNDARY: this pipeline executes plugin-author code -- it runs
``plugin.config.mjs`` and imports the emitted ``frontend/plugin.js`` under node
(``_CONFIG_RUNNER``/``_EXPORT_RUNNER``). It is the pip/datasette author-local
model: an author builds their OWN plugin on their OWN machine. Its only callers
are the ``frisket plugin build`` CLI (``frisket.cli``) and the CLI watch loop
(``frisket.plugin_dev``); NOTHING under ``frisket.server`` or any private
edition module invokes it. Never run ``plugin_build`` server-side on a third-party package --
that would execute untrusted code in the host process.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from importlib import resources
from pathlib import Path

# Paths inside the plugin root that the build reads through or writes to. A
# stray symlink at any of these (e.g. plugin.json -> /etc/nginx.conf, or
# .frisket-sdk -> ~/.ssh) would make the build clobber or read an out-of-tree
# target, so each is lstat-checked and rejected before any read/write --
# regardless of trust model, an author's own build must not follow a symlink
# out of the workspace.
_MANAGED_ARTIFACT_RELPATHS = (
    ".frisket-sdk",
    ".frisket-sdk/index.mjs",
    "plugin.json",
    "workbench-descriptors.json",
    "frontend",
    "frontend/plugin.js",
    "frontend/generated",
    "frontend/generated/actionTypes.ts",
    ".frisket-sdk/action-ui.d.ts",
)


def _first_symlinked_managed_path(plugin_root: Path) -> Path | None:
    """The first build-owned path that is itself a symlink, or None.

    Non-existent paths are fine (the build creates them); only an existing
    symlink at one of the managed locations is rejected. ``is_symlink`` uses
    lstat, so it never follows the link.
    """
    for rel in _MANAGED_ARTIFACT_RELPATHS:
        candidate = plugin_root / rel
        if candidate.is_symlink():
            return candidate
    return None


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a same-directory temp file + os.replace.

    A torn/interrupted write never leaves a half-written artifact for the
    validate-own-output pass (or a later load) to read, and the rename lands on
    ``path`` by name -- it does not follow a symlink there (the symlink guard
    above has already rejected one, and os.replace onto the final name is
    atomic on the same filesystem).
    """
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as sink:
            sink.write(text)
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


_CONFIG_RUNNER = """
import { pathToFileURL } from 'node:url';
const configUrl = pathToFileURL(process.argv[1]).href;
const module = await import(configUrl);
const defined = typeof module.default === 'function' ? await module.default() : module.default;
if (!defined || typeof defined !== 'object' || !defined.manifest || !defined.descriptors) {
  console.error('plugin.config.mjs must default-export the definePlugin(...) result');
  process.exit(3);
}
process.stdout.write(JSON.stringify(defined));
"""

# Cross-checks a workbench_components binding's component_key against the
# emitted module's real exports. Importing the module executes it — the SDK
# tutorial documents host-injected-React components with no runtime imports,
# so a top-level import is safe for a well-formed module; an import that
# throws (syntax error, bad top-level code) IS a build error and is reported
# with node's stderr.
_EXPORT_RUNNER = """
import { pathToFileURL } from 'node:url';
const moduleUrl = pathToFileURL(process.argv[1]).href;
let namespace;
try {
  namespace = await import(moduleUrl);
} catch (err) {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
}
process.stdout.write(JSON.stringify(Object.keys(namespace)));
"""

FRONTEND_ENTRY_CANDIDATES = (
    "frontend/plugin.tsx",
    "frontend/plugin.ts",
    "frontend/plugin.jsx",
    "frontend/plugin.js",
)

# A build step (node running plugin.config.mjs / an emitted module, or
# esbuild) that never exits — a plugin author's infinite loop, a hung
# `npx` fetch — must not block the `frisket plugin build` CLI (or the
# plugin_dev watch loop) forever. No legitimate build needs minutes.
_SUBPROCESS_TIMEOUT_SECONDS = 120


def _sdk_runtime_bytes() -> bytes:
    return (resources.files("frisket") / "data" / "plugin_sdk.mjs").read_bytes()


def vendor_sdk_runtime(plugin_root: Path) -> Path:
    target = plugin_root / ".frisket-sdk" / "index.mjs"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_sdk_runtime_bytes())
    return target


def _resolve_esbuild() -> list[str]:
    repo_root = Path(__file__).resolve().parents[3]
    local = repo_root / "sdk" / "node_modules" / ".bin" / "esbuild"
    if local.is_file():
        return [str(local)]
    found = shutil.which("esbuild")
    if found:
        return [found]
    return ["npx", "-y", "esbuild"]


# A specifier is legal in an EMITTED trusted-local module iff it's something
# the browser's plain `import()` (web/src/workbench/trustedLocalModule.ts —
# no import map anywhere in web/) can resolve on its own: relative (./ ../),
# absolute (/...), or an absolute URL (scheme:...). Anything else is a bare
# npm-style specifier that only resolves under a bundler's or node's own
# module resolution, neither of which the browser has.
_URL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")

# Scheme'd specifiers naming a server-runtime builtin module. These pass
# `_URL_SCHEME_RE` like a genuine `https:` URL — a scheme'd specifier is not
# "bare" per the ESM spec — but they are not fetchable resources: the
# browser's plain `import()` cannot load `node:fs` any more than it can load
# a bare `fs`. Without this list, `_extract_module_exports` below imports the
# emitted module with NODE, which resolves these builtins happily, so the
# build would go green on exactly the false-green shape the bare-specifier
# rejection below exists to kill, just narrowed to runtime builtins.
_RUNTIME_ONLY_SCHEMES = frozenset({"node", "bun", "deno"})


def _is_bare_specifier(specifier: str) -> bool:
    if specifier.startswith("./") or specifier.startswith("../"):
        return False
    if specifier.startswith("/"):
        return False
    if _URL_SCHEME_RE.match(specifier):
        return False
    return True


def _is_runtime_only_scheme_specifier(specifier: str) -> bool:
    """True for a scheme'd specifier naming a server-runtime builtin (at
    minimum `node:`, `bun:`, `deno:`) — legal per `_is_bare_specifier`'s
    scheme test, but not something the browser can load."""
    match = _URL_SCHEME_RE.match(specifier)
    if match is None:
        return False
    scheme = match.group(0)[:-1].lower()
    return scheme in _RUNTIME_ONLY_SCHEMES


def _scan_import_specifiers(
    esbuild: list[str], module_path: Path
) -> tuple[list[str] | None, str]:
    """List every import specifier esbuild's own parser finds in the EMITTED
    module — static `import ... from`/`export ... from` AND
    statically-analyzable dynamic `import(...)` calls — via
    `esbuild --bundle=false --metafile=...`.

    With bundling off, esbuild parses the file's syntax and records every
    import specifier it sees, but never attempts to RESOLVE any of them —
    it never touches node_modules or the filesystem for the imported paths.
    That is deliberate: node resolving a bare specifier via node_modules
    (which `_extract_module_exports` below relies on to import the module at
    all) is exactly the false-green plugin-build-bare-specifier-rejection-v1
    exists to kill, so this scan must not be fooled by node-resolvability
    either.

    CAVEAT (best-effort static scan, not a guarantee): a fully dynamic
    `import(someExpression)` has no literal specifier for esbuild's parser
    to record, so it is invisible to this check. The browser still has no
    import map to fall back on, so such an expression would need to
    evaluate to a relative/absolute/URL string at runtime to work at all —
    this scan just can't prove that ahead of time.

    Returns ``(None, stderr)`` if esbuild itself cannot parse the module
    (e.g. a genuine syntax error).
    """
    with tempfile.TemporaryDirectory() as scratch:
        meta_path = Path(scratch) / "meta.json"
        discard_path = Path(scratch) / "discard.js"
        try:
            run = subprocess.run(
                [
                    *esbuild,
                    str(module_path),
                    "--bundle=false",
                    "--format=esm",
                    f"--metafile={meta_path}",
                    f"--outfile={discard_path}",
                ],
                cwd=module_path.parent,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return (
                None,
                f"esbuild timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s scanning import specifiers\n",
            )
        if run.returncode != 0:
            return None, run.stderr
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    specifiers: list[str] = []
    for output in meta.get("outputs", {}).values():
        for entry in output.get("imports", []):
            path = entry.get("path")
            if isinstance(path, str):
                specifiers.append(path)
    return specifiers, run.stderr


def _extract_module_exports(
    node: str, module_path: Path
) -> tuple[list[str] | None, str]:
    """Run the emitted module through node and return its export names.

    Returns ``(None, stderr)`` when importing the module itself throws (a
    build error worth failing loudly on, per the CAVEAT — the SDK tutorial's
    emitted components have no runtime imports, so an import-time throw
    means the module is broken, not that the check is unsafe to run).
    """
    try:
        run = subprocess.run(
            [node, "--input-type=module", "-e", _EXPORT_RUNNER, str(module_path)],
            cwd=module_path.parent,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return (
            None,
            f"node timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s importing the emitted module\n",
        )
    if run.returncode != 0:
        return None, run.stderr
    return json.loads(run.stdout), run.stderr


def _missing_component_export_bindings(
    *,
    workbench_components: list[dict],
    module_rel_path: str,
    exports: set[str],
    exact_contributions: set[str] = frozenset(),
) -> list[dict]:
    """Bindings whose component_key resolves to no export the host would find.

    Mirrors the runtime resolution order in
    web/src/workbench/trustedLocalModule.ts (`loadTrustedLocalPluginExport`):
    the full component_key, then its last dot-segment (the export identifier
    an SDK author actually writes, e.g. `MainView`), then a default export as
    a fallback — a binding is only "missing" if none of those resolve.
    """
    missing = []
    for binding in workbench_components:
        if binding.get("module_path") != module_rel_path:
            continue
        component_key = binding.get("component_key", "")
        if binding.get("contribution_id") in exact_contributions:
            if component_key not in exports:
                missing.append(binding)
            continue
        export_name = component_key.rsplit(".", 1)[-1] if component_key else ""
        if component_key in exports or export_name in exports or "default" in exports:
            continue
        missing.append(binding)
    return missing


def _plugin_build_from_declarations(plugin_root: Path) -> int:
    """Backend-only build path: plugin.py's `Plugin(id=..., version=...,
    actions=(...))` declaration is the single source of truth, and
    plugin.json is emitted from it deterministically
    (frisket.plugins.manifest_generate). Chosen automatically whenever the
    workspace has no plugin.config.mjs. Same trust model as the config.mjs
    path above: author-local tooling importing the author's OWN backend
    module, with the exact loader real dispatch uses."""
    from frisket.cli.plugin import _plugin_validate
    from frisket.plugins.manifest_generate import (
        PluginManifestGenerationError,
        load_generation_source,
        manifest_dict_from_plugin,
        render_manifest_json,
    )

    module_path = plugin_root / "plugin.py"
    if not module_path.is_file():
        print(
            f"error: nothing to build in {plugin_root} — no plugin.config.mjs "
            "(SDK-shaped workspace) and no plugin.py (backend-only workspace)",
            file=sys.stderr,
        )
        return 1
    if module_path.is_symlink():
        print(
            f"error: refusing to import symlinked plugin.py ({module_path})",
            file=sys.stderr,
        )
        return 1
    manifest_path = plugin_root / "plugin.json"
    if manifest_path.is_symlink():
        print(
            f"error: refusing to build through symlinked path {manifest_path}"
            " (build artifacts must be regular files inside the plugin root)",
            file=sys.stderr,
        )
        return 1

    try:
        plugin = load_generation_source(plugin_root)
    except Exception as exc:  # noqa: BLE001 - author-facing build diagnostics
        print(f"error: plugin.py failed to import: {exc}", file=sys.stderr)
        return 1
    if plugin is None:
        print(
            "error: plugin.py declares no Plugin(id=..., version=...) — a "
            "backend-only workspace needs the manifest fields on its Plugin "
            "declaration (or add plugin.config.mjs for the SDK-shaped path)",
            file=sys.stderr,
        )
        return 1
    try:
        manifest = manifest_dict_from_plugin(plugin)
        manifest_text = render_manifest_json(manifest)
    except PluginManifestGenerationError as exc:
        print(f"error: {exc.code}: {exc.message}", file=sys.stderr)
        return 1

    if manifest["runtime"].get("workbench_components"):
        return _build_plugin_artifacts(
            plugin_root, {"manifest": manifest, "descriptors": {}}, plugin=plugin
        )

    prior = manifest_path.read_bytes() if manifest_path.exists() else None
    _atomic_write_text(manifest_path, manifest_text)
    # Build validates its OWN output — same gate as the config.mjs path.
    result = _plugin_validate(plugin_root)
    if result != 0:
        if prior is None:
            manifest_path.unlink(missing_ok=True)
        else:
            manifest_path.write_bytes(prior)
    return result


def plugin_build(plugin_root: Path) -> int:
    plugin_root = plugin_root.resolve()
    config_path = plugin_root / "plugin.config.mjs"
    if not config_path.is_file():
        return _plugin_build_from_declarations(plugin_root)

    # Reject symlinks at build-owned paths before any read/write follows one
    # out of the workspace. plugin.config.mjs is executed, so it is guarded
    # too; the rest are vendored/emitted below.
    if config_path.is_symlink():
        print(
            f"error: refusing to execute symlinked plugin.config.mjs ({config_path})",
            file=sys.stderr,
        )
        return 1
    symlinked = _first_symlinked_managed_path(plugin_root)
    if symlinked is not None:
        print(
            f"error: refusing to build through symlinked path {symlinked}"
            " (build artifacts must be regular files inside the plugin root)",
            file=sys.stderr,
        )
        return 1

    vendor_sdk_runtime(plugin_root)

    node = shutil.which("node")
    if node is None:
        print("error: `frisket plugin build` requires node on PATH", file=sys.stderr)
        return 1
    try:
        config_run = subprocess.run(
            [node, "--input-type=module", "-e", _CONFIG_RUNNER, str(config_path)],
            cwd=plugin_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=_SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        print(
            f"error: plugin.config.mjs timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s",
            file=sys.stderr,
        )
        return 1
    if config_run.returncode != 0:
        sys.stderr.write(config_run.stderr)
        print("error: plugin.config.mjs failed", file=sys.stderr)
        return 1
    defined = json.loads(config_run.stdout)
    return _build_plugin_artifacts(plugin_root, defined)


def _build_plugin_artifacts(plugin_root: Path, defined: dict, *, plugin=None) -> int:
    """Shared frontend emission and validation for both declaration sources."""
    from frisket.cli.plugin import _plugin_validate
    from frisket.plugins.frontend_modules import MAX_TRUSTED_LOCAL_FRONTEND_MODULE_BYTES

    node = shutil.which("node")
    if node is None:
        print("error: compiled plugin UI requires node on PATH", file=sys.stderr)
        return 1
    symlinked = _first_symlinked_managed_path(plugin_root)
    if symlinked is not None:
        print(
            f"error: refusing to build through symlinked path {symlinked}",
            file=sys.stderr,
        )
        return 1
    manifest_path = plugin_root / "plugin.json"
    descriptor_path = plugin_root / "workbench-descriptors.json"
    # Snapshot prior artifacts so a failed build never leaves this run's
    # half-emitted output behind.
    prior = {
        path: path.read_bytes() if path.exists() else None
        for path in (
            manifest_path,
            descriptor_path,
            # The compiled module too: a failed build must not leave a
            # freshly compiled frontend/plugin.js behind.
            plugin_root / "frontend" / "plugin.js",
            plugin_root / "frontend" / "generated" / "actionTypes.ts",
            plugin_root / ".frisket-sdk" / "action-ui.d.ts",
        )
    }

    def _restore_prior() -> None:
        for path, content in prior.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)

    if plugin is not None:
        from frisket.plugins.manifest_generate import render_plugin_action_types

        (plugin_root / "frontend/generated").mkdir(parents=True, exist_ok=True)
        (plugin_root / ".frisket-sdk").mkdir(exist_ok=True)
        _atomic_write_text(
            plugin_root / "frontend/generated/actionTypes.ts",
            render_plugin_action_types(plugin),
        )
        _atomic_write_text(
            plugin_root / ".frisket-sdk/action-ui.d.ts",
            resources.files("frisket.data")
            .joinpath("action_ui.d.ts")
            .read_text(encoding="utf-8"),
        )

    _atomic_write_text(
        manifest_path,
        json.dumps(defined["manifest"], indent=2, sort_keys=True) + "\n",
    )
    descriptors = defined["descriptors"]
    if descriptors.get("descriptors"):
        _atomic_write_text(
            descriptor_path,
            json.dumps(descriptors, indent=2, sort_keys=True) + "\n",
        )
    else:
        # No workbench contributions (e.g. an action-only plugin): validate
        # treats a missing descriptor package as legal, an EMPTY one as an
        # error — emit nothing.
        descriptor_path.unlink(missing_ok=True)

    entry = next(
        (
            plugin_root / candidate
            for candidate in FRONTEND_ENTRY_CANDIDATES
            if (plugin_root / candidate).is_file()
        ),
        None,
    )
    module_out = plugin_root / "frontend" / "plugin.js"
    needs_frontend = bool(
        defined["manifest"].get("runtime", {}).get("workbench_components")
    )
    if entry is None:
        if needs_frontend:
            print(
                "error: manifest binds workbench components but no frontend entry"
                f" exists (looked for {', '.join(FRONTEND_ENTRY_CANDIDATES)})",
                file=sys.stderr,
            )
            _restore_prior()
            return 1
    elif entry.suffix != ".js":
        esbuild = _resolve_esbuild()
        try:
            compile_run = subprocess.run(
                [
                    *esbuild,
                    str(entry),
                    "--bundle",
                    "--format=esm",
                    "--platform=neutral",
                    "--target=es2022",
                    "--jsx-factory=React.createElement",
                    "--jsx-fragment=React.Fragment",
                    f"--outfile={module_out}",
                ],
                cwd=plugin_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=_SUBPROCESS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            print(
                f"error: frontend compile timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s",
                file=sys.stderr,
            )
            _restore_prior()
            return 1
        if compile_run.returncode != 0:
            sys.stderr.write(compile_run.stderr)
            print("error: frontend compile failed", file=sys.stderr)
            _restore_prior()
            return 1

    if module_out.is_file():
        raw = module_out.read_bytes()
        if len(raw) > MAX_TRUSTED_LOCAL_FRONTEND_MODULE_BYTES:
            print(
                f"error: {module_out.name} is {len(raw)} bytes, over the trusted"
                f" module cap ({MAX_TRUSTED_LOCAL_FRONTEND_MODULE_BYTES})",
                file=sys.stderr,
            )
            _restore_prior()
            return 1
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            print(f"error: {module_out.name} is not valid UTF-8", file=sys.stderr)
            _restore_prior()
            return 1

        # Reject bare import specifiers in the EMITTED module: scan IT, not
        # the source, because that's what ships — a compiled .ts/.tsx/.jsx entry
        # gets this for free (esbuild's --bundle inlines resolvable
        # packages and fails the compile step above for unresolvable ones,
        # so nothing bare ever survives into the output), but the plain-.js
        # passthrough entry (no separate compile step) copies the author's
        # source verbatim, so a bare `import 'left-pad'` would otherwise
        # reach the browser unexamined.
        esbuild = _resolve_esbuild()
        specifiers, scan_stderr = _scan_import_specifiers(esbuild, module_out)
        if specifiers is None:
            sys.stderr.write(scan_stderr)
            print(
                f"error: could not parse {module_out.name} to check import specifiers",
                file=sys.stderr,
            )
            _restore_prior()
            return 1
        bare = sorted({s for s in specifiers if _is_bare_specifier(s)})
        runtime_only = sorted(
            {s for s in specifiers if _is_runtime_only_scheme_specifier(s)}
        )
        if bare or runtime_only:
            for specifier in bare:
                print(
                    f"error: {module_out.name} imports {specifier!r}, a bare"
                    " npm specifier the browser cannot resolve (no import"
                    " map) — write frontend/plugin.ts(x) so dependencies are"
                    " bundled",
                    file=sys.stderr,
                )
            for specifier in runtime_only:
                print(
                    f"error: {module_out.name} imports {specifier!r}, a"
                    " server-runtime builtin the browser cannot load — there"
                    " is no browser equivalent, so remove the import",
                    file=sys.stderr,
                )
            _restore_prior()
            return 1

        # Cross-check workbench_components bindings against the module's real
        # exports: a component_key that names no export otherwise surfaces
        # only at runtime as a "missing" binding.
        workbench_components = (
            defined["manifest"].get("runtime", {}).get("workbench_components", [])
        )
        module_rel_path = module_out.relative_to(plugin_root).as_posix()
        if any(
            binding.get("module_path") == module_rel_path
            for binding in workbench_components
        ):
            exports, export_stderr = _extract_module_exports(node, module_out)
            if exports is None:
                sys.stderr.write(export_stderr)
                print(
                    f"error: {module_out.name} threw on import — cannot verify"
                    " workbench component exports",
                    file=sys.stderr,
                )
                _restore_prior()
                return 1
            found = set(exports)
            missing = _missing_component_export_bindings(
                workbench_components=workbench_components,
                module_rel_path=module_rel_path,
                exports=found,
                exact_contributions={
                    item["kind"]
                    for item in defined["manifest"]["runtime"]["actions"]
                    if item.get("handler_api") == "typed_action"
                },
            )
            if missing:
                found_list = ", ".join(sorted(found)) or "(none)"
                for binding in missing:
                    print(
                        "error: workbench component binding"
                        f" {binding.get('contribution_id')!r} names component_key"
                        f" {binding.get('component_key')!r}, which matches no export"
                        f" in {module_out.name}; exports found: [{found_list}]",
                        file=sys.stderr,
                    )
                _restore_prior()
                return 1

    # Build validates its OWN output — same gate as `frisket plugin validate`.
    result = _plugin_validate(plugin_root)
    if result != 0:
        _restore_prior()
    return result
