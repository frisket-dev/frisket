#!/usr/bin/env bash
# Playwright backend. Seeding makes paid model calls, so isolated runs publish
# an initial seed to the persistent base cache.
set -euo pipefail
cd "$(dirname "$0")/../.."
BASE_WS="${FRISKET_E2E_BASE_WS:-$HOME/.frisket/e2e-ws}"
WS="${FRISKET_E2E_WS:-$BASE_WS}"
PORT="${FRISKET_E2E_BACKEND_PORT:-8000}"
FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-${XDG_CACHE_HOME:-$HOME/.cache}/frisket/fastembed}"
export FASTEMBED_CACHE_PATH
mkdir -p "$FASTEMBED_CACHE_PATH"
mkdir -p "$WS"
if [ "${FRISKET_E2E_ISOLATED:-0}" = "1" ] && [ -d "$BASE_WS" ] && [ "$WS" != "$BASE_WS" ]; then
  seed_lock="$BASE_WS/.seed-publish.lock"
  for _ in {1..60}; do
    if [ ! -d "$seed_lock" ]; then break; fi
    sleep 1
  done
  if [ -d "$seed_lock" ]; then
    echo "Timed out waiting for e2e seed publish lock: $seed_lock" >&2
    exit 1
  fi
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude '.queue.db*' --exclude '.frisket-e2e-active.json' "$BASE_WS"/ "$WS"/
  else
    (cd "$BASE_WS" && tar --exclude='.queue.db*' --exclude='.frisket-e2e-active.json' -cf - .) | (cd "$WS" && tar -xf -)
  fi
fi
# Persist seeded project data, but never persist job-queue state between e2e
# runs. A stale queue makes fresh tests wait behind orphaned work and surfaces
# as misleading "running 0/N rows" UI timeouts.
rm -f "$WS/.queue.db" "$WS/.queue.db-wal" "$WS/.queue.db-shm"
if [ -f .secrets/frisket.env ]; then set -a; . .secrets/frisket.env; set +a; fi
# Credential-gate specs (action-api-key-gate.spec.ts) assert the
# needs-credential banner for actions whose key is UNSET. Real dev keys from
# .secrets/frisket.env would satisfy resolve_credential (env wins over project
# secrets, src/frisket/credentials.py) and silently break that premise, so
# gate-tested credentials never reach the e2e backend. Provider model keys
# stay: the seeder records real model calls.
unset CENSUS_API_KEY FEC_API_KEY
if [ "${FRISKET_E2E_MINIMAL_DEPS:-0}" = "1" ]; then
  # Preserve the preceding keyless `uv sync --no-dev`; syncing here would
  # silently restore dev groups and local-E2E extras.
  exec uv run --no-sync frisket "$WS" "$PORT"
fi

# Default E2E covers the FollowTheMoney and graph-neighborhood surfaces.
exec uv run --extra entities frisket "$WS" "$PORT"
