import { expect, test } from '@playwright/test';
import {
  clickHeaderMenu,
  clickRunButton,
  createProject,
  importCsv,
  openProject,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';

test('clean this column starts from the header menu and preserves the source', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-clean-column'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'agencies.csv',
    'agency\n"  NEW YORK dept. of health "\n"New York Department of Health"\nN/A\n',
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await clickHeaderMenu(page, columns, 'agency');
  await expect(page.getByTestId('grid-column-header-menu')).toBeVisible();
  // Configure-first (workbench-ia-action-drawer-v1): the caret menu's column
  // action opens the drawer pre-bound to `agency` instead of running directly.
  // Accept the form defaults and run it from the drawer.
  await page.getByTestId('header-menu-action-map.clean_column').click();
  await expect(page.getByTestId('generated-action-form')).toBeVisible({ timeout: 20_000 });
  // The generated form derives a specific, collision-safe destination from
  // the pre-bound source column. Accept that default and run.
  await expect(page.getByTestId('field-output-cleaned')).toHaveValue('agency_clean');
  await clickRunButton(page);

  await expect.poll(async () => {
    const data = await sheetData(page.request, pid, sheetId);
    const names = data.columns.map((column) => column.name);
    if (!names.includes('agency_clean')) return null;
    const source = data.columns.find((column) => column.name === 'agency');
    const clean = data.columns.find((column) => column.name === 'agency_clean');
    if (!source || !clean) return null;
    return {
      source: data.rows.map((row) => row.cells[String(source.id)]),
      clean: data.rows.map((row) => row.cells[String(clean.id)]),
    };
  }, { timeout: 30_000 }).toEqual({
    source: ['  NEW YORK dept. of health ', 'New York Department of Health', 'N/A'],
    clean: ['New York Department of Health', 'New York Department of Health', null],
  });

  // map.clean_column is mechanical: it writes one column, no
  // confidence/justification support columns, and its rows auto-verify. So a
  // clean run does NOT flood the review queue.
  const columnsAfter = await sheetColumns(page.request, pid, sheetId);
  expect(columnsAfter).not.toContain('cleaned_confidence');
  expect(columnsAfter).not.toContain('cleaned_justification');
  const bundlesPage = await (await page.request.get(`/api/projects/${pid}/review/bundles`)).json();
  expect(bundlesPage.bundles).toEqual([]);
});
