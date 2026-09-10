import { expect, test } from '@playwright/test';
import { mockHostedSettingsShell, type SettingsProjectFixture } from './settingsTestHelpers';

test('Project General owns identity/delete and Data Management owns exports retention and compaction', async ({ page }) => {
  const project: SettingsProjectFixture = {
    id: 'alpha',
    name: 'Alpha Project',
    description: 'Field reporting workspace',
    sensitive: true,
    role: 'owner',
  };
  await mockHostedSettingsShell(page, [project]);

  let deleted = false;
  let compacted = false;
  let retention = {
    schemaVersion: 'frisket.project_retention_policy.v1',
    default_evidence: 'compactable',
    pin_evidence_by_default: false,
    no_compact: false,
    supported_default_evidence: ['compactable', 'pinned', 'materialized'],
  };

  await page.route(/\/api\/projects\/alpha(?:\?.*)?$/, async (route) => {
    if (route.request().method() === 'DELETE') {
      deleted = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ ok: true, deleted: 'alpha' }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(project),
    });
  });
  await page.route('**/api/projects/alpha/retention', async (route) => {
    if (route.request().method() === 'PATCH') {
      retention = { ...retention, ...(route.request().postDataJSON() as Partial<typeof retention>) };
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(retention) });
  });
  await page.route('**/api/projects/alpha/settings', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        media_allow_private_hosts: false,
        media_allow_private_hosts_locked: false,
      }),
    });
  });
  await page.route('**/api/projects/alpha/network', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schemaVersion: 'frisket.project_network_policy.v1',
        mode: 'inherit',
        org_default: null,
        effective: 'on',
      }),
    });
  });
  await page.route('**/api/projects/alpha/compact', async (route) => {
    compacted = true;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        skipped: false,
        reason: null,
        results_pruned: 2,
        blobs_removed: 1,
        bytes_freed: 256,
        db_bytes_before: 4096,
        db_bytes_after: 3072,
        db_bytes_reclaimed: 1024,
      }),
    });
  });

  await page.goto('/p/alpha/settings/project/general');

  const general = page.getByTestId('project-general-settings');
  await expect(general).toBeVisible();
  await expect(general.getByTestId('export-project-with-media')).toHaveCount(0);
  await expect(general.getByText('Deleting this project permanently removes its sheets, files, run history, and settings.')).toBeVisible();
  const deleteButton = general.getByTestId('project-delete-button');
  await expect(deleteButton).toHaveText(/Delete/);
  await expect(deleteButton).toBeDisabled();
  await general.getByTestId('project-delete-confirm-input').fill('Alpha');
  await expect(deleteButton).toBeDisabled();
  await general.getByTestId('project-delete-confirm-input').fill('Alpha Project');
  await expect(deleteButton).toBeEnabled();

  await page.goto('/p/alpha/settings/project/data-management');

  const data = page.getByTestId('project-data-management-settings');
  await expect(data.getByTestId('export-project-with-media')).toHaveAttribute(
    'href',
    '/api/projects/alpha/export?mode=bundle&include_media=true&include_traces=false',
  );
  await expect(data.getByTestId('export-project-without-media')).toHaveAttribute(
    'href',
    '/api/projects/alpha/export?mode=bundle&include_media=false&include_traces=false',
  );
  await expect(data.getByTestId('export-project-database')).toHaveAttribute(
    'href',
    '/api/projects/alpha/export?mode=db',
  );
  await expect(data.getByText('Includes project data and stored media files.')).toBeVisible();
  await expect(data.getByText('SQLite database snapshot for backup or inspection.')).toBeVisible();
  await expect(data.getByText('Pin evidence by default')).toHaveCount(0);

  const evidenceDefault = data.getByTestId('project-evidence-default');
  await expect(evidenceDefault.locator('option')).toHaveText([
    'Compactable',
    'Pinned',
    'Materialized',
  ]);
  await evidenceDefault.click();
  const evidenceMenu = page.getByTestId('project-evidence-default-menu');
  await expect(evidenceMenu).toContainText(
    'Manual compaction may remove supporting run evidence; sheet results stay.',
  );
  await expect(evidenceMenu).toContainText(
    'Keep supporting run evidence when this project is manually compacted.',
  );
  await expect(evidenceMenu).toContainText(
    'Keep output-linked evidence as part of the stored result.',
  );
  await evidenceMenu.getByRole('option', { name: /Pinned/ }).click();
  await expect(page.getByText('Retention policy saved')).toBeVisible();
  expect(retention.default_evidence).toBe('pinned');

  const compactButton = data.getByTestId('compact-project-button');
  await expect(data.getByText(/Free disk space by permanently deleting old run data/)).toBeVisible();
  await expect(compactButton).toBeDisabled();
  await data.getByTestId('compact-confirm-input').fill('COMPACT');
  await expect(compactButton).toBeEnabled();
  await compactButton.click();
  await expect(page.getByText('Compaction reclaimed 1024 bytes and removed 1 blob.')).toBeVisible();
  expect(compacted).toBe(true);

  await page.goto('/p/alpha/settings/project/general');
  await page.getByTestId('project-delete-confirm-input').fill('Alpha Project');
  await page.getByTestId('project-delete-button').click();
  await expect.poll(() => deleted).toBe(true);
});
