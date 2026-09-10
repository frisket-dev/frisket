// The registry-contract half (unique ids + admitted V1 sections; route patterns
// match routePath) lives in web/tests/unit/settingsRegistry.test.ts. This file
// asserts live app-shell routing — the settings nav renders
// exactly the registry sections, and forbidden section routes are rejected —
// which a component/unit test can't honestly express.

import { expect, test } from '@playwright/test';
import { SETTINGS_SECTIONS } from '../../src/settings/settingsRegistry';
import { projectIdByName } from './helpers';

test('settings nav renders exactly the registry sections', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  await page.goto(`/p/${pid}/settings/project/general`);
  await expect(page.getByTestId('settings-workspace')).toBeVisible();

  const navIds = await page.getByTestId('settings-nav').locator('[data-settings-section-id]')
    .evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-settings-section-id')));
  expect(navIds).toEqual(SETTINGS_SECTIONS.map((definition) => definition.id));
});

test('source schedule and action/run-option settings routes are invalid', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  for (const section of ['sources', 'schedules', 'actions', 'run-options']) {
    await page.goto(`/p/${pid}/settings/project/${section}`);
    await expect(page.getByTestId('settings-invalid-section')).toBeVisible();
    await expect(page.getByTestId(`settings-section-project-${section}`)).toHaveCount(0);
    await expect(page.getByTestId('workbench-shell')).toHaveCount(0);
  }
});
