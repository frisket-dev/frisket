import { describe, expect, it, vi } from 'vitest';

vi.mock('../../playwright/playwright.local-stack', () => ({
  localStack: () => ({ baseURL: 'http://local.frisket.test', webServer: [] }),
}));
vi.mock('../../playwright/playwright.team-stack', () => ({
  teamStack: () => ({
    baseURL: 'http://team.frisket.test',
    reportPath: '/tmp/team-report.json',
    storageStatePath: '/tmp/team-auth.json',
    webServer: [],
  }),
}));

import localConfig from '../../playwright/playwright.config';
import {
  TEAM_SPEC_BASENAMES,
  TEAM_SPEC_PATTERNS,
} from '../../playwright/playwright.edition-specs';
import teamConfig from '../../playwright/playwright.team.config';

describe('Playwright edition selection', () => {
  it('assigns every team-only spec to team and excludes it from local Chromium', () => {
    const local = localConfig.projects?.find((project) => project.name === 'chromium');
    const team = teamConfig.projects?.find((project) => project.name === 'team-admin');

    expect(local?.testIgnore).toBe(TEAM_SPEC_PATTERNS);
    expect(team?.testMatch).toBe(TEAM_SPEC_PATTERNS);
    for (const basename of TEAM_SPEC_BASENAMES) {
      expect(TEAM_SPEC_PATTERNS.some((pattern) => pattern.test(basename))).toBe(true);
    }
  });

  it('keeps the local account profile spec out of the team manifest', () => {
    expect(TEAM_SPEC_BASENAMES).not.toContain('account.spec.ts');
  });
});
