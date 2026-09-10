import { expect, test } from '@playwright/test';
import {
  createProject,
  finishLocalQueuedRun,
  importCsv,
  openAction,
  selectActionInputColumns,
  sheetData,
  uniqueName,
} from './helpers';

test('deploy smoke imports, runs a deterministic action, and reads results', async ({ page }) => {
  const health = await page.request.get('/api/health');
  expect(health.ok()).toBeTruthy();

  const pid = await createProject(page.request, uniqueName('deploy-smoke'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'smoke.csv',
    'note\n"contract worth $4,200 total"\n"routine agenda item"\n',
  );

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('sheet-stats')).toHaveText(/2 rows · 1 columns?/);

  await openAction(page, 'map.regex_extract');
  await selectActionInputColumns(page, 'note');
  await expect(page.getByTestId('field-pattern')).toBeVisible();
  await page.getByTestId('field-pattern').fill('\\$[0-9,]+');
  const runResponse = page.waitForResponse((response) =>
    response.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
    response.request().method() === 'POST',
  );
  await page.getByTestId('generated-action-run').click();
  const { run_id: runId } = (await (await runResponse).json()) as { run_id: number | null };
  if (runId == null) throw new Error('regex v1 run did not return a run id');
  finishLocalQueuedRun(pid, runId);

  await expect(page.getByTestId('sheet-stats')).toHaveText('2 rows · 2 columns', {
    timeout: 30_000,
  });

  await expect
    .poll(
      async () => {
        const latest = await sheetData(page.request, pid, sheetId);
        const extracted = latest.columns.find((col) => col.name === 'extracted');
        if (!extracted) return null;
        return latest.rows[0].cells[String(extracted.id)] ?? null;
      },
      { timeout: 30_000 },
    )
    .toBe('$4,200');

  const data = await sheetData(page.request, pid, sheetId);
  const extracted = data.columns.find((col) => col.name === 'extracted');
  expect(extracted).toBeTruthy();
  expect(data.rows[1].cells[String(extracted!.id)] ?? null).toBeNull();
});
