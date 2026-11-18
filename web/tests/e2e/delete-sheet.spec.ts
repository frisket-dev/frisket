import { expect, test, type APIRequestContext } from '@playwright/test';
import {
  createProject,
  importCsv,
  listSheets,
  openProject,
  postAction,
  sheetData,
  typedActionRequest,
  uniqueName,
} from './helpers';

async function createJoin(
  request: APIRequestContext,
  projectId: string,
  leftSheetId: number,
  rightSheetId: number,
): Promise<number> {
  const body = await postAction(request, projectId, typedActionRequest('derive.join', {
    sheetId: leftSheetId,
    sheetName: 'Joined records',
    params: {
      right: { sheet_id: rightSheetId },
      join_keys: [{ left_column: 'id', right_column: 'id' }],
      how: 'inner',
      indicator: false,
    },
  }));
  expect(body.status).toBe('completed');
  const output = body.outputs?.find((candidate) => candidate.kind === 'sheet');
  return Number(output?.sheet_id);
}

test('an active standalone sheet can be deleted', async ({ page, request }) => {
  const projectId = await createProject(request, uniqueName('delete-sheet'));
  const keepSheetId = await importCsv(request, projectId, 'keep.csv', 'name\nKeep\n');
  const deleteSheetId = await importCsv(request, projectId, 'discard.csv', 'name\nDiscard\n');
  await openProject(page, projectId, deleteSheetId);

  await page.getByTestId(`workbench-mainView-tab-delete-${deleteSheetId}`).click();
  const modal = page.getByTestId('delete-sheet-modal');
  await expect(modal).toContainText('Delete discard?');
  await expect(modal).toContainText(
    'Remove this sheet from the project. You can undo this change; other sheets keep their data.',
  );
  await page.getByTestId('delete-sheet-confirm').click();

  await expect(modal).toBeHidden();
  await expect(page.getByTestId(`workbench-mainView-tab-${deleteSheetId}`)).toHaveCount(0);
  await expect(page.getByTestId(`workbench-mainView-tab-${keepSheetId}`)).toBeVisible();
  expect((await listSheets(request, projectId)).map((sheet) => sheet.id)).toEqual([keepSheetId]);
});

test('deleting a parent sheet keeps the joined sheet built from it, with its rows', async ({ page, request }) => {
  const projectId = await createProject(request, uniqueName('delete-sheet-dependent'));
  const parentSheetId = await importCsv(request, projectId, 'parents.csv', 'id,name\n1,Parent\n');
  const otherSheetId = await importCsv(request, projectId, 'others.csv', 'id,label\n1,Other\n');
  const joinedSheetId = await createJoin(request, projectId, parentSheetId, otherSheetId);
  await openProject(page, projectId, parentSheetId);

  await page.getByTestId(`workbench-mainView-tab-delete-${parentSheetId}`).click();
  const modal = page.getByTestId('delete-sheet-modal');
  await expect(modal).toContainText('Delete parents?');
  await page.getByTestId('delete-sheet-confirm').click();

  await expect(modal).toBeHidden();
  await expect(page.getByTestId(`workbench-mainView-tab-${parentSheetId}`)).toHaveCount(0);
  expect((await listSheets(request, projectId)).map((sheet) => sheet.id)).toEqual([
    otherSheetId,
    joinedSheetId,
  ]);
  const joined = await sheetData(request, projectId, joinedSheetId);
  expect(joined.total).toBe(1);
});
