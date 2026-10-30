#!/bin/bash
set -euo pipefail

usage() {
  echo "usage: $0 <action-compare-worktree> <node-args...>" >&2
  echo "example: $0 /home/dev/actor-worktrees/action-compare-ui node_modules/vitest/vitest.mjs run tests/foo.test.tsx" >&2
  exit 64
}

(( $# >= 2 )) || usage
worktree=$(realpath -e -- "$1")
shift

case "$worktree" in
  /home/dev/actor-worktrees/docs-captures-20261030) ;;
  *) echo "refusing non-Action-Compare worktree: $worktree" >&2; exit 65 ;;
esac

[[ ! -e "$worktree/.secrets" && ! -L "$worktree/.secrets" ]] || {
  echo "refusing worktree containing .secrets" >&2; exit 66
}

expected_web_lock=3a694a00123729d97e6b891a3c921782a43bdc2c9feaddb550de3c21de7a7623
expected_sdk_lock=a1ec9d28d7c5f94ce3c94808b9512e4078a83a7179cfa554e6c2db6f100fa778
actual_web_lock=$(sha256sum "$worktree/web/package-lock.json" | awk '{print $1}')
actual_sdk_lock=$(sha256sum "$worktree/sdk/package-lock.json" | awk '{print $1}')
[[ "$actual_web_lock" == "$expected_web_lock" ]] || {
  echo "web lock drift: expected $expected_web_lock, got $actual_web_lock" >&2
  exit 66
}
[[ "$actual_sdk_lock" == "$expected_sdk_lock" ]] || {
  echo "SDK lock drift: expected $expected_sdk_lock, got $actual_sdk_lock" >&2
  exit 66
}

provider_root=/home/dev/Development/frisket/actor-worktrees/ui-inventory-20261019
provider_web="$provider_root/web/node_modules"
provider_sdk="$provider_root/sdk/node_modules"
python_venv=/home/dev/Development/frisket/frisket/.venv
python="$python_venv/bin/python"
[[ -d "$provider_web" && -d "$provider_sdk" ]] || {
  echo "exact read-only Node provider is unavailable: $provider_root" >&2
  exit 67
}
[[ -x "$python" ]] || {
  echo "canonical read-only Python is unavailable: $python" >&2
  exit 67
}

prepare_shell() {
  local destination=$1
  local provider=$2
  local kind=$3
  local marker="$destination/.docs-foundation-readonly-shell"

  if [[ -e "$destination" && ! -f "$marker" ]]; then
    echo "refusing pre-existing unmanaged dependency tree: $destination" >&2
    exit 68
  fi
  mkdir -p "$destination" "$destination/.bin"
  : > "$marker"

  local entry name scope package target
  for entry in "$provider"/*; do
    [[ -e "$entry" || -L "$entry" ]] || continue
    name=$(basename -- "$entry")
    [[ "$name" == "@frisket" ]] && continue
    if [[ "$name" == @* && -d "$entry" ]]; then
      scope="$destination/$name"
      mkdir -p "$scope"
      for package in "$entry"/*; do
        [[ -e "$package" || -L "$package" ]] || continue
        target="$scope/$(basename -- "$package")"
        if [[ -e "$target" || -L "$target" ]]; then
          [[ -L "$target" && "$(readlink -- "$target")" == "$package" ]] || {
            echo "dependency-shell collision: $target" >&2
            exit 69
          }
        else
          ln -s -- "$package" "$target"
        fi
      done
    else
      target="$destination/$name"
      if [[ -e "$target" || -L "$target" ]]; then
        [[ -L "$target" && "$(readlink -- "$target")" == "$entry" ]] || {
          echo "dependency-shell collision: $target" >&2
          exit 69
        }
      else
        ln -s -- "$entry" "$target"
      fi
    fi
  done

  for entry in "$provider"/.bin/*; do
    [[ -e "$entry" || -L "$entry" ]] || continue
    target="$destination/.bin/$(basename -- "$entry")"
    if [[ -e "$target" || -L "$target" ]]; then
      [[ -L "$target" && "$(readlink -- "$target")" == "$entry" ]] || {
        echo "binary-shell collision: $target" >&2
        exit 69
      }
    else
      ln -s -- "$entry" "$target"
    fi
  done

  if [[ "$kind" == web ]]; then
    mkdir -p "$destination/@frisket"
    target="$destination/@frisket/plugin-sdk"
    if [[ -e "$target" || -L "$target" ]]; then
      [[ -L "$target" && "$(readlink -- "$target")" == "$worktree/sdk" ]] || {
        echo "plugin SDK collision: $target" >&2
        exit 69
      }
    else
      ln -s -- "$worktree/sdk" "$target"
    fi
  fi
}

web_shell="$worktree/web/node_modules"
sdk_shell="$worktree/sdk/node_modules"
prepare_shell "$web_shell" "$provider_web" web
prepare_shell "$sdk_shell" "$provider_sdk" sdk

artifact_root=/home/dev/actor-artifacts/docs-execution-20261030/captures
lane=$(basename -- "$worktree")
runtime="$artifact_root/runtime/$lane/node"
mkdir -p "$runtime/home" "$runtime/cache" "$runtime/npm-cache" "$runtime/tmp" \
  "$runtime/vite" "$runtime/vite-temp" "$runtime/node-tmp" "$worktree/sdk/dist" \
  "$web_shell/.vite" "$web_shell/.vite-temp" "$web_shell/.tmp"

# The SDK contract module is generated and gitignored. Keep its content in the
# lane runtime, then mount it over an ignored placeholder so generation never
# writes to the source checkout or shared provider.
contracts_target="$worktree/sdk/src/contracts.gen.ts"
contracts_overlay="$runtime/contracts.gen.ts"
if [[ ! -e "$contracts_target" ]]; then
  : > "$contracts_target"
fi
if [[ ! -s "$contracts_overlay" && -s "$contracts_target" ]]; then
  cp -- "$contracts_target" "$contracts_overlay"
elif [[ ! -e "$contracts_overlay" ]]; then
  : > "$contracts_overlay"
fi

exec /usr/bin/env -i PATH=/usr/bin:/bin LANG=C.UTF-8 TZ=UTC \
  /usr/bin/bwrap --die-with-parent --new-session --unshare-net --unshare-pid \
  --ro-bind /usr /usr --symlink usr/bin /bin --symlink usr/lib /lib --symlink usr/lib64 /lib64 \
  --dir /etc --ro-bind /etc/ld.so.cache /etc/ld.so.cache --ro-bind /etc/passwd /etc/passwd \
  --ro-bind /etc/group /etc/group --ro-bind /etc/nsswitch.conf /etc/nsswitch.conf \
  --ro-bind "$worktree" "$worktree" --ro-bind "$provider_root" "$provider_root" \
  --ro-bind "$python_venv" "$python_venv" \
  --bind "$runtime" "$runtime" --bind "$worktree/sdk/dist" "$worktree/sdk/dist" \
  --bind "$contracts_overlay" "$contracts_target" \
  --bind "$runtime/vite" "$web_shell/.vite" --bind "$runtime/vite-temp" "$web_shell/.vite-temp" \
  --bind "$runtime/node-tmp" "$web_shell/.tmp" --proc /proc --dev /dev --tmpfs /tmp --clearenv \
  --setenv PATH "/usr/bin:/bin:$web_shell/.bin:$sdk_shell/.bin" \
  --setenv LANG C.UTF-8 --setenv TZ UTC --setenv HOME "$runtime/home" \
  --setenv XDG_CACHE_HOME "$runtime/cache" --setenv npm_config_cache "$runtime/npm-cache" \
  --setenv TMPDIR "$runtime/tmp" --setenv VITE_CACHE_DIR "$runtime/vite-temp" \
  --setenv FRISKET_TEST_PYTHON "$python" --setenv PYTHONPATH "$worktree/src" \
  --setenv PYTHONDONTWRITEBYTECODE 1 --setenv PYTHONNOUSERSITE 1 \
  --setenv CI 1 --chdir "$worktree/web" /usr/bin/node "$@"
