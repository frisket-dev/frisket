import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import {
  clickHeaderMenu, clickRunButton, createProject, importCsv, openAction, openProject,
  sheetColumns, sheetData, uniqueName,
} from '../helpers';
import { captureCheckpoint, publishCaptureBundle } from './capture';

test.use({ deviceScaleFactor: 2, viewport: { width: 1600, height: 1000 } });
test.skip(!process.env.FRISKET_DOCS_PROOF_DIR, 'Run through the isolated documentation capture harness');
test.beforeAll(() => {
  const root = path.resolve('..');
  const git = (...args: string[]) => execFileSync('git', args, { cwd: root, encoding: 'utf8' }).trim();
  expect(process.env.FRISKET_DOCS_REVISION).toBe(git('rev-parse', 'HEAD'));
  const changes = git('status', '--porcelain', '--untracked-files=no').split('\n').filter(Boolean);
  const overlay = process.env.FRISKET_DOCS_PACKAGE_OVERLAY === '1' ? ['M web/package.json'] : [];
  expect(changes.filter(change => !overlay.includes(change))).toEqual([]);
});

test('clean imported values and join matching rows from another sheet', async ({ page }) => {
  test.setTimeout(180_000);
  const pid = await createProject(page.request, uniqueName('docs-tables'));
  const leftId = await importCsv(page.request, pid, 'payments.csv',
    'agency,amount\n"  NEW YORK dept. of health ",100\n"New York Department of Health",200\nN/A,50\n');
  const rightId = await importCsv(page.request, pid, 'agencies.csv',
    'agency,region\nNew York Department of Health,East\nCalifornia Department of Health,West\n');
  const originalColumns = await sheetColumns(page.request, pid, leftId);
  await openProject(page, pid, leftId);
  await expect(page.getByTestId('grid')).toBeVisible();

  await publishCaptureBundle(path.join(process.env.FRISKET_DOCS_PROOF_DIR!, 'table-workflows'), {
    appRevision: process.env.FRISKET_DOCS_REVISION!,
    fixture: { id: 'table-workflows-fixture', revision: 1 },
  }, async stage => {
    const captures = [];
    await clickHeaderMenu(page, originalColumns, 'agency');
    await page.getByTestId('header-menu-action-map.clean_column').click();
    await expect(page.getByTestId('field-output-cleaned')).toHaveValue('agency_clean');
    captures.push(await captureCheckpoint(page, { id: 'table-clean-settings', root: page.getByTestId('action-panel'), annotations: [] }, stage));
    await clickRunButton(page);
    await expect.poll(async () => {
      const data = await sheetData(page.request, pid, leftId);
      const source = data.columns.find(column => column.name === 'agency');
      const clean = data.columns.find(column => column.name === 'agency_clean');
      if (!source || !clean) return null;
      return {
        source: data.rows.map(row => row.cells[String(source.id)]),
        clean: data.rows.map(row => row.cells[String(clean.id)]),
      };
    }, { timeout: 30_000 }).toEqual({
      source: ['  NEW YORK dept. of health ', 'New York Department of Health', 'N/A'],
      clean: ['New York Department of Health', 'New York Department of Health', null],
    });
    await openProject(page, pid, leftId);
    await expect(page.getByTestId('grid')).toBeVisible();
    captures.push(await captureCheckpoint(page, { id: 'table-clean-result', root: page.getByTestId('workbench-region-mainView'), annotations: [] }, stage));

    await openAction(page, 'derive.join');
    const join = page.getByTestId('tabular-join-form');
    await expect(join).toBeVisible();
    await expect(page.getByTestId('field-join_right_sheet')).toHaveValue(String(rightId));
    await page.getByTestId('field-join_key_left-0').selectOption('agency_clean');
    await page.getByTestId('field-join_key_right-0').selectOption('agency');
    await expect(page.getByTestId('field-join_how')).toHaveValue('inner');
    await page.getByTestId('field-target_sheet_name').fill('Payments with regions');
    captures.push(await captureCheckpoint(page, { id: 'table-join-settings', root: page.getByTestId('action-panel'), annotations: [] }, stage));
    await page.getByTestId('generated-action-preview').click();
    await expect(page.getByTestId('preview-tab-banner')).toHaveAttribute('data-status', 'done', { timeout: 60_000 });
    await page.getByTestId('action-drawer-close').click();
    captures.push(await captureCheckpoint(page, { id: 'table-join-preview', root: page.getByTestId('workbench-region-mainView'), annotations: [] }, stage));
    const response = page.waitForResponse(r => r.url().includes(`/api/projects/${pid}/actions/v1/run`) && r.request().method() === 'POST');
    await page.getByTestId('preview-run-for-real-button').click();
    const resultResponse = await response;
    const body = await resultResponse.text();
    expect(resultResponse.ok(), body).toBeTruthy();
    const result = JSON.parse(body);
    expect(result.action.kind).toBe('derive.join');
    expect(result.status).toBe('completed');
    const output = result.outputs.find((item: { kind: string }) => item.kind === 'sheet');
    expect(output).toBeTruthy();
    const joined = await sheetData(page.request, pid, Number(output.sheet_id));
    expect(joined.total).toBe(2);
    const region = joined.columns.find(column => column.name === 'region');
    expect(region).toBeDefined();
    expect(joined.rows.map(row => row.cells[String(region!.id)])).toEqual(['East', 'East']);
    await expect(page).toHaveURL(new RegExp(`/p/${pid}/s/${output.sheet_id}(?:[/?#]|$)`));
    await expect(page.getByTestId('grid')).toBeVisible();
    captures.push(await captureCheckpoint(page, { id: 'table-join-result', root: page.getByTestId('workbench-region-mainView'), annotations: [] }, stage));
    return captures;
  });
});
