// An import that lands media links before anything has downloaded them offers
// "Download media?", never "Transcribe these?": transcription needs a media
// column, which this import has not produced. Pasted rows take the same table
// import path as a CSV file.

import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openImportWorkspace, uniqueName } from './helpers';

async function pasteAndImport(page: Page, header: string, values: string[]): Promise<void> {
  await openImportWorkspace(page);
  const add = page.getByRole('region', { name: 'Add data', exact: true });
  await add.getByRole('button', { name: 'Paste rows', exact: true }).click();
  await add.getByRole('textbox', { name: 'Paste rows' }).fill([header, ...values].join('\n'));
  await add.getByRole('button', { name: 'Check rows →', exact: true }).click();
  await expect(page.getByRole('region', { name: 'Check rows', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Columns →', exact: true }).click();
  await expect(page.getByRole('table', { name: 'Columns to import' })).toBeVisible();
  await page.getByRole('button', { name: 'Continue →', exact: true }).click();
  const destination = page.locator('.table-import-flow').getByRole('region', { name: 'Import', exact: true });
  await expect(destination).toBeVisible();
  await destination.getByRole('button', { name: /^Import(?: |$)/ }).and(page.locator('.btn-primary')).click();
}

test('an import landing undownloaded YouTube URL rows prompts "Download media?"; Download closes and launches the matching action', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-media-download-prompt-run'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await pasteAndImport(
    page,
    'video_url',
    ['https://youtube.com/watch?v=abc123', 'https://youtube.com/watch?v=def456'],
  );

  // The dialog stays open, showing the download prompt — never a transcribe
  // prompt (no media column exists yet inside this same dialog turn).
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  const prompt = page.getByTestId('media-download-prompt');
  await expect(prompt).toBeVisible();
  await expect(prompt).toContainText('Download media?');
  await expect(prompt).toContainText('video_url');
  await expect(prompt).not.toContainText(/transcribe/i);
  await expect(page.getByTestId('media-download-run')).toBeVisible();
  await expect(page.getByTestId('media-download-dismiss')).toBeVisible();

  await page.getByTestId('media-download-run').click();

  // Closes the import dialog, then routes to the download action
  // (download_media, per compatibleSourceColumns) prefilled with the
  // detected URL column — the same runActionFromSurface(kind, column) shape
  // the caret menu uses.
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();
  await expect(page).toHaveURL(/\/action\/download_media(?:\?|$)/);
  const drawer = page.getByTestId('action-drawer');
  await expect(drawer).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('media-source-column-select')).toHaveValue('video_url');
});

test('"Not now" declines quietly — the dialog closes with no action launched', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-media-download-prompt-dismiss'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  const runRequests: string[] = [];
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as { kind?: string; action_id?: string };
    const actionId = payload?.action_id ?? payload?.kind;
    if (actionId) runRequests.push(actionId);
    await route.continue();
  });

  await pasteAndImport(page, 'video_url', ['https://youtube.com/watch?v=abc123']);

  const prompt = page.getByTestId('media-download-prompt');
  await expect(prompt).toBeVisible();
  await page.getByTestId('media-download-dismiss').click();

  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();
  await expect(page.getByTestId('action-drawer')).toHaveCount(0);
  expect(runRequests).toEqual([]);
});
