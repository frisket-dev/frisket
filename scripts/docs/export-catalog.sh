#!/usr/bin/env bash
# Export the served catalog through the frontend presentation projection.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
python=${FRISKET_DOCS_PYTHON:-python3}
temporary=$(mktemp -d)
trap 'rm -r -- "$temporary"' EXIT
"$root/web/node_modules/.bin/esbuild" "$root/web/scripts/export-docs-catalog.ts" \
  --bundle --platform=node --format=esm --outfile="$temporary/catalog.mjs" >&2
PYTHONPATH="$root/src${PYTHONPATH:+:$PYTHONPATH}" "$python" \
  "$root/scripts/ci/gen_action_presentation_catalog_fixture.py" | node "$temporary/catalog.mjs"
