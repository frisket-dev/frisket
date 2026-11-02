# Documentation captures

Run from a disposable checkout with no `.secrets` directory, inside a
network-denied namespace with loopback available. Prepare the locked Node and
Python dependencies, Chromium, ffmpeg/ffprobe and the pinned Whisper base model
before entering that namespace. The capture must not see developer credentials.
For the backend, `FRISKET_E2E_MINIMAL_DEPS=1` selects `uv run --no-sync`;
`UV_PROJECT_ENVIRONMENT` selects the prepared Python environment. The local
stack sets `FRISKET_E2E_WS` to its disposable workspace.

From `web/`, set `CAPTURE_HOME` to an empty writable directory and `CAPTURE_VENV`
to the prepared Python environment, then run **inside that isolated namespace**:

```sh
test ! -e ../.secrets && test ! -L ../.secrets || { echo "Remove secrets from the disposable checkout" >&2; exit 1; }
env -i HOME="$CAPTURE_HOME" XDG_CACHE_HOME="$CAPTURE_HOME/cache" \
  PATH="$PATH" LANG=C.UTF-8 TZ=UTC CI=1 \
  UV_NO_SYNC=1 UV_PROJECT_ENVIRONMENT="$CAPTURE_VENV" \
  FRISKET_E2E_MINIMAL_DEPS=1 FRISKET_E2E_PYTHON="$CAPTURE_VENV/bin/python" \
  FRISKET_E2E_BASE_WS="$CAPTURE_HOME/workspace" FRISKET_E2E_WORKERS=1 \
  FRISKET_CACHE_MODE=replay_strict GEMINI_API_KEY=docs-offline-placeholder \
  OPENROUTER_API_KEY=docs-offline-placeholder \
  FRISKET_DOCS_REVISION="$(git -C .. rev-parse HEAD)" \
  FRISKET_DOCS_PROOF_DIR="$CAPTURE_HOME/captures" \
  PYTHONPATH="$PWD/tests/e2e/docs/mai_transport:$PWD/../src" \
  HF_HUB_CACHE="$CAPTURE_HOME/model-cache" \
  npx playwright test tests/e2e/docs/recipes.spec.ts --config playwright/playwright.config.ts --project chromium --no-deps --workers=1 --retries=0
```

The preflight prevents the standard backend launcher from sourcing a checkout's
`.secrets/frisket.env`. Mount the prepared model cache read-only at the indicated
path and keep browser and dependency caches available read-only. A capture must
not be promoted unless the complete guide bundle, settings, actual values and
revision have been verified together.

The court fixture contains real responses from Gemini 3.5 Flash-Lite for the
versioned sample and exact requests saved alongside it. The journey renders the
requests from the imported project and checks equality before using the router's
recorded SQLite cache. If inputs or parameters drift, record new responses;
never alter the request to force a cache hit.

For the council comparison, put `mai_transport` before `src` on `PYTHONPATH` and
use a non-secret placeholder OpenRouter key. Its narrowly scoped transport
intercepts only the exact recorded MAI request, including the selected MP3 bytes
and options, and fails on mismatch. The fixture has no authentication headers.
The local Whisper base model must already be installed in the environment's
read-only Hugging Face cache. The provider and media inputs are public sample
material. Captures are evidence of these responses, not a general accuracy claim.
