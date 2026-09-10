// Media cells: the seeded "Council audio" project has an audio column
// whose value is a blob envelope; the row drawer must resolve it to a
// playable /api/projects/{pid}/blobs/{digest} URL.
// (playwright.config.ts launches Chromium with autoplay enabled.)

import { expect, test } from '@playwright/test';
import { routeV1ActionRun, routeV1ActionStatus } from './actionStatusFixtures';
import {
  clickCell,
  clickCellLeadingControl,
  listSheets,
  openAction,
  openProject,
  projectIdByName,
  sheetColumns,
  sheetData,
} from './helpers';

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
  const pid = await projectIdByName(page.request, 'Council audio');
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
    total: 1,
    completed: status === 'complete' ? 1 : 0,
  }));

  await openProject(page, pid);
  await clickCellLeadingControl(page, columns, 'media', 0);
  const audio = page.getByTestId('now-playing-audio');
  await expect(audio).toHaveCount(1);
  const firstSrc = await audio.getAttribute('src');

  await openAction(page, 'map.python');
  await page.getByTestId('field-code').fill("result = row.get('transcript', '')");
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
