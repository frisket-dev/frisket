import { expect, test } from '@playwright/test';
import { createProject, uniqueName } from './helpers';

async function openFromImport(page: import('@playwright/test').Page): Promise<void> {
  await page.getByTestId('ribbon-tab-data').click();
  await page.getByTestId('ribbon-command-sources').click();
}

test('Sources & connections opens an obvious focused management modal', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-sources-home'));
  await page.goto(`/p/${pid}`);

  await openFromImport(page);

  const dialog = page.getByTestId('sources-connections-dialog');
  await expect(dialog).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Sources & connections' })).toBeVisible();
  await expect(page.getByText('Manage recurring feeds, polling, and connection health')).toBeVisible();
  await expect(page.getByTestId('sources-panel')).toBeVisible();
  await expect(page.getByTestId('sources-empty')).toBeVisible();
  await expect(page.getByTestId('sources-connections-close')).toBeFocused();

  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);

  // The detached project menu reaches the same home instead of subtly
  // changing a tab in the resident Discover panel.
  await page.getByTestId('switch-project').click();
  await page.getByTestId('project-sources-open').click();
  await expect(page.getByTestId('sources-connections-dialog')).toBeVisible();
  await expect(page.getByTestId('sources-connections-close')).toBeFocused();
});

test('Add source reuses Import Feed and returns to the manager', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-sources-home-add'));
  await page.goto(`/p/${pid}`);
  await openFromImport(page);

  await page.getByTestId('sources-add-source').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  await expect(page.getByTestId('import-mode-feed')).toHaveAttribute('aria-selected', 'true');

  await page.getByTestId('import-workspace-close').click();
  await expect(page.getByTestId('import-workspace-dialog')).toHaveCount(0);
  await expect(page.getByTestId('sources-connections-dialog')).toBeVisible();
  await expect(page.getByTestId('sources-add-source')).toBeVisible();
});
