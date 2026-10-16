import { expect, test } from '@playwright/test';
import type { ModelPullDto } from '../../src/api/types';
import { createProject, importCsv, openPalette, openProject, textPdf, uniqueName } from './helpers';

test.use({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });

test('project search prepares the missing PDF sheet while a text sheet is open', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('search-gap-scope'));
  const textSheet = await importCsv(page.request, pid, 'dispatches.csv', 'story\nHarbor meeting\n');
  const settings = await page.request.patch(`/api/projects/${pid}/settings`, {
    data: { retrieval: { prepare_new_files_automatically: false } },
  });
  expect(settings.ok()).toBeTruthy();
  const imported = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=Documents`, {
    multipart: { files: {
      name: 'harbor.pdf', mimeType: 'application/pdf',
      buffer: textPdf([['Harbor report', 'The bluewharf contract covers dredging.']]),
    } },
  });
  expect(imported.ok()).toBeTruthy();
  const { sheet_id: documentSheet } = await imported.json() as { sheet_id: number };
  await openProject(page, pid, textSheet);
  const palette = await openPalette(page);
  await palette.getByTestId('search-mode-select').selectOption('lexical');
  await palette.getByTestId('command-palette-input').fill('bluewharf');
  const prepare = palette.getByTestId('search-preparation-gap-action');
  await expect(prepare).toContainText('Documents');
  const admitted = page.waitForResponse((response) => (
    response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/projects/${pid}/search-preparation`
  ));
  await prepare.click();
  const response = await admitted;
  expect(response.ok()).toBeTruthy();
  expect(response.request().postDataJSON()).toEqual({ sheet_id: documentSheet });
  const batch = await response.json() as { id: string };
  await expect.poll(async () => {
    const result = await page.request.get(`/api/projects/${pid}/search-preparation/${batch.id}`);
    const body = await result.json() as { status: string; groups: Array<{ count: number }> };
    expect(body.groups.reduce((sum, group) => sum + group.count, 0)).toBeGreaterThan(0);
    return body.status;
  }, { timeout: 30_000 }).toBe('complete');
  await page.getByTestId('search-preparation-done').click();
  await expect(page.getByTestId('search-preparation-receipt')).toBeHidden();
  // Closing and reopening searches the real index, not a mocked success response.
  await page.keyboard.press('Escape');
  const reopened = await openPalette(page);
  await reopened.getByTestId('command-palette-input').fill('bluewharf');
  await expect(reopened.getByTestId('search-hit').filter({ hasText: 'bluewharf' }).first())
    .toBeVisible({ timeout: 30_000 });
});

test('semantic setup keeps download progress and errors visible after closing search', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('search-download-progress'));
  const sheet = await importCsv(page.request, pid, 'stories.csv', 'story\nHarbor repair contract\n');
  let started = false;
  let pull: ModelPullDto = {
    schemaVersion: 'frisket.model_pull.v4', id: 9876,
    model: 'engine-setup:embeddinggemma-2.local@1',
    status: 'pending', phase: 'Queued', total_bytes: 1_400_000_000, completed_bytes: 0,
    error: null, resolved_digest: null, resolved_size: null,
    created_at: new Date().toISOString(), started_at: null, finished_at: null,
    cancel_requested: false, endpoint_id: null, endpoint_origin: null, initiated_by: null,
    artifact: null, operation_kind: 'engine_setup', display_name: 'EmbeddingGemma 2',
    capabilities: { cancel: true, retry: false, remove: false },
  };
  // Only the large external model download is simulated; the workspace and UI are real.
  await page.route('**/api/providers/models/setup', async (route) => {
    started = true;
    await route.fulfill({ status: 202, json: { pull, deduplicated: false } });
  });
  await page.route('**/api/providers/models/pulls', (route) => (
    route.fulfill({ json: { pulls: started ? [pull] : [] } })
  ));
  await page.route('**/api/providers/models/pulls/9876', (route) => route.fulfill({ json: pull }));
  await openProject(page, pid, sheet);
  const palette = await openPalette(page);
  await palette.getByTestId('command-palette-input').fill('harbor');
  await palette.getByTestId('search-setup').click();
  await palette.getByRole('button', { name: 'Download and set up' }).click();
  await expect(palette.getByTestId('model-pull-progress')).toHaveAttribute('data-status', 'pending');
  pull = { ...pull, status: 'running', phase: 'Downloading', completed_bytes: 350_000_000 };
  await expect(palette.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '25');
  await page.keyboard.press('Escape');
  await expect(palette).toBeHidden();
  const notice = page.getByTestId('search-preparation-banner');
  await expect(notice).toContainText('EmbeddingGemma 2');
  await expect(notice.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '25');
  const reopened = await openPalette(page);
  await reopened.getByTestId('command-palette-input').fill('harbor');
  await expect(reopened.getByTestId('model-pull-progress')).toHaveAttribute('data-status', 'running');
  pull = {
    ...pull, status: 'failed', phase: null,
    error: { code: 'download_failed', message: 'The model download was interrupted.' },
    capabilities: { cancel: false, retry: true, remove: false },
  };
  await expect(reopened.getByTestId('model-pull-error')).toContainText('interrupted');
  await page.keyboard.press('Escape');
  await expect(notice).toContainText('interrupted');
});
