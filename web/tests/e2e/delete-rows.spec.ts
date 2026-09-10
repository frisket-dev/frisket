// Row deletion gate:
// - select rows in the grid, the toolbar trash opens a confirm dialog
// - confirming deletes through the canonical v1 row.delete action
// - the visible total drops, the legacy direct-mutation routes stay untouched,
//   and the deletion is reversible through undo

import { expect, test, type Locator } from '@playwright/test';
import {
  createProject,
  importCsv,
  openHistory,
  openProject,
  selectRow,
  sheetData,
  uniqueName,
} from './helpers';

async function expectDeleteRowsContribution(contribution: Locator) {
  await expect(contribution).toHaveAttribute(
    'data-schema-version',
    'frisket.workbench.view.v1',
  );
  await expect(contribution).toHaveAttribute(
    'data-contribution-id',
    'frisket.core.view.row_delete_confirm',
  );
  await expect(contribution).toHaveAttribute('data-host', 'modalOrPeek');
  await expect(contribution).toHaveAttribute('data-mode', 'peek');
  await expect(contribution).toHaveAttribute(
    'data-runtime-component-key',
    'core.views.ConfirmDeleteRowsModal',
  );
  const capabilitiesAttr = await contribution.getAttribute('data-required-capabilities');
  expect(capabilitiesAttr).not.toBeNull();
  expect(capabilitiesAttr?.split(/\s+/).filter(Boolean).sort()).toEqual([
    'operation.undo',
    'row.delete',
  ]);
}

test('deletes selected rows through a confirmed v1 row.delete action', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('delete-rows'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'people.csv',
    'name\nAda\nGrace\nKatherine\n',
  );

  const rowDeleteActions: Array<Record<string, unknown>> = [];
  let legacyRowsCalled = false;
  await page.route(`**/api/projects/${pid}/sheets/${sheetId}/rows`, async (route) => {
    legacyRowsCalled = true;
    await route.fulfill({
      status: 599,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'legacy /rows route should not be called' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    if (payload.action_id === 'row.delete') rowDeleteActions.push(payload);
    await route.continue();
  });

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('sheet-stats')).toHaveText('3 rows · 1 columns');
  await openHistory(page);

  // Delete-N is selection-contextual: absent (not merely disabled) until rows
  // are selected (workbench-ia-toolbar-diet-v1).
  await expect(page.getByTestId('delete-rows-button')).toHaveCount(0);

  await selectRow(page, 0);
  await selectRow(page, 2);
  await expect(page.getByTestId('delete-rows-button')).toBeEnabled();

  await page.getByTestId('delete-rows-button').click();
  await expectDeleteRowsContribution(
    page.getByTestId('workbench-contribution-frisket-core-view-row-delete-confirm'),
  );
  await expect(page.getByRole('dialog', { name: 'Delete 2 rows?' })).toBeVisible();
  await expect(page.getByTestId('delete-rows-modal')).toContainText('Delete 2 rows?');

  // cancel leaves everything intact
  await page.getByTestId('delete-rows-cancel').click();
  await expect(page.getByTestId('delete-rows-modal')).toBeHidden();
  expect((await sheetData(page.request, pid, sheetId)).total).toBe(3);

  // confirm deletes the two selected rows
  await page.getByTestId('delete-rows-button').click();
  await page.getByTestId('delete-rows-confirm').click();
  await expect(page.getByTestId('sheet-stats')).toHaveText('1 rows · 1 columns');
  await expect.poll(async () => (await sheetData(page.request, pid, sheetId)).total).toBe(1);

  expect(legacyRowsCalled).toBe(false);
  expect(rowDeleteActions).toHaveLength(1);
  expect(rowDeleteActions[0]).toMatchObject({
    action_id: 'row.delete',
    scope: { kind: 'project' },
    output_names: {},
    params: {
      sheet_id: Number(sheetId),
    },
  });
  expect(rowDeleteActions[0]).not.toHaveProperty('kind');
  expect(rowDeleteActions[0]).not.toHaveProperty('capabilities');
  expect((rowDeleteActions[0]?.params as { row_ids: number[] }).row_ids).toHaveLength(2);
  expect(String(rowDeleteActions[0]?.idempotency_key)).toMatch(/^web-row\.delete:/);
  await expect(page.getByTestId('history-list')).toContainText('delete rows');

  // delete is soft + reversible: undo restores the visible total
  await page.getByTestId('undo-button').click();
  await expect.poll(async () => (await sheetData(page.request, pid, sheetId)).total).toBe(3);
});
