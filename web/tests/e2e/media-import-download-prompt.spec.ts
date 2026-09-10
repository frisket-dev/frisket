// Import download-prompt contract.
//
// An import that lands `link`/`text` rows before anything has downloaded prompts
// "Download media?" — NEVER "Transcribe
// these?" (transcribe only makes sense once a real media column exists,
// which this import path hasn't produced). Modeled directly on
// feed-add-populate-prompt.spec.ts's sibling pattern. The shared
// `finishImport` unifies the three previously-independent close-on-success
// paths (confirmDraft here; the file-input/URL-submit upload callbacks are
// covered by import-surfaces.spec.ts staying green — FeedSourceForm's own
// populate-prompt flow is untouched).

import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openImportWorkspace, uniqueName } from './helpers';

async function pasteAndConfirm(page: Page, header: string, values: string[], type?: string): Promise<void> {
  await openImportWorkspace(page);
  await page.getByTestId('import-mode-paste').click();
  await page.getByTestId('import-paste-text').fill([header, ...values].join('\n'));
  await page.getByTestId('import-detect-submit').click();
  await expect(page.getByTestId('import-draft-preview')).toBeVisible();
  if (type) {
    await page.locator('[data-testid^="import-column-type-"]').selectOption(type);
  }
  await page.getByTestId('import-map-continue').click();
  await expect(page.getByTestId('import-confirm-submit')).toBeVisible();
  await page.getByTestId('import-confirm-submit').click();
}

test('an import landing undownloaded YouTube URL rows prompts "Download media?"; Download closes and launches the matching action', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-media-download-prompt-run'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await pasteAndConfirm(
    page,
    'video_url',
    ['https://youtube.com/watch?v=abc123', 'https://youtube.com/watch?v=def456'],
    'link',
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

  await pasteAndConfirm(
    page,
    'video_url',
    ['https://youtube.com/watch?v=abc123'],
    'link',
  );

  const prompt = page.getByTestId('media-download-prompt');
  await expect(prompt).toBeVisible();
  await page.getByTestId('media-download-dismiss').click();

  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();
  await expect(page.getByTestId('action-drawer')).toHaveCount(0);
  expect(runRequests).toEqual([]);
});

test('an ordinary import with no classifiable URL column closes immediately — no regression', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-media-download-prompt-ordinary'));
  await importCsv(page.request, pid, 'seed.csv', 'name\nseed\n');
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await pasteAndConfirm(page, 'city', ['Syracuse', 'Albany']);

  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();
  await expect(page.getByTestId('media-download-prompt')).toHaveCount(0);
});
