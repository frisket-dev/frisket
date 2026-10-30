# Documentation captures

Run the documentation Playwright spec using the existing local-stack config from
`web/`. Set `FRISKET_DOCS_PROOF_DIR` to a new writable artifact directory,
`FRISKET_DOCS_REVISION` to the exact committed application revision, and
`FRISKET_E2E_PYTHON` to the environment's Python interpreter. The Playwright
stack sets `FRISKET_E2E_WS` for its disposable workspace. Use one worker and a
network-denied environment; do not expose provider credentials. Set
`FRISKET_CACHE_MODE=replay_strict` and a non-secret placeholder Gemini key so the
normal selector can choose the recorded model. A capture must not be promoted
unless the complete guide bundle, settings, actual values and revision have been
verified together.

```sh
npx playwright test tests/e2e/docs/recipes.spec.ts --config playwright/playwright.config.ts --project chromium --no-deps --workers=1 --retries=0
```

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
