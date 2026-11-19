import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openProject,
  runAndWait,
  TINY_CSV,
  uniqueName,
} from './helpers';

// Some actions execute synchronously inside the request that launched them and
// never write a queue Job row; the jobs listing still surfaces them as
// `run.inline` entries.

test('the jobs panel (default-active tab) lists an INLINE run and its detail, terminal', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('parity-ui'));
  const sheetId = await importCsv(request, pid, 'stories.csv', TINY_CSV);
  const runId = await runAndWait(request, pid, {
    action_id: 'map.template',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: { template: { text: 'note: {{snippet}}' } },
    output_names: { rendered: 'note' },
    idempotency_key: `runs-jobs-parity-ui-${pid}`,
  });

  await openProject(page, pid, sheetId);

  const jobsTab = page.getByTestId('bottom-dock-tab-jobs');
  await expect(jobsTab).toHaveAttribute('aria-selected', 'true');

  const row = page.getByTestId(`bottom-dock-job-${-runId}`);
  await expect(row).toBeVisible();
  await expect(row).toHaveAttribute('data-job-kind', 'run.inline');
  await expect(row).toHaveAttribute('data-action-kind', 'map.template');

  await row.click();
  const detail = page.locator('aside.bottom-dock-detail');
  await expect(detail).toContainText(String(runId));
  await expect(detail).toContainText('complete');
});
