# frisket e2e suite + screenshot gallery

Both run against a **live local stack** and are deliberately **not wired into
CI**: `run.spec.ts`, `review.spec.ts`, and the gallery make real (sub-cent)
`gemini-2.5-flash` calls, which don't belong in CI yet.

## One-time setup

```sh
cd web
npm install                     # @playwright/test is a devDependency
npx playwright install chromium
```

## Boot + seed the stack

```sh
# Manual stack, only needed when you want to poke the app yourself.
cd /path/to/frisket
set -a; source .secrets/frisket.env; set +a
rm -rf /tmp/fk-e2e && uv run frisket /tmp/fk-e2e 8000

# 2. seed demo + showcase projects (live model runs, < $0.25 total;
#    near-free on re-runs thanks to the response cache)
uv run python scripts/e2e/seed_demo.py http://localhost:8000
uv run python scripts/e2e/seed_demo.py showcase http://localhost:8000 dummy

# 3. frontend dev server on :5173 (optional — playwright starts one if absent)
cd web && npm run dev
```

By default Playwright does **not** use those fixed ports. `playwright/playwright.config.ts`
self-boots a backend and Vite on probed free ports, copies the seeded base
workspace (`~/.frisket/e2e-ws`) into a UUID temp workspace, and clears only that
temp queue. That means separate agents can run `npm run e2e -- <spec>` at the
same time without fighting over `:8000`, `:5173`, or `.queue.db`.

The suite expects the copied/seeded projects (`Local stories`, `Tariff impacts`,
`Council audio`, `People mentioned`, `Faces demo`, …) to exist. On a fresh
machine, the first isolated seed run publishes the seeded temp workspace back to
the base cache under a `.seed-publish.lock`; later runs copy from that base.
Specs that mutate state (`run`, `costgate`, `review`, picker-create) each
create their own throwaway project and never touch the seeds. Set
`FRISKET_E2E_FIXED_PORTS=1` to force the old `:8000/:5173` behavior for manual
debugging.

## Run the suite

```sh
npm run e2e          # headless, the whole suite
npm run e2e:ui       # playwright UI mode
npx playwright test --config playwright/playwright.config.ts tests/e2e/grid.spec.ts   # one spec
```

Config: `playwright/playwright.config.ts` — chromium only, dynamic local baseURL by default,
trace on first retry, screenshots only on failure.

Notes:

- The grid is a canvas (glide-data-grid), so cell/header interactions are
  coordinate clicks (`tests/e2e/helpers.ts`) and cell-level assertions go
  through the row drawer's DOM.
- `costgate.spec.ts` trips the server's 402 gate with an injected Opus model
  option and then **cancels** — it never spends money.
- Live-run specs put a nonce in the prompt so the server's response cache
  can't replay them; that's what keeps the pending→complete transition
  observable (and costs a fraction of a cent per run).

## Screenshot gallery

```sh
npm run gallery
```

Walks every major view (picker, grids, drawers, recipe form, run watcher,
history, review queue, search, account, derived sheets, audio row) and writes
labeled full-page screenshots to `screenshots/gallery/`, printing a manifest
table at the end. Use it to eyeball what looks good vs. not after UI changes.

`01-signin` is only captured on the hosted tier (the local tier has no
sign-in gate and the manifest marks it skipped).
