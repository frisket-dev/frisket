import type { PlaywrightTestConfig } from '@playwright/test';
import { TEAM_SPEC_PATTERNS } from './playwright.edition-specs';
import { teamStack } from './playwright.team-stack';

const stack = teamStack();

// These specs ONLY exist under capabilities.team/capabilities.identity.
// They are red under the
// local-only harness (web/playwright/playwright.config.ts forces FRISKET_EDITION=local,
// where those routes/settings are structurally unreachable) — this
// config boots the real single-org team ASGI entrypoint instead
// (frisket.team.asgi:app, via playwright.team-stack.ts) so they can run.
// A type-only import (erased at build time, see `import type` above) plus a
// plain config object — NOT `defineConfig(...)` — deliberately: this exact
// file can be imported directly (not via the playwright CLI) and bundled with
// `--bundle --platform=node --format=esm` and no `--external` flags. A runtime
// import from `@playwright/test` (even just
// `defineConfig`/`devices`) pulls playwright-core's browser-launch machinery
// (chromium-bidi, fsevents.node) into that bundle and fails to resolve.
// `defineConfig` is an identity function for a single config argument (see
// node_modules/playwright/lib/common/index.js), so a plain object of the
// same shape is equivalent for the real `playwright test` CLI too.
const config: PlaywrightTestConfig = {
  testDir: '../tests/e2e',
  outputDir: '../test-results',
  testMatch: [/team-auth\.setup\.ts/, ...TEAM_SPEC_PATTERNS],
  workers: 1,
  fullyParallel: false,
  timeout: 90_000,
  expect: { timeout: 10_000 },
  retries: 0,
  reporter: [['list'], ['json', { outputFile: stack.reportPath }]],
  use: {
    baseURL: stack.baseURL,
    actionTimeout: 8_000,
    navigationTimeout: 20_000,
    trace: 'off',
    screenshot: 'only-on-failure',
  },
  projects: [
    // team-auth seeds a real authenticated admin session (storageState) once
    // per run; admin-users and audit-log-viewer need it (they navigate
    // straight to /admin without mocking /api/me themselves). See
    // tests/e2e/team-auth.setup.ts.
    { name: 'team-seed', testMatch: /team-auth\.setup\.ts/ },
    {
      name: 'team-admin',
      testMatch: TEAM_SPEC_PATTERNS,
      use: { viewport: { width: 1600, height: 900 }, storageState: stack.storageStatePath },
      dependencies: ['team-seed'],
    },
  ],
  webServer: stack.webServer,
};

export default config;
