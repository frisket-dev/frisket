// The row-field action rail: [explain] [edit] [copy] are ALL hover-revealed;
// explain leads so its absence never gaps the right edge. The rail must reveal without ANY
// layout shift: fixed slots, placeholders when hidden.

import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openCellDrawer,
  openProject,
  sheetColumns,
  uniqueName,
} from './helpers';

test('row drawer action rail is hover-revealed with zero layout shift', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-row-action-layout'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'stories.csv',
    'snippet,source\n"City hall awarded a paving contract.","minutes"\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);
  // 'snippet' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columns, 'snippet', 0);

  const field = page.getByTestId('row-field-snippet');
  await expect(field).toBeVisible();

  // Pre-hover: NOTHING renders — edit is hover-only now, like copy.
  await expect(field.getByTestId('cell-edit-snippet')).toHaveCount(0);
  await expect(field.getByTestId('cell-action-copy')).toHaveCount(0);

  const boxBefore = await field.boundingBox();

  await field.hover();
  const edit = field.getByTestId('cell-edit-snippet');
  await expect(edit).toBeVisible();
  await expect(field.getByTestId('cell-action-copy')).toBeVisible();

  // Revealing must not shift the field's geometry (fixed slots + placeholders).
  const boxAfter = await field.boundingBox();
  expect(Math.abs(boxAfter!.height - boxBefore!.height)).toBeLessThan(0.5);
  expect(Math.abs(boxAfter!.width - boxBefore!.width)).toBeLessThan(0.5);
  expect(Math.abs(boxAfter!.y - boxBefore!.y)).toBeLessThan(0.5);

  // Slot order on a source column: [placeholder(explain)] [edit] [copy] — edit
  // sits left of copy; the (absent) explain slot leads.
  const editBox = await edit.boundingBox();
  const copyBox = await field.getByTestId('cell-action-copy').boundingBox();
  expect(editBox!.x).toBeLessThan(copyBox!.x);

  // Un-hover (move to the header) hides them again.
  await page.getByTestId('inspect-detail-rownum').hover();
  await expect(field.getByTestId('cell-action-copy')).toHaveCount(0);
  await expect(field.getByTestId('cell-edit-snippet')).toHaveCount(0);
});
