import { expect, test } from '@playwright/test';
import { createProject, projectIdByName, uniqueName } from './helpers';

test('global settings hide project-only nav and use the project switcher to enter project settings', async ({
  page,
}) => {
  const pid = await projectIdByName(page.request, 'Local stories');

  await page.goto('/settings/personal/profile');

  await expect(page.getByTestId('settings-workspace')).toBeVisible();
  await expect(page.getByTestId('settings-section-personal-profile')).toBeVisible();
  await expect(page.locator('[data-testid^="settings-nav-project-"]')).toHaveCount(0);

  const switcher = page.getByTestId('settings-project-switcher');
  await expect(switcher).toBeVisible();
  await expect(switcher).toBeEnabled();

  await switcher.selectOption(pid);

  await expect(page).toHaveURL(new RegExp(`/p/${pid}/settings/project/general(?:\\?|$)`));
  await expect(page.getByTestId('settings-section-project-general')).toContainText('Local stories');
  await expect(page.getByTestId('settings-nav-project-general')).toBeVisible();
  await expect(page.getByTestId('workbench-shell')).toHaveCount(0);
});

test('project settings switcher changes the project while preserving the project settings section', async ({
  page,
}) => {
  const sourceProjectId = await projectIdByName(page.request, 'Local stories');
  const targetProjectName = uniqueName('settings-switcher-target');
  const targetProjectId = await createProject(page.request, targetProjectName);

  await page.goto(`/p/${sourceProjectId}/settings/project/data-management`);

  await expect(page.getByTestId('settings-section-project-data-management')).toBeVisible();
  await expect(page.getByTestId('settings-project-switcher')).toHaveValue(sourceProjectId);

  await page.getByTestId('settings-project-switcher').selectOption(targetProjectId);

  await expect(page).toHaveURL(
    new RegExp(`/p/${targetProjectId}/settings/project/data-management(?:\\?|$)`),
  );
  await expect(page.getByTestId('settings-project-switcher')).toHaveValue(targetProjectId);
  await expect(page.getByTestId('settings-context-name')).toContainText(targetProjectName);
  await expect(page.getByTestId('settings-section-project-data-management')).toBeVisible();
  await expect(page.getByTestId('workbench-shell')).toHaveCount(0);
});
