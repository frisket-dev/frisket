import { expect, test } from '@playwright/test';
import {
  commitTableImport, createProject, importCsv, openDiscoverTab, openImportWorkspace,
  openProject, previewTableFile, reviewTableColumns, runAndWait, sheetData, uniqueName,
} from './helpers';
import { openReviewRoute } from './reviewFixtures';

test('a current grid selection becomes a persistent View with its own alert', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('domain-view-alert'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', 'title,status\nBudget,open\nRoad,closed\n');
  await openProject(page, pid, sheetId);
  await openDiscoverTab(page, 'Watches');
  const panel = page.getByTestId('watches-panel');
  await panel.getByTestId('new-watch-from-current-grid').click();
  await page.getByTestId('watch-create-name').fill('Daily stories');
  await page.getByLabel('Check frequency', { exact: true }).selectOption('@daily');
  await page.getByLabel('Rows leaving this view', { exact: true }).check();
  const created = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/projects/${pid}/watches`);
  await page.getByTestId('create-watch-button').click();
  const response = await created;
  expect(response.ok()).toBeTruthy();
  const request = response.request().postDataJSON();
  expect(request).toMatchObject({ name: 'Daily stories', view_id: expect.any(Number),
    schedule: '@daily', detection_policy: { kind: 'conditions', entered: true, exited: true } });
  expect(request).not.toHaveProperty('query');
  const views = await (await page.request.get(`/api/projects/${pid}/views`)).json();
  expect(views).toEqual(expect.arrayContaining([expect.objectContaining({
    id: request.view_id, spec: expect.objectContaining({
      selection: expect.objectContaining({ scope: { kind: 'sheet', sheet_id: sheetId } }),
    }),
  })]));
  const item = panel.getByTestId('watch-item').filter({ hasText: 'Daily stories' });
  await expect(item).toBeVisible();
  await page.screenshot({ path: 'test-results/domain-view-alert.png', fullPage: true });
  await item.getByRole('button', { name: 'Alert conditions', exact: true }).click();
  await expect(item.getByLabel('Check frequency')).toHaveValue('@daily');
  await page.screenshot({ path: 'test-results/domain-alert-schedule.png', fullPage: true });
});

test('keyed table import updates matching rows through the ordinary import review', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('domain-keyed-update'));
  const sheetId = await importCsv(page.request, pid, 'people.csv', 'id,name\na,Ada\nb,Grace\n');
  await openProject(page, pid, sheetId);
  await openImportWorkspace(page);
  await previewTableFile(page, { name: 'update.csv', mimeType: 'text/csv',
    buffer: Buffer.from('id,name\na,Augusta\nz,Unmatched\n') });
  await reviewTableColumns(page);
  const destination = page.locator('.table-import-flow').getByRole('region', { name: 'Import', exact: true });
  await destination.getByRole('radio', { name: 'Add to an existing sheet' }).check();
  await destination.getByRole('combobox', { name: 'Existing sheet', exact: true }).selectOption(String(sheetId));
  await expect(destination.getByLabel('Map id')).toHaveValue(/^existing:/);
  await destination.getByRole('button', { name: 'Update matching rows', exact: true }).click();
  await destination.getByRole('group', { name: 'Match rows using' }).getByRole('checkbox', { name: 'id', exact: true }).check();
  await destination.getByLabel('When imported values change').selectOption('incoming_wins');
  await page.screenshot({ path: 'test-results/domain-keyed-import.png', fullPage: true });
  expect(await commitTableImport(page, pid)).toBe(sheetId);
  await expect.poll(async () => {
    const data = await sheetData(page.request, pid, sheetId);
    const id = String(data.columns.find(column => column.name === 'name')!.id);
    return data.rows.map(row => row.cells[id]);
  }).toEqual(['Augusta', 'Grace']);
});

test('saved results can be reapplied from Review without another provider run', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('domain-reapply'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', 'title\nBudget\nRoad\n');
  const runId = await runAndWait(page.request, pid, {
    action_id: 'map.template', scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: { template: { text: 'Story: {{title}}' } }, output_names: { rendered: 'headline' },
    idempotency_key: `template-${pid}`,
  });
  await openProject(page, pid, sheetId);
  await openReviewRoute(page, pid, sheetId);
  await page.getByRole('button', { name: 'Results', exact: true }).click();
  await page.getByRole('button', { name: 'Reapply saved results', exact: true }).click();
  const completed = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/projects/${pid}/actions/v1/run`
    && response.request().postDataJSON()?.action_id === 'run.reapply');
  await page.screenshot({ path: 'test-results/domain-reapply-confirm.png', fullPage: true });
  await page.getByRole('button', { name: 'Reapply results', exact: true }).click();
  const response = await completed;
  expect(response.ok()).toBeTruthy();
  expect(response.request().postDataJSON()).toMatchObject({ params: { source_run_id: runId } });
  await expect(page.getByTestId('reapply-results-confirm')).toHaveCount(0);
  // A successful action receipt is insufficient: the copied cells must also
  // survive the real grid response contract and remain readable.
  const data = await sheetData(page.request, pid, sheetId);
  const outputs = data.columns.filter(column => column.name !== 'title');
  expect(outputs).toHaveLength(1);
  for (const output of outputs) {
    expect(data.rows.map(row => row.cells[String(output.id)])).toEqual(['Story: Budget', 'Story: Road']);
    for (const row of data.rows) expect(row.meta?.[String(output.id)]).toMatchObject({
      current_value_ref: { kind: 'copied_result', retained_result: { run_id: runId, row_id: row.id, column_id: output.id } },
    });
  }
  await page.screenshot({ path: 'test-results/domain-reapply.png', fullPage: true });
});
