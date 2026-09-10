import { describe, expect, it, vi } from 'vitest';

vi.mock('../../playwright/playwright.local-stack', () => ({
  localStack: () => ({
    baseURL: 'http://frisket.test',
    webServer: [],
  }),
}));

import config from '../../playwright/playwright.config';

describe('paid seed retry policy', () => {
  it('disables retries only for seed setup', () => {
    const seed = config.projects?.find((project) => project.name === 'seed');
    const chromium = config.projects?.find((project) => project.name === 'chromium');

    expect(config.retries).toBe(1);
    expect(seed?.retries).toBe(0);
    expect(chromium?.retries).toBeUndefined();
  });
});
