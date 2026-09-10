import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';

import { localStack } from '../../playwright/playwright.local-stack';

const originalEnv = { ...process.env };

afterEach(() => {
  for (const name of Object.keys(process.env)) {
    if (!(name in originalEnv)) delete process.env[name];
  }
  Object.assign(process.env, originalEnv);
});

describe('local Playwright stack edition', () => {
  it('overrides an inherited team edition in the process and frontend command', () => {
    const runRoot = mkdtempSync(path.join(os.tmpdir(), 'frisket-local-stack-edition-'));
    try {
      process.env.FRISKET_EDITION = 'team';
      process.env.FRISKET_E2E_FIXED_PORTS = '1';
      process.env.FRISKET_E2E_FRONTEND_PORT = '45173';
      process.env.FRISKET_E2E_BACKEND_PORT = '48000';
      process.env.FRISKET_E2E_BASE_WS = path.join(runRoot, 'base');
      process.env.FRISKET_E2E_WS = path.join(runRoot, 'run');
      process.env.FRISKET_E2E_RUN_ID = 'hostile-team-env';

      const stack = localStack();

      expect(process.env.FRISKET_EDITION).toBe('local');
      expect(stack.webServer[1].command).toContain(
        "FRISKET_EDITION=local FRISKET_BACKEND_URL='http://127.0.0.1:48000'",
      );
    } finally {
      rmSync(runRoot, { recursive: true, force: true });
    }
  });
});
