#!/bin/bash
set -euo pipefail

usage() {
  echo "usage: $0 <action-compare-worktree> [--self-test]" >&2
  echo "example: $0 /home/dev/actor-worktrees/action-compare-connected" >&2
  exit 64
}

(( $# >= 1 && $# <= 2 )) || usage
worktree=$(realpath -e -- "$1")
mode=${2:-run}
[[ "$mode" == run || "$mode" == --self-test ]] || usage

case "$worktree" in
  /home/dev/actor-worktrees/docs-captures-20261030) ;;
  *) echo "refusing non-Action-Compare worktree: $worktree" >&2; exit 65 ;;
esac

[[ -z "$(git -C "$worktree" status --porcelain --untracked-files=all)" ]] || {
  echo "refusing capture from an uncommitted worktree: $worktree" >&2; exit 66
}

[[ ! -e "$worktree/.secrets" && ! -L "$worktree/.secrets" ]] || {
  echo "refusing to mount a worktree containing .secrets: $worktree" >&2
  exit 66
}

artifact_root=/home/dev/actor-artifacts/docs-execution-20261030/captures
run_node="$worktree/web/tests/e2e/docs/run-node.sh"
provider_root=/home/dev/Development/frisket/actor-worktrees/ui-inventory-20261019
provider_web="$provider_root/web/node_modules"
provider_sdk="$provider_root/sdk/node_modules"
python_venv=/home/dev/Development/frisket/frisket/.venv
python="$python_venv/bin/python"
uv=/home/dev/.local/bin/uv
browser_cache=/home/dev/.cache/ms-playwright

for required in "$run_node" "$python" "$uv" \
  "$browser_cache/chromium-1223/chrome-linux64/chrome" \
  "$browser_cache/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell" \
  "$browser_cache/ffmpeg-1011/ffmpeg-linux"; do
  [[ -e "$required" ]] || {
    echo "required read-only validation resource is unavailable: $required" >&2
    exit 67
  }
done

# Reuse the exact-lock Node harness to validate both locks, prepare the
# per-lane dependency shells, and generate the ignored SDK contract overlay.
"$run_node" "$worktree" ../sdk/scripts/gen-contracts.mjs

web_shell="$worktree/web/node_modules"
sdk_shell="$worktree/sdk/node_modules"
contracts_target="$worktree/sdk/src/contracts.gen.ts"
lane=$(basename -- "$worktree")
runtime="$artifact_root/runtime/$lane/connected"
screenshots="$worktree/web/screenshots/action-compare-connected"
frisket_state="$worktree/.frisket"
connected_artifacts="$artifact_root/images/$lane"

mkdir -p "$runtime/home" "$runtime/cache" "$runtime/npm-cache" "$runtime/config" \
  "$runtime/uv-cache" "$runtime/tmp" "$runtime/vite" \
  "$runtime/vite-temp" "$runtime/node-tmp" "$screenshots" \
  "$frisket_state" "$connected_artifacts"

for writable in "$runtime" "$screenshots" "$frisket_state" "$connected_artifacts"; do
  [[ ! -L "$writable" ]] || {
    echo "refusing symlinked writable target: $writable" >&2
    exit 68
  }
done

contracts_overlay="$artifact_root/runtime/$lane/node/contracts.gen.ts"
[[ -s "$contracts_overlay" && -e "$contracts_target" ]] || {
  echo "SDK contracts overlay was not generated: $contracts_overlay" >&2
  exit 69
}

# The lane's per-package node_modules shell resolves font files into the
# read-only provider outside Vite's default filesystem allowlist. Keep the
# product config untouched: import that exact config from a lane-runtime
# overlay, preserving its own relative imports and __dirname, and alter only
# the dev server allowlist. A read-only package overlay selects this config for
# the existing `npm run dev` command used by the standard Playwright stack.
vite_overlay="$runtime/config/action-compare-vite.config.mjs"
package_overlay="$runtime/config/package.json"
/usr/bin/node - "$worktree/web/vite.config.ts" "$worktree" "$provider_web" "$provider_sdk" \
  "$vite_overlay" "$worktree/web/package.json" "$package_overlay" <<'JS'
const fs = require('node:fs');

const [viteConfig, worktree, providerWeb, providerSdk, viteOverlay, packageSource, packageOverlay] = process.argv.slice(2);
const quoted = JSON.stringify;
const overlay = `import baseConfig from ${quoted(viteConfig)};

const harnessAllow = ${JSON.stringify([worktree, providerWeb, providerSdk])};

export default async function actionCompareHarnessConfig(env) {
  const base = typeof baseConfig === 'function' ? await baseConfig(env) : baseConfig;
  const server = base.server ?? {};
  const fileSystem = server.fs ?? {};
  return {
    ...base,
    server: {
      ...server,
      fs: {
        ...fileSystem,
        allow: [...new Set([...harnessAllow, ...(fileSystem.allow ?? [])])],
      },
    },
  };
}
`;
const pkg = JSON.parse(fs.readFileSync(packageSource, 'utf8'));
if (pkg?.scripts?.dev !== 'vite') {
  throw new Error(`refusing unexpected lane dev command: ${String(pkg?.scripts?.dev)}`);
}
pkg.scripts.dev = `vite --config ${viteOverlay}`;

for (const [target, contents] of [
  [viteOverlay, overlay],
  [packageOverlay, `${JSON.stringify(pkg, null, 2)}\n`],
]) {
  const temporary = `${target}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, contents, { mode: 0o444 });
  fs.renameSync(temporary, target);
}
JS

cli=node_modules/@playwright/test/cli.js
spec='docs/recipes.spec.ts'
config=playwright/playwright.config.ts

bwrap=(
  /usr/bin/bwrap --die-with-parent --new-session --unshare-net --unshare-pid
  --ro-bind /usr /usr
  --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/lib64 /lib64
  --dir /etc
  --ro-bind /etc/ld.so.cache /etc/ld.so.cache
  --ro-bind /etc/passwd /etc/passwd --ro-bind /etc/group /etc/group
  --ro-bind /etc/nsswitch.conf /etc/nsswitch.conf --ro-bind /etc/fonts /etc/fonts
  --ro-bind "$worktree" "$worktree"
  --ro-bind "$provider_web" "$provider_web" --ro-bind "$provider_sdk" "$provider_sdk"
  --ro-bind "$python_venv" "$python_venv" --ro-bind "$uv" "$uv"
  --ro-bind "$browser_cache" "$browser_cache"
  --ro-bind /home/dev/.cache/huggingface/hub /home/dev/.cache/huggingface/hub
  --ro-bind /home/dev/actor-artifacts/docs-execution-20261030/integration-runtime/mai-30s-recording.json "$runtime/mai-30s-recording.json"
  --bind "$runtime" "$runtime"
  --ro-bind /home/dev/actor-artifacts/docs-execution-20261030/integration-runtime/court-live.cache.db "$runtime/court-live.cache.db"
  --ro-bind "$vite_overlay" "$vite_overlay"
  --ro-bind "$package_overlay" "$worktree/web/package.json"
  --bind "$screenshots" "$screenshots"
  --bind "$frisket_state" "$frisket_state"
  --bind "$connected_artifacts" "$connected_artifacts"
  --bind "$contracts_overlay" "$contracts_target"
  --bind "$runtime/vite" "$web_shell/.vite"
  --bind "$runtime/vite-temp" "$web_shell/.vite-temp"
  --bind "$runtime/node-tmp" "$web_shell/.tmp"
  --proc /proc --dev /dev --tmpfs /tmp --clearenv
  --setenv PATH "/home/dev/.local/bin:/usr/bin:/bin:$web_shell/.bin:$sdk_shell/.bin"
  --setenv LANG C.UTF-8 --setenv TZ UTC --setenv HOME "$runtime/home"
  --setenv XDG_CACHE_HOME "$runtime/cache" --setenv npm_config_cache "$runtime/npm-cache"
  --setenv UV_CACHE_DIR "$runtime/uv-cache" --setenv TMPDIR "$runtime/tmp"
  --setenv VITE_CACHE_DIR "$runtime/vite-temp"
  --setenv PYTHONPATH "$worktree/web/tests/e2e/docs/mai_transport:$worktree/src"
  --setenv HF_HUB_CACHE /home/dev/.cache/huggingface/hub
  --setenv FRISKET_DOCS_MAI_FIXTURE "$runtime/mai-30s-recording.json" --setenv PYTHONDONTWRITEBYTECODE 1
  --setenv PYTHONNOUSERSITE 1 --setenv UV_PROJECT_ENVIRONMENT "$python_venv"
  --setenv UV_NO_SYNC 1 --setenv FRISKET_E2E_PYTHON "$python"
  --setenv FRISKET_DOCS_COURT_CACHE "$runtime/court-live.cache.db"
  --setenv OPENROUTER_API_KEY docs-offline-placeholder --setenv GEMINI_API_KEY docs-offline-placeholder --setenv FRISKET_CACHE_MODE replay_strict --setenv FRISKET_E2E_MINIMAL_DEPS 1
  --setenv FRISKET_E2E_WORKERS 1 --setenv FRISKET_E2E_CLEANUP always
  --setenv FRISKET_E2E_BASE_WS "$frisket_state/e2e-ws"
  --setenv FRISKET_CHECKLOG_DISABLE 1
  --setenv FRISKET_DOCS_PROOF_DIR "$connected_artifacts/bundle"
  --setenv FRISKET_DOCS_REVISION "$(git -C "$worktree" rev-parse HEAD)"
  --setenv FRISKET_E2E_VITE_CONFIG "$vite_overlay"
  --setenv FRISKET_E2E_PROVIDER_WEB "$provider_web"
  --setenv FRISKET_E2E_PROVIDER_SDK "$provider_sdk"
  --setenv PLAYWRIGHT_BROWSERS_PATH "$browser_cache"
  --setenv PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD 1 --setenv CI 1
  --chdir "$worktree/web"
)

playwright_args=(
  "$cli" test "$spec" --config "$config" --project=chromium --no-deps
  --workers=1 --retries=0 --trace=on --output "$runtime/test-results"
)

if [[ -n "${FRISKET_COMPARE_GREP:-}" ]]; then
  playwright_args+=(--grep "$FRISKET_COMPARE_GREP")
fi

if [[ "$mode" == --self-test ]]; then
  self_test_js=$(cat <<'JS'
const http = require('node:http');
const { execFileSync } = require('node:child_process');
const { existsSync, realpathSync } = require('node:fs');

const browser = `${process.env.PLAYWRIGHT_BROWSERS_PATH}/chromium_headless_shell-1223/chrome-headless-shell-linux64/chrome-headless-shell`;
if (!existsSync(browser)) throw new Error(`missing exact Chromium 1223 executable: ${browser}`);

const server = http.createServer((_request, response) => response.end('loopback-ok'));
server.listen(0, '127.0.0.1', async () => {
  try {
    const address = server.address();
    if (!address || typeof address === 'string') throw new Error('loopback server has no TCP address');
    const response = await fetch(`http://127.0.0.1:${address.port}`);
    if (await response.text() !== 'loopback-ok') throw new Error('isolated loopback response mismatch');
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
    execFileSync(process.env.FRISKET_E2E_PYTHON, ['-c',
      'import frisket, os, sys; source = os.path.realpath(os.environ["PYTHONPATH"].split(os.pathsep)[-1]); assert os.path.realpath(frisket.__file__).startswith(source + os.sep); from frisket.actions.system import typed_action_for_request; from frisket.ai.llm import LLMRequest, LLMResponse, ResponseCache, request_key; from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan; from frisket.engine.store import Project; import frisket.cli; print("source-first connected Python imports ok:", sys.executable)'
    ], { stdio: 'inherit' });
    execFileSync('/home/dev/.local/bin/uv', ['--version'], { stdio: 'inherit' });
    const vite = await import(`${process.env.FRISKET_E2E_PROVIDER_WEB}/vite/dist/node/index.js`);
    const resolved = await vite.resolveConfig({ configFile: process.env.FRISKET_E2E_VITE_CONFIG }, 'serve');
    const allowed = new Set(resolved.server.fs.allow.map((entry) => realpathSync(entry)));
    for (const required of [process.env.PYTHONPATH.split(':').at(-1).replace(/\/src$/, ''), process.env.FRISKET_E2E_PROVIDER_WEB, process.env.FRISKET_E2E_PROVIDER_SDK]) {
      if (!allowed.has(realpathSync(required))) throw new Error(`Vite filesystem allowlist is missing ${required}`);
    }
    execFileSync(process.execPath, process.argv.slice(1).concat('--list'), { stdio: 'inherit' });
    console.log(`isolated loopback ok: 127.0.0.1:${address.port}`);
    console.log('Playwright connected command/config discovery ok');
    console.log('Vite read-only dependency-provider allowlist ok');
  } catch (error) {
    server.close(() => {});
    console.error(error && error.stack ? error.stack : String(error));
    process.exitCode = 1;
  }
});
JS
)
  exec /usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC \
    "${bwrap[@]}" /usr/bin/node -e "$self_test_js" "${playwright_args[@]}"
fi

exec /usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC \
  "${bwrap[@]}" /usr/bin/node "${playwright_args[@]}"
