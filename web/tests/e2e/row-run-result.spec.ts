import { expect, test } from '@playwright/test';
import { clickCell, createProject, importCsv, openCellDrawer, openProject, sheetColumns, uniqueName } from './helpers';
import { seedReviewClassifyRun } from './reviewFixtures';

test('run-result details stay expanded for a field when selecting another row', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('run-result-disclosure'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', 'story\nRoad repairs\nSchool funding\n');
  seedReviewClassifyRun({
    pid, sheetId, sourceColumns: ['story'], context: 'Classify each story.',
    fields: [{ name: 'beat', type: 'category', labels: ['civic', 'private'] }],
    reply: { beat: 'civic', beat_confidence: 0.8, beat_justification: 'Public affairs.' },
  });
  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);
  await openCellDrawer(page, columns, 'beat', 0);
  const field = page.getByTestId('row-field-beat');
  const toggle = field.getByTestId('cell-provenance-run-result-toggle');
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await expect(field.getByTestId('prov-justification')).toHaveText('Public affairs.');
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await expect(field.getByTestId('cell-provenance')).toContainText('confidence');

  await clickCell(page, columns, 'beat', 1);
  await expect(page.getByTestId('row-drawer')).toContainText('School funding');
  await expect(toggle).toHaveAttribute('aria-expanded', 'true');
  await toggle.click();
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
  await clickCell(page, columns, 'beat', 0);
  await expect(page.getByTestId('row-drawer')).toContainText('Road repairs');
  await expect(toggle).toHaveAttribute('aria-expanded', 'false');
});
