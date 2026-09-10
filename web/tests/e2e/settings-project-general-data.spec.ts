import { expect, test } from '@playwright/test';
import { mockHostedSettingsShell, type SettingsProjectFixture } from './settingsTestHelpers';

test('Project General and Data Management settings update metadata retention and compaction', async ({ page }) => {
  const project: SettingsProjectFixture = {
    id: 'alpha',
    name: 'Alpha Project',
    description: 'Field reporting workspace',
    sensitive: true,
    role: 'owner',
  };
  await mockHostedSettingsShell(page, [project]);

  let projectDetails = {
    id: project.id,
    name: project.name,
    description: project.description ?? '',
    sensitive: project.sensitive ?? false,
    starred: false,
    archived: false,
  };
  let retention = {
    schemaVersion: 'frisket.project_retention_policy.v1',
    default_evidence: 'compactable',
    pin_evidence_by_default: false,
    no_compact: false,
    supported_default_evidence: ['compactable', 'pinned', 'materialized'],
  };
  let compacted = false;

  await page.route('**/api/projects/alpha', async (route) => {
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON() as { name?: string; description?: string };
      projectDetails = {
        ...projectDetails,
        name: body.name ?? projectDetails.name,
        description: body.description ?? projectDetails.description,
      };
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(projectDetails) });
  });
  await page.route('**/api/projects/alpha/retention', async (route) => {
    if (route.request().method() === 'PATCH') {
      retention = { ...retention, ...(route.request().postDataJSON() as Partial<typeof retention>) };
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(retention) });
  });
  await page.route('**/api/projects/alpha/compact', async (route) => {
    compacted = true;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, project_id: 'alpha' }),
    });
  });

  await page.goto('/p/alpha/settings/project/general');

  const general = page.getByTestId('project-general-settings');
  await expect(general).toContainText('Sensitive');
  await expect(general).toContainText('Yes');
  await page.getByTestId('project-name-input').fill('Alpha Reports');
  await page.getByTestId('project-description-input').fill('Updated description');
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(page.getByText('Project saved')).toBeVisible();
  expect(projectDetails.name).toBe('Alpha Reports');
  expect(projectDetails.description).toBe('Updated description');

  await page.goto('/p/alpha/settings/project/data-management');

  const data = page.getByTestId('project-data-management-settings');
  await expect(data).toBeVisible();
  await data.getByTestId('project-evidence-default').selectOption('materialized');
  await expect(page.getByText('Retention policy saved')).toBeVisible();
  expect(retention.default_evidence).toBe('materialized');
  await data.getByTestId('compact-confirm-input').fill('COMPACT');
  await data.getByTestId('compact-project-button').click();
  await expect(page.getByText('Compaction reclaimed 0 bytes and removed 0 blobs.')).toBeVisible();
  expect(compacted).toBe(true);
  await expect(page.getByTestId('workbench-shell')).toHaveCount(0);
});
