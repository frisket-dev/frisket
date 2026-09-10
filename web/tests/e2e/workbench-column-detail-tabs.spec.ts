import { expect, test } from '@playwright/test';
import {
  clickHeader,
  importCsv,
  listSheets,
  openProject,
  patchColumn,
  createProject,
  projectIdByName,
  sheetColumns,
} from './helpers';

test('column drawer settings and runs use columnDetail descriptor tabs', async ({
  page,
}) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  const sheets = await listSheets(page.request, pid);
  const columns = await sheetColumns(page.request, pid, sheets[0].id);

  await openProject(page, pid);
  await clickHeader(page, columns, 'beat');

  const drawer = page.getByTestId('column-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId('column-type-select')).toBeVisible();
  await expect(drawer.getByTestId('column-versions')).toBeVisible();

  const settingsSection = drawer.getByTestId(
    'workbench-contribution-frisket-core-column-inspector-section-settings',
  );
  await expect(settingsSection).toHaveAttribute('data-schema-version', 'frisket.column_inspector.section.v1');
  await expect(settingsSection).toHaveAttribute('data-contribution-id', 'frisket.core.column_inspector.section.settings');
  await expect(settingsSection).toHaveAttribute('data-host', 'columnInspector');
  await expect(settingsSection).toHaveAttribute('data-mode', 'section');
  await expect(settingsSection).toHaveAttribute('data-tab-host', 'columnDetail');
  await expect(settingsSection).toHaveAttribute('data-tab-mode', 'tab');
  await expect(settingsSection).toHaveAttribute('data-runtime-component-key', 'core.columnInspector.ColumnSettings');
  await expect(settingsSection).toHaveAttribute('data-required-capabilities', /column\.update/);

  const runsSection = drawer.getByTestId(
    'workbench-contribution-frisket-core-column-inspector-section-runs',
  );
  await expect(runsSection).toHaveAttribute('data-schema-version', 'frisket.column_inspector.section.v1');
  await expect(runsSection).toHaveAttribute('data-contribution-id', 'frisket.core.column_inspector.section.runs');
  await expect(runsSection).toHaveAttribute('data-host', 'columnInspector');
  await expect(runsSection).toHaveAttribute('data-mode', 'section');
  await expect(runsSection).toHaveAttribute('data-tab-host', 'columnDetail');
  await expect(runsSection).toHaveAttribute('data-tab-mode', 'tab');
  await expect(runsSection).toHaveAttribute('data-runtime-component-key', 'core.columnInspector.ColumnRuns');
  await expect(runsSection).toHaveAttribute('data-required-capabilities', /column\.runs\.list/);
  await expect(runsSection).toHaveAttribute('data-required-capabilities', /column\.backfill/);
});

test('column drawer automatically shows stats for small columns', async ({ page }) => {
  const pid = await createProject(page.request, 'Column stats e2e');
  const sheetId = await importCsv(
    page.request,
    pid,
    'stats.csv',
    [
      'name,score,bytes,status,seen_at',
      'Ada,10,7906,open,2026-01-01',
      'Grace Hopper,30,137689,open,2026-01-03',
      ',20,27281,closed,2026-01-02',
    ].join('\n'),
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const bytes = columns.find((column) => column.name === 'bytes');
  if (!bytes) throw new Error('bytes column missing');
  await patchColumn(page.request, pid, bytes.id, { format: 'filesize' });

  await openProject(page, pid);
  await clickHeader(page, columns, 'score');

  const drawer = page.getByTestId('column-drawer');
  await expect(drawer.getByTestId('column-stats-result')).toBeVisible();
  await expect(drawer.getByTestId('column-stats-rows')).toHaveText('3');
  await expect(drawer.getByTestId('column-stats-missing')).toHaveText('0');
  await expect(drawer.getByTestId('column-stats-numeric')).toContainText('Mean');
  await expect(drawer.getByTestId('column-stats-numeric')).toContainText('20');
  await expect(drawer.getByTestId('column-stats-histogram')).toBeVisible();
  await expect(drawer.getByTestId('column-stats-top-values')).toContainText('10');

  await clickHeader(page, columns, 'name');
  await expect(drawer.getByTestId('column-stats-missing')).toHaveText('1');
  await expect(drawer.getByTestId('column-stats-text')).toContainText('Text');
  await expect(drawer.getByTestId('column-stats-shortest')).toContainText('Ada');
  await expect(drawer.getByTestId('column-stats-longest')).toContainText('Grace Hopper');

  await clickHeader(page, columns, 'bytes');
  await expect(drawer.getByTestId('column-stats-numeric')).toContainText('File size');
  await expect(drawer.getByTestId('column-stats-numeric')).toContainText('7.72 KB');
  await expect(drawer.getByTestId('column-stats-numeric')).toContainText('134.5 KB');
  await expect(drawer.getByTestId('column-stats-histogram')).toContainText('KB');
  await expect(drawer.getByTestId('column-stats-top-values')).toContainText('7.72 KB');
});

test('large column stats require explicit analyze click', async ({ page }) => {
  const pid = await createProject(page.request, 'Large column stats e2e');
  const sheetId = await importCsv(
    page.request,
    pid,
    'large-stats.csv',
    ['score', '10', '20', '30'].join('\n'),
  );
  const columns = await sheetColumns(page.request, pid, sheetId);
  const score = columns.find((column) => column.name === 'score');
  if (!score) throw new Error('score column missing');

  let forced = false;
  await page.route(
    `/api/projects/${pid}/sheets/${sheetId}/columns/${score.id}/stats**`,
    async (route) => {
      const url = new URL(route.request().url());
      forced = url.searchParams.get('force') === 'true';
      if (!forced) {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            schema_version: 'frisket.column_stats.v1',
            sheet_id: sheetId,
            column: { id: score.id, name: 'score', type: 'integer' },
            row_count: 100001,
            threshold: 100000,
            computed: false,
            requires_manual_analyze: true,
          }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schema_version: 'frisket.column_stats.v1',
          sheet_id: sheetId,
          column: { id: score.id, name: 'score', type: 'integer' },
          row_count: 100001,
          threshold: 100000,
          computed: true,
          requires_manual_analyze: false,
          missing: 0,
          present: 100001,
          distinct: 3,
          top_values: [{ value: '10000', count: 40000 }],
          numeric: {
            count: 100001,
            mean: 20,
            median: 20,
            min: 10,
            max: 30,
            histogram: [{ min: 10, max: 30, count: 100001 }],
          },
          text: null,
          date: null,
          json_types: [{ type: 'int', count: 100001 }],
        }),
      });
    },
  );

  await openProject(page, pid);
  await clickHeader(page, columns, 'score');

  const drawer = page.getByTestId('column-drawer');
  await expect(drawer.getByTestId('column-stats-deferred')).toContainText('100,001 rows');
  await expect(drawer.getByTestId('column-stats-result')).toHaveCount(0);
  await drawer.getByTestId('analyze-column-button').click();
  await expect(drawer.getByTestId('column-stats-result')).toBeVisible();
  await expect(drawer.getByTestId('column-stats-rows')).toHaveText('100,001');
  await expect(drawer.getByTestId('column-stats-top-values')).toContainText('10,000');
  expect(forced).toBeTruthy();
});
