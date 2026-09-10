import { expect, test, type APIRequestContext } from '@playwright/test';
import { createProject, importCsv, listSheets, openProject, uniqueName } from './helpers';

async function createJoin(
  request: APIRequestContext,
  projectId: string,
  leftSheetId: number,
  rightSheetId: number,
): Promise<number> {
  const response = await request.post(`/api/projects/${projectId}/actions/v1/run`, {
    data: {
      schema_version: 'frisket.action.v2',
      kind: 'derive.join',
      capabilities: ['project:write'],
      params: {
        left_sheet_id: leftSheetId,
        right_sheet_id: rightSheetId,
        join_keys: [{ left_column: 'id', right_column: 'id' }],
        how: 'inner',
        indicator: false,
        target_sheet_name: 'Joined records',
        confirmed: false,
      },
      idempotency_key: `delete-sheet-join@sha256:${Math.random().toString(16).slice(2)}`,
    },
  });
  const body = await response.json();
  expect(response.ok(), JSON.stringify(body)).toBeTruthy();
  expect(body.status).toBe('completed');
  const output = body.outputs.find((candidate: Record<string, unknown>) => candidate.kind === 'sheet');
  return Number(output.sheet_id);
}

test('an active standalone sheet can be permanently deleted', async ({ page, request }) => {
  const projectId = await createProject(request, uniqueName('delete-sheet'));
  const keepSheetId = await importCsv(request, projectId, 'keep.csv', 'name\nKeep\n');
  const deleteSheetId = await importCsv(request, projectId, 'discard.csv', 'name\nDiscard\n');
  await openProject(page, projectId, deleteSheetId);

  await page.getByTestId(`workbench-mainView-tab-delete-${deleteSheetId}`).click();
  const modal = page.getByTestId('delete-sheet-modal');
  await expect(modal).toContainText('Delete discard?');
  await expect(modal).toContainText('permanently removes the sheet');
  await page.getByTestId('delete-sheet-confirm').click();

  await expect(modal).toBeHidden();
  await expect(page.getByTestId(`workbench-mainView-tab-${deleteSheetId}`)).toHaveCount(0);
  await expect(page.getByTestId(`workbench-mainView-tab-${keepSheetId}`)).toBeVisible();
  expect((await listSheets(request, projectId)).map((sheet) => sheet.id)).toEqual([keepSheetId]);
});

test('a parent sheet names the joined sheet that must be deleted first', async ({ page, request }) => {
  const projectId = await createProject(request, uniqueName('delete-sheet-dependent'));
  const parentSheetId = await importCsv(request, projectId, 'parents.csv', 'id,name\n1,Parent\n');
  const otherSheetId = await importCsv(request, projectId, 'others.csv', 'id,label\n1,Other\n');
  const joinedSheetId = await createJoin(request, projectId, parentSheetId, otherSheetId);
  await openProject(page, projectId, parentSheetId);

  await page.getByTestId(`workbench-mainView-tab-delete-${parentSheetId}`).click();
  const modal = page.getByTestId('delete-sheet-modal');
  await expect(modal).toContainText('Delete the dependent sheet Joined records first.');
  await expect(page.getByTestId('delete-sheet-confirm')).toHaveCount(0);
  await modal.getByRole('button', { name: 'Close' }).click();

  await expect(modal).toBeHidden();
  expect((await listSheets(request, projectId)).map((sheet) => sheet.id)).toEqual([
    parentSheetId,
    otherSheetId,
    joinedSheetId,
  ]);
});
