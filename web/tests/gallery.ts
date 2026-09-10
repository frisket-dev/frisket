// Screenshot gallery: boots through every major view against the seeded
// backend and saves labeled full-page screenshots to screenshots/gallery/.
// Run with `npm run gallery` (requires the seeded stack — tests/README.md).
//
// This is the "what looks good vs not" artifact: each view is framed
// deliberately (drawers/panels open where they carry the design weight).

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type Page } from '@playwright/test';
import {
  clickCell,
  clickHeader,
  createProject,
  importCsv,
  listSheets,
  openProject,
  projectIdByName,
  sheetColumns,
  TINY_CSV,
  uniqueName,
} from './e2e/helpers';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT_DIR = path.resolve(HERE, '../screenshots/gallery');

interface ManifestRow {
  name: string;
  file: string;
  note: string;
}
const manifest: ManifestRow[] = [];

async function shot(page: Page, name: string, note: string): Promise<void> {
  const file = path.join(OUT_DIR, `${name}.png`);
  await page.waitForTimeout(400); // let canvas paints / drawer slides settle
  await page.screenshot({ path: file, fullPage: true });
  expect(fs.statSync(file).size, `${name}.png should not be empty`).toBeGreaterThan(0);
  manifest.push({ name, file: path.relative(path.resolve(HERE, '..'), file), note });
}

function skip(name: string, note: string): void {
  manifest.push({ name, file: '(skipped)', note });
}

test.afterAll(() => {
  const w = Math.max(...manifest.map((r) => r.name.length));
  const fw = Math.max(...manifest.map((r) => r.file.length));
  console.log(
    '\nGallery manifest\n' +
      `${'view'.padEnd(w)}  ${'file'.padEnd(fw)}  note\n` +
      `${'-'.repeat(w)}  ${'-'.repeat(fw)}  ${'-'.repeat(30)}\n` +
      manifest.map((r) => `${r.name.padEnd(w)}  ${r.file.padEnd(fw)}  ${r.note}`).join('\n') +
      '\n',
  );
});

test('screenshot gallery', async ({ page }) => {
  fs.mkdirSync(OUT_DIR, { recursive: true });

  // ---- 01 sign-in (hosted tier only) --------------------------------------
  const me = await page.request.get('/api/me');
  if (me.status() === 401) {
    await page.goto('/');
    await expect(page.getByTestId('sign-in')).toBeVisible();
    await shot(page, '01-signin', 'hosted sign-in gate');
  } else {
    skip('01-signin', 'local tier — no sign-in gate');
  }

  // ---- 02 picker -----------------------------------------------------------
  await page.goto('/');
  await expect(page.getByTestId('project-list')).toBeVisible();
  await shot(page, '02-picker', 'project picker with seeded projects');

  // ---- 03–06 Local stories: grid, drawers, recipe form ---------------------
  const stories = await projectIdByName(page.request, 'Local stories');
  const storySheets = await listSheets(page.request, stories);
  const storyCols = await sheetColumns(page.request, stories, storySheets[0].id);
  await openProject(page, stories);
  await expect(page.getByTestId('sheet-stats')).toHaveText(/8 rows/);
  await shot(page, '03-grid-stories', 'classify demo grid, recipe panel open');

  await clickCell(page, storyCols, 'snippet', 0);
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('cell-provenance').first()).toBeVisible();
  await shot(page, '04-row-drawer', 'row drawer: values + provenance + justification');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('row-drawer')).toBeHidden();

  await clickHeader(page, storyCols, 'beat');
  await expect(page.getByTestId('column-drawer')).toBeVisible();
  await expect(page.getByTestId('column-prompt')).toBeVisible();
  await shot(page, '05-column-drawer', 'AI column drawer: prompt, fields, versions');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('column-drawer')).toBeHidden();

  await page.getByTestId('action-classify').click();
  await expect(page.getByTestId('action-form')).toBeVisible();
  await shot(page, '06-action-form', 'classify action form with cost estimate');

  // ---- 07 run watcher (live run in a scratch project) ----------------------
  // A real (sub-cent) gemini-flash run so the watcher popover has live
  // numbers; the nonce in the prompt defeats the response cache so the run
  // is actually observable in flight.
  const scratch = await createProject(page.request, uniqueName('gallery-run'));
  await importCsv(page.request, scratch, 'snippets.csv', TINY_CSV);
  await openProject(page, scratch);
  await page.getByTestId('action-classify').click();
  await page
    .getByTestId('action-prompt')
    .fill(`Each row is a one-line local news item. Pick the single best label. (gallery ${Date.now()})`);
  await page.getByLabel('Field 1 labels').fill('transit, money, other');
  await page.getByTestId('run-button').click();
  await expect(page.getByTestId('run-watcher-toggle')).toBeVisible({ timeout: 15_000 });
  await page.getByTestId('run-watcher-toggle').click();
  await expect(page.getByTestId('run-watcher')).toBeVisible();
  await shot(page, '07-run-watcher', 'status bar + run watcher popover (live run)');
  await expect(page.getByTestId('run-progress')).toContainText('complete', { timeout: 120_000 });

  // ---- 08–10 history, review queue, search ---------------------------------
  await openProject(page, stories);
  await page.getByTestId('bottom-dock-tab-history').click();
  await expect(page.getByTestId('history-list')).toBeVisible();
  await shot(page, '08-history', 'operation history (bottom dock list + detail)');

  await page.getByTestId('review-queue-button').click();
  await expect(page.getByTestId('review-queue')).toBeVisible();
  await expect(page.getByTestId('review-card')).toBeVisible();
  await shot(page, '09-review-queue', 'review overlay, least-confident first');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('review-queue')).toBeHidden();

  await page.keyboard.press('ControlOrMeta+k');
  await page.getByTestId('search-input').fill('listeria');
  await expect(page.getByTestId('search-results')).toBeVisible();
  await shot(page, '10-search', '⌘K search overlay with grouped FTS hits');
  await page.keyboard.press('Escape');

  // ---- 11 account -----------------------------------------------------------
  await page.goto('/account');
  await expect(page.getByTestId('account-page')).toBeVisible();
  await expect(page.getByTestId('account-page')).toContainText(/Signed in as|running locally/);
  await shot(page, '11-account', 'account page (local notice or balance)');

  // ---- 12 tariff grid (json badges + scores) --------------------------------
  const tariff = await projectIdByName(page.request, 'Tariff impacts');
  await openProject(page, tariff);
  await expect(page.getByTestId('sheet-stats')).toHaveText(/6 rows/);
  await shot(page, '12-tariff-grid', 'web_search→summarize→score chain grid');

  // ---- 13 faces child sheet -------------------------------------------------
  const faces = await projectIdByName(page.request, 'Faces demo');
  const facesSheets = await listSheets(page.request, faces);
  const facesChild = facesSheets.find((s) => s.parent_sheet_id !== null);
  if (!facesChild) throw new Error('Faces child sheet missing — reseed showcase');
  await openProject(page, faces, facesChild.id);
  await expect(page.getByTestId('sheet-breadcrumb')).toBeVisible();
  await page.waitForTimeout(1200); // image cells load async into the canvas
  await shot(page, '13-faces-grid', 'derived Faces sheet (image cells + breadcrumb)');

  // ---- 14 people derive (child sheet + breadcrumb) ---------------------------
  const people = await projectIdByName(page.request, 'People mentioned');
  const peopleSheets = await listSheets(page.request, people);
  const peopleChild = peopleSheets.find((s) => s.parent_sheet_id !== null);
  if (!peopleChild) throw new Error('People child sheet missing — reseed showcase');
  await openProject(page, people, peopleChild.id);
  await expect(page.getByTestId('sheet-breadcrumb')).toBeVisible();
  await shot(page, '14-people-derive', 'derived People sheet with lineage breadcrumb');

  // ---- 15 audio row ----------------------------------------------------------
  const audio = await projectIdByName(page.request, 'Council audio');
  const audioSheets = await listSheets(page.request, audio);
  const audioCols = await sheetColumns(page.request, audio, audioSheets[0].id);
  await openProject(page, audio);
  await clickCell(page, audioCols, 'transcript', 0);
  await expect(page.getByTestId('row-audio')).toBeVisible();
  await shot(page, '15-audio-row', 'row drawer with playable audio blob');
});
