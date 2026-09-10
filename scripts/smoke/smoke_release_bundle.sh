#!/bin/sh
# Boot the exact rendered server tarball, exercise its public HTTP contract,
# restart it, and prove data persisted.  This is a release-CI lifecycle wrapper;
# scripts/smoke/deployment_http_smoke.py is the provider-neutral proof itself.
set -eu

die() { printf '%s\n' "release-bundle-smoke: $*" >&2; exit 1; }

[ "$#" -ge 1 ] && [ "$#" -le 2 ] \
  || die "usage: smoke_release_bundle.sh frisket-server-VERSION.tar.gz [standalone|multi-service]"
command -v docker >/dev/null 2>&1 || die "Docker Engine is required"
command -v python3 >/dev/null 2>&1 || die "Python 3 is required"
command -v flock >/dev/null 2>&1 || die "flock (util-linux) is required"
docker info >/dev/null 2>&1 || die "Docker daemon is unavailable"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"

BUNDLE=$1
PACKAGE=${2:-standalone}
[ "$PACKAGE" = standalone ] || [ "$PACKAGE" = multi-service ] \
  || die "package must be standalone or multi-service"
[ -f "$BUNDLE" ] || die "bundle not found: $BUNDLE"
case "$BUNDLE" in
  /*) ;;
  *) BUNDLE=$(CDPATH='' cd -- "$(dirname -- "$BUNDLE")" && pwd)/$(basename -- "$BUNDLE") ;;
esac

# The bundled installer intentionally uses the fixed Compose project `frisket`
# and loopback port 8000. Serialize the entire clean-check -> install -> smoke
# -> teardown lifetime on a shared daemon; the installer root lock alone cannot
# coordinate two different temporary roots.
old_umask=$(umask)
umask 077
exec 9>>/tmp/frisket-release-smoke.lock
umask "$old_umask"
flock -x -n 9 \
  || die "another release-bundle smoke already owns the fixed Compose project"

SMOKE_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/frisket-release-smoke.XXXXXX")
SMOKE_ROOT=$(CDPATH='' cd -- "$SMOKE_ROOT" && pwd -P)
EXTRACTED=$SMOKE_ROOT/bundle
RUNTIME=$SMOKE_ROOT/runtime
PROJECT=frisket
CLEANUP_IMAGE=

runtime_project_owned() {
  # The installer writes this hash-checked ownership marker before Compose
  # startup. It also lets cleanup remove a project network when `up` failed
  # before creating its first container.
  [ -f "$RUNTIME/.frisket-install" ] || return 1
  container_ids=$(docker ps -aq \
    --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null) || return 1
  for container_id in $container_ids; do
    working_dir=$(docker inspect --format \
      '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' \
      "$container_id" 2>/dev/null) || return 1
    [ "$working_dir" = "$RUNTIME" ] || return 1
  done
}

require_clean_fixed_project() {
  existing_containers=$(docker ps -aq \
    --filter "label=com.docker.compose.project=$PROJECT") \
    || die "cannot inspect existing Compose containers"
  existing_networks=$(docker network ls -q \
    --filter "label=com.docker.compose.project=$PROJECT") \
    || die "cannot inspect existing Compose networks"
  [ -z "$existing_containers" ] \
    || die "fixed Compose project '$PROJECT' already has containers; refusing smoke"
  [ -z "$existing_networks" ] \
    || die "fixed Compose project '$PROJECT' already has networks; refusing smoke"
}

cleanup() {
  status=$?
  cleanup_failed=0
  trap - EXIT HUP INT TERM
  # Check labels at cleanup time rather than toggling a flag after `up`: even a
  # partially failed installer/Compose start is removed, while an unrelated
  # project with the same fixed installer project name is never touched.
  if runtime_project_owned; then
    if [ "$status" -ne 0 ]; then
      printf '%s\n' "release-bundle-smoke: Compose state at failure:" >&2
      (cd "$RUNTIME" && docker compose -p "$PROJECT" -f compose.yaml ps) >&2 2>/dev/null || true
      if [ -x "$EXTRACTED/frisket-deployment-diagnostics" ]; then
        printf '%s\n' "release-bundle-smoke: bounded redacted diagnostics:" >&2
        "$EXTRACTED/frisket-deployment-diagnostics" \
          --root "$RUNTIME" \
          --project "$PROJECT" \
          --readiness-url http://127.0.0.1:8000/api/ready \
          --tail 40 >&2 || true
      fi
    fi
    (cd "$RUNTIME" && docker compose -p "$PROJECT" -f compose.yaml down --remove-orphans --timeout 20) >/dev/null 2>&1 || true
  fi
  # SMOKE_ROOT is always a mktemp-created, task-specific directory.
  rm -rf -- "$SMOKE_ROOT" 2>/dev/null || true
  if [ -e "$SMOKE_ROOT" ]; then
    # Bind-mounted app/Postgres data can be owned by container UIDs that the
    # unprivileged CI runner cannot remove. Reuse only the already-validated,
    # already-pulled Frisket digest, with no network and no host path beyond
    # this unique smoke root, to remove those entries as container root.
    if [ -n "$CLEANUP_IMAGE" ] && [ -d "$SMOKE_ROOT" ]; then
      docker run --rm --pull=never --network none --read-only \
        --user 0:0 \
        --entrypoint /bin/sh \
        --mount "type=bind,src=$SMOKE_ROOT,dst=/smoke" \
        "$CLEANUP_IMAGE" \
        -c 'rm -rf -- /smoke/* /smoke/.[!.]* /smoke/..?*' \
        >/dev/null 2>&1 || true
      rm -rf -- "$SMOKE_ROOT" 2>/dev/null || true
    else
      cleanup_failed=1
    fi
  fi
  if [ -e "$SMOKE_ROOT" ]; then
    cleanup_failed=1
  fi
  if [ "$cleanup_failed" -ne 0 ]; then
    printf '%s\n' \
      "release-bundle-smoke: cleanup could not remove owned root $SMOKE_ROOT" >&2
    # Never replace the useful original smoke failure with teardown noise, but
    # do fail an otherwise-successful proof when it leaks its temporary root.
    [ "$status" -ne 0 ] || status=1
  fi
  exit "$status"
}
trap cleanup EXIT HUP INT TERM
require_clean_fixed_project

mkdir -p "$EXTRACTED"
# Reject links, devices, absolute names, and traversal before extracting even
# though CI generated this archive moments earlier.  Release gates should also
# remain safe when invoked manually against a downloaded candidate.
python3 - "$BUNDLE" "$EXTRACTED" <<'PY'
from __future__ import annotations

import os
import pathlib
import shutil
import sys
import tarfile

archive_path, destination = sys.argv[1:]
root = pathlib.Path(destination)
with tarfile.open(archive_path, "r:gz") as archive:
    members = archive.getmembers()
    if not members:
        raise SystemExit("release-bundle-smoke: archive is empty")
    for member in members:
        relative = pathlib.PurePosixPath(member.name)
        if relative.is_absolute() or ".." in relative.parts or not member.isfile():
            raise SystemExit(
                f"release-bundle-smoke: unsafe archive member: {member.name!r}"
            )
        source = archive.extractfile(member)
        if source is None:
            raise SystemExit(
                f"release-bundle-smoke: unreadable archive member: {member.name!r}"
            )
        target = root.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
        os.chmod(target, member.mode & 0o777)
PY

for file in \
  VERSION \
  frisket-install \
  frisket-deployment-diagnostics \
  frisket-smoke \
  deploy/release/release.env \
  "deploy/release/compose.$PACKAGE.yaml"; do
  [ -f "$EXTRACTED/$file" ] || die "rendered bundle is missing $file"
done
[ -x "$EXTRACTED/frisket-smoke" ] || die "rendered frisket-smoke is not executable"
[ -x "$EXTRACTED/frisket-install" ] || die "rendered frisket-install is not executable"
[ -x "$EXTRACTED/frisket-deployment-diagnostics" ] \
  || die "rendered frisket-deployment-diagnostics is not executable"

# The release renderer must leave no mutable tag in any image position.
while IFS='=' read -r key value; do
  case "$key" in
    FRISKET_IMAGE|POSTGRES_IMAGE|CADDY_IMAGE)
      printf '%s' "$value" | grep -Eq '^[^[:space:]]+@sha256:[0-9a-f]{64}$' \
        || die "$key is not an immutable digest reference"
      if [ "$key" = FRISKET_IMAGE ]; then
        CLEANUP_IMAGE=$value
      fi
      ;;
    '') ;;
    *) die "unexpected release.env key: $key" ;;
  esac
done < "$EXTRACTED/deploy/release/release.env"
[ -n "$CLEANUP_IMAGE" ] || die "release.env is missing FRISKET_IMAGE"

# The standalone frisket-release-pins.env asset (published beside this
# bundle so a downstream overlay can re-pin without extracting the whole
# tarball) must be the exact same bytes as the bundled release.env above --
# guards the split-brain failure mode where the two copies are rendered
# separately and quietly diverge.
PIN_ASSET="$(dirname -- "$BUNDLE")/frisket-release-pins.env"
[ -f "$PIN_ASSET" ] || die "missing standalone pin asset: $PIN_ASSET"
cmp -s "$PIN_ASSET" "$EXTRACTED/deploy/release/release.env" \
  || die "standalone $PIN_ASSET is not byte-identical to the bundled deploy/release/release.env"

# Exercise the exact installer shipped in the archive. The guarded test-root
# hook changes only the fixed /srv/frisket destination; package selection,
# host preflight, digest pulls, generated config/marker integrity, Compose
# startup, and readiness are the production paths.
FRISKET_INSTALL_ARTIFACT_DIR=$EXTRACTED/deploy/release \
FRISKET_INSTALL_TEST_ROOT=$RUNTIME \
FRISKET_INSTALL_ALLOW_TEST_ROOT=1 \
FRISKET_INSTALL_READY_SECONDS=180 \
  "$EXTRACTED/frisket-install" \
    --package "$PACKAGE" \
    --tunnel \
    --yes
runtime_project_owned \
  || die "installer returned success without an owned runtime Compose project"

deadline=$(( $(date +%s) + 180 ))
setup_code=
while [ "$(date +%s)" -lt "$deadline" ]; do
  setup_code=$(
    cd "$RUNTIME" && \
      docker compose -p "$PROJECT" -f compose.yaml logs --no-color app 2>/dev/null | \
      awk '{for (i=1; i<=NF; i++) if ($i == "FRISKET_SETUP_CODE") print $(i+1)}' | \
      tail -n 1
  )
  [ -z "$setup_code" ] || break
  sleep 1
done
[ -n "$setup_code" ] || die "fresh $PACKAGE app emitted no setup code within 180s"

password=$(python3 -c 'import secrets; print("Frisket-smoke-" + secrets.token_urlsafe(24))')
state_file=$SMOKE_ROOT/state.json
FRISKET_SMOKE_SETUP_CODE=$setup_code \
FRISKET_SMOKE_PASSWORD=$password \
  "$EXTRACTED/frisket-smoke" seed \
    --base-url http://localhost:8000 \
    --state-file "$state_file" \
    --ready-timeout 180 \
    --action-timeout 180

# Recreate the complete selected service graph, not just the web process. Bind
# data remains at the installer-owned temporary root across container removal.
(cd "$RUNTIME" && docker compose -p "$PROJECT" -f compose.yaml down --remove-orphans --timeout 20)
(cd "$RUNTIME" && docker compose -p "$PROJECT" -f compose.yaml up -d)
FRISKET_SMOKE_PASSWORD=$password \
  "$EXTRACTED/frisket-smoke" verify \
    --base-url http://localhost:8000 \
    --state-file "$state_file" \
    --ready-timeout 180

printf '%s\n' "release-bundle-smoke: $PACKAGE installed-bundle persistence proof passed"
