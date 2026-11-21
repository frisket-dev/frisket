// Media cells: the seeded "Council audio" project has an audio column
// whose value is a blob envelope; the row drawer must resolve it to a
// playable /api/projects/{pid}/blobs/{digest} URL.
// (playwright.config.ts launches Chromium with autoplay enabled.)

import { promises as fs } from 'node:fs';
import { expect, test } from '@playwright/test';
import { routeV1ActionRun, routeV1ActionStatus } from './actionStatusFixtures';
import {
  clickCell,
  clickCellLeadingControl,
  createProject,
  listSheets,
  openAction,
  openProject,
  projectIdByName,
  selectActionInputColumns,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';

const SAMPLE_AUDIO_FILENAMES = [
  'ann-arbor-policy-committee-2022-04-19-excerpt.mp3',
  'ann-arbor-icpoc-2020-07-01-excerpt.mp3',
] as const;

test('audio row controls the one app-level player and actually plays', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Council audio');
  const sheets = await listSheets(page.request, pid);
  const columns = await sheetColumns(page.request, pid, sheets[0].id);
  expect(columns.find((c) => c.name === 'media')?.type).toBe('audio');

  await openProject(page, pid);
  await clickCell(page, columns, 'transcript', 0);
  await page.keyboard.press('Enter');

  const rowAudio = page.getByTestId('row-audio');
  await expect(rowAudio).toBeVisible();
  await rowAudio.click();
  const audio = page.getByTestId('now-playing-audio');
  const src = await audio.getAttribute('src');
  expect(src).toMatch(new RegExp(`/api/projects/${pid}/blobs/`));

  // The blob endpoint really serves the bytes…
  const blob = await page.request.get(src!);
  expect(blob.ok()).toBeTruthy();
  expect(blob.headers()['content-type']).toContain('audio');

  // …and the element can play them: start playback, then watch the playhead.
  await audio.evaluate((el: HTMLAudioElement) => el.play());
  await expect
    .poll(async () => audio.evaluate((el: HTMLAudioElement) => el.currentTime), {
      timeout: 15_000,
    })
    .toBeGreaterThan(0);
  await audio.evaluate((el: HTMLAudioElement) => el.pause());
});

test('audio grid leading control opens one dismissible player without opening row detail', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Council audio');
  const sheets = await listSheets(page.request, pid);
  const columns = await sheetColumns(page.request, pid, sheets[0].id);
  const mediaColumn = columns.find((column) => column.name === 'media');
  expect(mediaColumn?.type).toBe('audio');
  const firstPage = await sheetData(page.request, pid, sheets[0].id, 0, 1);
  const mediaCell = firstPage.rows[0]?.cells[String(mediaColumn!.id)];
  expect(mediaCell).toBeTruthy();
  const mediaBlob = (mediaCell as { blob: string }).blob;
  expect(mediaBlob).toMatch(/^[a-f0-9]{64}$/);

  await openProject(page, pid);
  await clickCell(page, columns, 'media', 0);
  await expect(page.getByTestId('now-playing-widget')).toHaveCount(0);
  await clickCellLeadingControl(page, columns, 'media', 0);

  await expect(page.getByTestId('row-drawer')).toHaveCount(0);
  const audio = page.getByTestId('now-playing-audio');
  await expect(audio).toHaveCount(1);
  await expect(audio).toBeVisible();
  expect(await audio.evaluate((el) => el instanceof HTMLAudioElement)).toBeTruthy();
  const src = await audio.getAttribute('src');
  expect(src).toBe(`/api/projects/${pid}/blobs/${mediaBlob}`);

  await expect
    .poll(async () => audio.evaluate((el: HTMLAudioElement) => el.currentTime), {
      timeout: 15_000,
    })
    .toBeGreaterThan(0);

  const localStoriesPid = await projectIdByName(page.request, 'Local stories');
  await page.getByTestId('switch-project').click();
  await page.getByTestId(`menu-project-${localStoriesPid}`).click();
  await page.waitForURL(`**/p/${localStoriesPid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('now-playing-audio')).toHaveCount(0);
});

test('audio playback survives a run while new grid starts wait for terminal progress', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-media-run-gate'));
  const form = new FormData();
  for (const filename of SAMPLE_AUDIO_FILENAMES) {
    const bytes = await fs.readFile(
      new URL(`../../../src/frisket/sample_content/audio/${filename}`, import.meta.url),
    );
    form.append('files', new Blob([bytes], { type: 'audio/mpeg' }), filename);
  }
  const imported = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=files`, {
    multipart: form,
  });
  expect(imported.ok()).toBeTruthy();
  const sheets = await listSheets(page.request, pid);
  const columns = await sheetColumns(page.request, pid, sheets[0].id);
  const runId = 8282;
  let status: 'running' | 'complete' = 'running';
  await routeV1ActionRun(page, pid, {
    actionKind: 'map.python',
    receiptId: `receipt-grid-audio-${runId}`,
    runId,
    status: 'queued',
  });
  await routeV1ActionStatus(page, pid, () => ({
    actionKind: 'map.python',
    actionName: 'Python',
    projectId: pid,
    runId,
    sheetId: Number(sheets[0].id),
    status,
    live: status === 'running',
    total: 2,
    completed: status === 'complete' ? 2 : 0,
  }));

  await openProject(page, pid);
  await clickCellLeadingControl(page, columns, 'media', 0);
  const audio = page.getByTestId('now-playing-audio');
  await expect(audio).toHaveCount(1);
  const firstSrc = await audio.getAttribute('src');

  await openAction(page, 'map.python');
  await selectActionInputColumns(page, 'filename');
  await page.getByTestId('field-code').fill("result = row.get('filename', '')");
  await page.getByTestId('generated-action-run').click();
  await expect(page.getByTestId('run-progress')).toContainText('running');
  await expect(audio).toHaveAttribute('src', firstSrc!);

  // The run gate blocks starts, not the safety-critical pause on the source
  // that was already playing when the run began.
  await clickCellLeadingControl(page, columns, 'media', 0);
  await expect.poll(() => audio.evaluate((el: HTMLAudioElement) => el.paused)).toBe(true);
  await clickCellLeadingControl(page, columns, 'media', 0);
  await expect.poll(() => audio.evaluate((el: HTMLAudioElement) => el.paused)).toBe(true);

  await clickCellLeadingControl(page, columns, 'media', 1);
  await expect(audio).toHaveAttribute('src', firstSrc!);

  // Terminal progress is intentionally retained by the run controller, but
  // must no longer block a new grid audio click.
  status = 'complete';
  await expect(page.getByTestId('run-progress')).toContainText('complete');
  await clickCellLeadingControl(page, columns, 'media', 1);
  await expect(audio).not.toHaveAttribute('src', firstSrc!);
});
