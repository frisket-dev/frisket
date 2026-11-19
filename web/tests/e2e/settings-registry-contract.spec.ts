// The registry contract (unique ids, admitted sections, route patterns) lives in
// web/tests/unit/settingsRegistry.test.ts. This file asserts live app-shell
// routing: forbidden section routes are rejected.

import { expect, test } from '@playwright/test';
import { createProject, uniqueName } from './helpers';

test('source schedule and action/run-option settings routes are invalid', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-settings-routes'));
  for (const section of ['sources', 'schedules', 'actions', 'run-options']) {
    await page.goto(`/p/${pid}/settings/project/${section}`);
    await expect(page.getByTestId('settings-invalid-section')).toBeVisible();
    await expect(page.getByTestId(`settings-section-project-${section}`)).toHaveCount(0);
    await expect(page.getByTestId('workbench-shell')).toHaveCount(0);
  }
});
