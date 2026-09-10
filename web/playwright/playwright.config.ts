import { defineConfig, devices } from '@playwright/test';
import { TEAM_SPEC_PATTERNS } from './playwright.edition-specs';
import { localStack } from './playwright.local-stack';

const configuredWorkers = Number(process.env.FRISKET_E2E_WORKERS ?? '2');
const workers = Number.isFinite(configuredWorkers) && configuredWorkers > 0 ? configuredWorkers : 1;
const stack = localStack();

// E2E suite against a LIVE local stack:
//   backend  uv run frisket <workspace> <dynamic-port>
//   frontend vite on a matching dynamic port, /api proxied to that backend
// Some specs make real (cheap) model calls; this suite is local-only, not CI.
export default defineConfig({
  testDir: '../tests/e2e',
  outputDir: '../test-results',
  // Mutating specs each create their own project, so parallelism is safe. The
  // self-booted backend starts one local recipe worker, so keep the default
  // modest and allow FRISKET_E2E_WORKERS=1 when investigating queue timing.
  workers,
  fullyParallel: true,
  // Keep failures crisp. Live model assertions must opt into explicit waits;
  // ordinary missing UI should fail quickly instead of consuming minutes.
  timeout: 90_000,
  expect: { timeout: 10_000 },
  retries: 1,
  reporter: [['list'], ['./playwright.checklog-reporter.mjs']],
  use: {
    baseURL: stack.baseURL,
    actionTimeout: 8_000,
    navigationTimeout: 20_000,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    // media.spec plays audio programmatically; headless Chromium blocks
    // autoplay without this flag. The swiftshader flag lets deck.gl's WebGL
    // (deckgl-map.spec) render under software GL in headless Chromium, which
    // recent builds gate behind --enable-unsafe-swiftshader.
    launchOptions: {
      args: [
        '--autoplay-policy=no-user-gesture-required',
        '--enable-unsafe-swiftshader',
        '--ignore-gpu-blocklist',
      ],
    },
  },
  projects: [
    // Seeding runs first and can spend real model money. Never retry the whole
    // setup after a later infrastructure or fixture failure.
    { name: 'seed', testMatch: /seed\.setup\.ts/, retries: 0 },
    {
      name: 'chromium',
      testIgnore: TEAM_SPEC_PATTERNS,
      // The Workbench IA redesign adds resident right-edge panels (Inspect
      // Detail + Discover, workbench-ia-right-edge-v1). Keep a desktop-width
      // viewport so the Work grid stays usable — and coordinate-based cell
      // clicks reach columns past the first — with all regions open. Specs that
      // exercise narrow/responsive behavior set their own viewport.
      use: { ...devices['Desktop Chrome'], viewport: { width: 1600, height: 900 } },
      dependencies: ['seed'],
    },
  ],
  // SELF-BOOTING stack: playwright starts backend + vite itself, so
  // `npm run e2e -- <spec>` works anywhere with zero hand-started processes.
  webServer: stack.webServer,
});
