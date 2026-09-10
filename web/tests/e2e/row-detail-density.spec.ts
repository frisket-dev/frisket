// Live demo (required checkpoint evidence, not just a green suite) for the
// Inspect-column density pass:
//   1. every field value is capped at 50vh with a "Show more" that expands it
//      in place — verified here in a real browser, since the overflow check
//      that gates the toggle needs actual layout (jsdom can't measure it);
//   2. entity-mention json shows only `text` + `type` (offsets/score/
//      fingerprint hidden);
//   3. timestamped transcript segments drop the redundant `segment_index`.
//
// Saves a review screenshot to screenshots/row-detail/ (not a golden test).

import { expect, test } from '@playwright/test';
import { createProject, importCsv, openCellDrawer, sheetColumns, uniqueName } from './helpers';
import { ensureShotsDir, shotsDir } from './screenshotHelpers';

const OUT_DIR = shotsDir('row-detail');

/** One CSV field, quoted with internal quotes doubled per RFC 4180. */
function csvField(value: string): string {
  return `"${value.replace(/"/g, '""')}"`;
}

const ENTITIES = JSON.stringify([
  { text: 'Ada Lovelace', type: 'PERSON', start: 0, end: 12, score: 0.98, fingerprint: 'person:ada lovelace' },
  { text: 'London', type: 'GPE', start: 20, end: 26, score: 0.91, fingerprint: 'gpe:london' },
]);

const SEGMENTS = JSON.stringify([
  { segment_index: 0, start: 0.0, end: 3.2, text: 'Good morning, thanks for joining us today.' },
  { segment_index: 1, start: 3.2, end: 6.4, text: 'Happy to be here.' },
]);

// Long enough to overflow 50vh once it wraps in the narrow Detail column.
const LONG_NOTES = Array.from({ length: 40 }, (_, i) =>
  `Paragraph ${i + 1}: this transcript note is deliberately verbose so the ` +
  `field value comfortably exceeds half the viewport height and triggers the ` +
  `Show more affordance.`,
).join(' ');

test('row detail clamps long values and trims entity/segment columns', async ({ page }) => {
  ensureShotsDir(OUT_DIR);
  const pid = await createProject(page.request, uniqueName('e2e-row-density'));
  const csv =
    'country,entities,segments,notes\n' +
    [csvField('Germany'), csvField(ENTITIES), csvField(SEGMENTS), csvField(LONG_NOTES)].join(',') +
    '\n';
  const sheetId = await importCsv(page.request, pid, 'rows.csv', csv);
  const columns = await sheetColumns(page.request, pid, sheetId);

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // Open the resident Detail column focused on the long notes field.
  // 'notes' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columns, 'notes', 0);
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();

  // (1) The long field overflows → a "Show more" toggle appears; expanding it
  // removes the clamp in place.
  const notesField = page.getByTestId('row-field-notes');
  const toggle = notesField.getByTestId('row-field-clamp-toggle');
  await expect(toggle).toBeVisible();
  await expect(toggle).toHaveText('Show more');
  const clampContent = notesField.getByTestId('row-field-clamp');
  const clampedHeight = await clampContent.evaluate((el) => el.clientHeight);
  await toggle.click();
  await expect(toggle).toHaveText('Show less');
  const expandedHeight = await clampContent.evaluate((el) => el.clientHeight);
  expect(expandedHeight).toBeGreaterThan(clampedHeight);

  // (2) Entity json: text + type only.
  const entityTable = page.getByTestId('entity-mini-table');
  await expect(entityTable).toBeVisible();
  const entityHeaders = await entityTable.locator('th').allTextContents();
  expect(entityHeaders).toEqual(['text', 'type']);
  await expect(entityTable).toContainText('Ada Lovelace');
  await expect(entityTable).not.toContainText('fingerprint');
  await expect(entityTable).not.toContainText('0.98');

  // (3) Transcript segments: no segment_index column.
  const segmentTable = page.getByTestId('transcript-segment-mini-table');
  await expect(segmentTable).toBeVisible();
  const segmentHeaders = await segmentTable.locator('th').allTextContents();
  expect(segmentHeaders).not.toContain('segment_index');
  expect(segmentHeaders).toEqual(['start', 'end', 'text']);

  await drawer.screenshot({ path: `${OUT_DIR}/row-detail-density.png`, animations: 'disabled' });
});
