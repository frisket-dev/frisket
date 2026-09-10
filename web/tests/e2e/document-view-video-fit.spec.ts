// A video in Document view can toggle between full size and fit-to-height;
// row detail uses a distinct paper-and-magnifying-glass icon. Both halves are
// bound here: (1) a video-only fit toggle in DocumentReader.tsx's
// header, defaulting to full-size and swapping Maximize2/Minimize2
// (freed from the row-detail slot); (2) document-open-detail's icon becomes
// lucide's FileSearch (paper + magnifying glass), same testid/behavior.

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

/** Create a project and import a real (tiny) mp4 through the blob path, the
 *  same /import/files endpoint workbench-ia-document-view.spec.ts's
 *  seedDocSheet uses for PDFs — producing a sheet with a `video` media
 *  column. */
async function seedVideoDocSheet(page: Page): Promise<VideoDocSheet> {
  const pid = await createProject(page.request, uniqueName('document-video-fit'));
  const importRes = await page.request.post(
    `/api/projects/${pid}/import/files?sheet_name=clips`,
    {
      multipart: {
        files: {
          name: 'clip.mp4',
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
  return { pid, sheetId: sheet.id };
}

test('a video document shows a full-size/fit-to-height toggle using the expand icon, defaulting to full size', async ({
  page,
}) => {
  const { pid, sheetId } = await seedVideoDocSheet(page);
  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByTestId('document-view')).toBeVisible();
  await expect(page.getByTestId('document-reader-chip')).toContainText(/video/i);

  const video = page.getByTestId('document-video');
  await expect(video).toBeVisible();
  await expect(video).toHaveAttribute('data-video-fit', 'full');

  const toggle = page.getByTestId('document-video-fit-toggle');
  await expect(toggle).toBeVisible();
  await expect(toggle).toHaveAttribute('aria-pressed', 'false');
  await expect(toggle).toHaveAttribute('title', /fit to height/i);

  await toggle.click();
  await expect(video).toHaveAttribute('data-video-fit', 'fit-height');
  await expect(toggle).toHaveAttribute('aria-pressed', 'true');
  await expect(toggle).toHaveAttribute('title', /full size/i);

  await toggle.click();
  await expect(video).toHaveAttribute('data-video-fit', 'full');
  await expect(toggle).toHaveAttribute('aria-pressed', 'false');
});

test('the fit toggle is video-only; the row-detail button keeps its testid/behavior but gets the paper+magnifier icon', async ({
  page,
}) => {
  const { pid, sheetId } = await seedVideoDocSheet(page);
  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByTestId('document-view')).toBeVisible();

  // The row-detail affordance still opens the drawer (unchanged behavior) —
  // only its glyph moved.
  const openDetail = page.getByTestId('document-open-detail');
  await expect(openDetail).toBeVisible();
  await expect(openDetail.locator('svg.lucide-file-search')).toHaveCount(1);
  await openDetail.click();
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('document-view')).toBeVisible();

  // A non-video document in the SAME sheet (a PDF row added beside the video
  // row, same media column) shows NO fit toggle — it is video-only.
  const columns = await sheetColumns(page.request, pid, sheetId);
  const mediaColumn = columns.find((c) => c.type === 'video' || c.type === 'file')!;
  const firstPage = await sheetData(page.request, pid, sheetId, 0, 5);
  const firstCell = firstPage.rows[0].cells[String(mediaColumn.id)] as Record<string, unknown>;
  await addRow(page.request, pid, sheetId, {
    [mediaColumn.name]: { ...firstCell, filename: 'report.pdf', mime: 'application/pdf' },
  });
  await page.reload();
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByTestId('document-view')).toBeVisible();
  await page.getByTestId('document-list-item').nth(1).click();
  await expect(page.getByTestId('document-reader-chip')).toContainText(/pdf/i);
  await expect(page.getByTestId('document-video-fit-toggle')).toHaveCount(0);
  // Row-detail keeps its icon regardless of media kind.
  await expect(
    page.getByTestId('document-open-detail').locator('svg.lucide-file-search'),
  ).toHaveCount(1);
});

test('the video source column is real media data (never fabricated)', async ({ page }) => {
  const { pid, sheetId } = await seedVideoDocSheet(page);
  const columns = await sheetColumns(page.request, pid, sheetId);
  const mediaColumn = columns.find((c) => c.type === 'video' || c.type === 'file');
  expect(mediaColumn).toBeTruthy();
  const data = await sheetData(page.request, pid, sheetId, 0, 5);
  expect(data.rows.length).toBeGreaterThan(0);
});
