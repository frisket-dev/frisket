// LIVE acceptance for the annotated-text reader's mention-detail panel.
//
// Nothing here is stubbed. The entities, their fingerprints, their offsets and
// the annotation layer they are drawn from are all produced by a real spaCy
// `map.ner` run against the real coordinate substrate; the reader then draws at
// the offsets that run recorded. What is being demonstrated is the seam the
// unit and component tests cannot reach: server offsets -> the in-cell marks ->
// Route A's document list -> Route B's occurrences and snippets, on one live
// stack.
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { expect, test, type Page } from '@playwright/test';

import {
  createProject,
  importCsv,
  openProject,
  runAndWait,
  uniqueName,
} from './helpers';

const SHOT_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../test-results');

// Three documents, all mentioning Maria Gomez, one of them repeatedly — so
// "N mentions across M documents" has two genuinely different numbers, and the
// expanded document has more than one occurrence to place.
const ROWS = [
  'Maria Gomez convened the review in Albany. The committee heard that Maria Gomez had '
    + 'filed in March. Later the same day Maria Gomez signed the order and left Albany.',
  'Acme Corporation wrote to Maria Gomez about the Buffalo office.',
  'The Buffalo office closed without comment from Acme Corporation.',
];

const csv = (rows: string[]) => `snippet\n${rows.map((row) => `"${row}"`).join('\n')}\n`;

const shoot = (page: Page, name: string) =>
  page.screenshot({
    path: path.join(SHOT_DIR, `annotated-text-reader-${name}.png`),
    animations: 'disabled',
    fullPage: false,
  });

test('a mark opens the mention panel, and expanding a document shows where it is', async ({
  page,
  request,
}) => {
  test.setTimeout(180_000);
  const pid = await createProject(request, uniqueName('e2e-annotated-reader'));
  const sheetId = await importCsv(request, pid, 'filings.csv', csv(ROWS));

  // Real extraction: this is what writes the text surfaces, the source spans
  // and the annotation links the reader reads back.
  await runAndWait(request, pid, {
    schema_version: 'frisket.action.v2',
    kind: 'map.ner',
    capabilities: ['project:write'],
    params: {
      sheet_id: sheetId,
      input_columns: ['snippet'],
      labels: ['person', 'organization', 'location'],
      engine: 'spacy',
      output_name: 'entities',
    },
  });

  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-document').click();
  await expect(page.getByTestId('document-view')).toBeVisible();

  // The text source lights up only because the sheet now has an annotated text
  // column (the narrow availability signal, R13).
  const reader = page.getByTestId('annotated-text-reader');
  await expect(reader).toBeVisible({ timeout: 30_000 });
  await page.getByTestId('document-list-item').first().click();
  await expect(page.getByTestId('annotated-text-content')).toBeVisible();

  // Marks are drawn from the run's own offsets over the exact cell string.
  const marks = page.getByTestId('annotation-mark');
  await expect(marks.first()).toBeVisible({ timeout: 30_000 });
  const maria = marks.filter({ hasText: 'Maria Gomez' }).first();
  await expect(maria).toBeVisible();
  await shoot(page, '1-marks');

  await maria.click();

  // Level 1: which documents, with the two counts in the Mentions panel's order.
  const panel = page.getByTestId('mention-detail-panel');
  await expect(panel).toBeVisible();
  await expect(panel.getByTestId('mention-detail-title')).toContainText('Maria Gomez');
  await expect(panel.getByTestId('mention-detail-counts')).toContainText(/mentions across/);
  const documents = panel.getByTestId('mention-detail-doc');
  await expect(documents.first()).toBeVisible();
  await shoot(page, '2-documents');

  // Level 2: expand the busiest document and read where the mention actually is.
  await panel.getByTestId('mention-detail-expand').first().click();
  const occurrences = panel.getByTestId('mention-occurrence');
  await expect(occurrences.first()).toBeVisible({ timeout: 30_000 });
  // The first document has three occurrences of the same normalized mention.
  await expect(occurrences).toHaveCount(3);
  // Each snippet marks the mention INSIDE its context, at the server's offsets.
  await expect(occurrences.first().locator('mark')).toHaveText('Maria Gomez');
  await expect(occurrences.first()).toContainText('convened the review');
  await shoot(page, '3-occurrences');

  // Clicking an occurrence opens that document at the mark and leaves the
  // panel open (O3) — a drill-down, not a navigation.
  await occurrences.nth(2).click();
  await expect(panel).toBeVisible();
  await expect(page.getByTestId('annotation-mark').first()).toBeVisible();
  await expect(
    page.locator('[data-testid="annotation-mark"][data-active="true"]').first(),
  ).toBeVisible();
  await shoot(page, '4-opened-at-occurrence');
});
