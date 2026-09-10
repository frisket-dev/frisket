// The video fit-to-height preference persists when moving to another document.
// The full-size/fit-to-height toggle began as DocumentReader-LOCAL `useState`
// (see that file's original comment); because DocumentView.tsx remounts
// DocumentReader on every document switch (`key={reader-${activeRowId}}`),
// the choice reset to 'full' every time. Fixed by promoting the toggle into
// DocumentViewState.videoFit (web/src/state/chromeStore.ts) — the SAME
// per-project persisted "view preference" home the PDF `fit`/`layout`/
// `textLayer` fields already use (workbench-ia-document-view.spec.ts's
// "persisted per project" test pins that precedent for `fit`). videoFit is
// chrome (not scratch): it is a reading PREFERENCE, not ephemeral UI/run
// state, so it belongs beside fit/layout/textLayer and survives both
// document navigation (this spec's main assertion) and reload (chrome
// state's existing persistence contract — exercised here too, the same way
// workbench-ia-document-view.spec.ts exercises it for the PDF `fit` field).

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import { addRow, createProject, openProject, sheetColumns, sheetData, uniqueName } from './helpers';

const MEDIA_DIR = path.resolve(process.cwd(), 'tests', 'fixtures', 'media');
const VIDEO_FIXTURE = readFileSync(path.join(MEDIA_DIR, 'tiny-video.mp4'));

interface VideoDocSheet {
  pid: string;
  sheetId: number;
}

/** Two video documents in one sheet (same blob-import path
 *  document-view-video-fit.spec.ts uses for its single-doc seed), so the
 *  spec can navigate list item 1 -> list item 2 within the SAME sheet and
 *  observe whether the fit choice follows. */
async function seedTwoVideoDocSheet(page: Page): Promise<VideoDocSheet> {
  const pid = await createProject(page.request, uniqueName('document-fit-persist'));
  const importRes = await page.request.post(
    `/api/projects/${pid}/import/files?sheet_name=clips`,
    {
      multipart: {
        files: {
          name: 'clip-one.mp4',
          mimeType: 'video/mp4',
          buffer: VIDEO_FIXTURE,
        },
      },
    },
  );
  expect(importRes.ok()).toBeTruthy();
  const sheets = (await (await page.request.get(`/api/projects/${pid}/sheets`)).json()) as Array<{
    id: number;
    name: string;
  }>;
  const sheet = sheets.find((candidate) => candidate.name === 'clips')!;
  expect(sheet).toBeTruthy();

  const columns = await sheetColumns(page.request, pid, sheet.id);
  const mediaColumn = columns.find((c) => c.type === 'video' || c.type === 'file')!;
  const firstPage = await sheetData(page.request, pid, sheet.id, 0, 5);
  const firstCell = firstPage.rows[0].cells[String(mediaColumn.id)] as Record<string, unknown>;
  await addRow(page.request, pid, sheet.id, {
    [mediaColumn.name]: { ...firstCell, filename: 'clip-two.mp4' },
  });

  return { pid, sheetId: sheet.id };
}

test('fit-to-height sticks when navigating to the next document', async ({ page }) => {
  const { pid, sheetId } = await seedTwoVideoDocSheet(page);
  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByTestId('document-view')).toBeVisible();

  const items = page.getByTestId('document-list-item');
  await expect(items).toHaveCount(2);
  await items.first().click();

  const video = page.getByTestId('document-video');
  await expect(video).toHaveAttribute('data-video-fit', 'full');

  // Set fit-to-height on document 1.
  const toggle = page.getByTestId('document-video-fit-toggle');
  await toggle.click();
  await expect(video).toHaveAttribute('data-video-fit', 'fit-height');

  // Navigate to document 2 — the choice must survive the switch (the reader
  // remounts on every active-row change).
  await items.nth(1).click();
  await expect(page.getByTestId('document-video')).toHaveAttribute('data-video-fit', 'fit-height');
  await expect(page.getByTestId('document-video-fit-toggle')).toHaveAttribute('aria-pressed', 'true');

  // Keyboard nav (↑) back to document 1 also keeps it sticky, not just clicks.
  await page.getByTestId('document-list-body').press('ArrowUp');
  await expect(page.getByTestId('document-video')).toHaveAttribute('data-video-fit', 'fit-height');

  // Reload: the preference is chrome state, so it survives per-project too
  // (same contract workbench-ia-document-view.spec.ts pins for the PDF fit
  // field) — documented behavior, not left ambiguous.
  await page.reload();
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByTestId('document-view')).toBeVisible();
  await expect(page.getByTestId('document-video')).toHaveAttribute('data-video-fit', 'fit-height');
});
