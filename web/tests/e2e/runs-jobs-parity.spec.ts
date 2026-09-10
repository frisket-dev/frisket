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
// never write a queue Job row. The jobs-list
// service (server/services/action_runs.py's list_jobs/job_detail) unions
// `runs` rows lacking a covering queue job into the SAME frisket.job.v1 shape
// (jobs/projection.py's run_inline_job_payload), server-side, so every run
// surfaces regardless of execution path.
//
// `map.template` is a real, deterministic, $0, model-free typed action and
// exercises that inline projection directly.

test('an INLINE run is visible in the jobs listing while running (real backend, no queue job), and flips to terminal with progress on completion', async ({
  request,
}) => {
  const pid = await createProject(request, uniqueName('parity-api'));
  // Large enough that the synchronous run takes measurable wall-clock time,
  // so a concurrent poll has a real window to observe status='running'
  // before the launching request itself returns.
  const rowCount = 9000;
  const rows = Array.from({ length: rowCount }, (_, i) => `row ${i}`).join('\n');
  const sheetId = await importCsv(request, pid, 'rows.csv', `snippet\n${rows}\n`);

  const runPromise = request.post(`/api/projects/${pid}/actions/v1/run`, {
    data: {
      action_id: 'map.template',
      scope: { kind: 'sheet_rows', sheet_id: sheetId },
      params: { template: { text: 'note: {{snippet}}' } },
      output_names: { rendered: 'note' },
      idempotency_key: `runs-jobs-parity-api-${pid}`,
    },
  });

  interface WireJob {
    job_id: number;
    kind: string;
    run_id: number | null;
    status: string;
  }

  let sawRunning = false;
  let sawJobId: number | null = null;
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    const listed = await request.get(`/api/projects/${pid}/actions/jobs`);
    expect(listed.ok()).toBeTruthy();
    const jobs = ((await listed.json()).jobs ?? []) as WireJob[];
    const entry = jobs.find((j) => j.kind === 'run.inline');
    if (entry) {
      sawJobId = entry.job_id;
      if (entry.status === 'running') {
        sawRunning = true;
        break;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 15));
  }

  const runResponse = await runPromise;
  expect(runResponse.ok()).toBeTruthy();
  const result = (await runResponse.json()) as {
    status: string;
    run_id: number | null;
    job_id: number | null;
  };
  expect(result.status).toBe('completed');
  expect(result.job_id).toBeNull(); // INLINE placement never mints a queue job id.
  const runId = result.run_id;
  expect(runId).not.toBeNull();

  // The polling loop caught the run mid-flight as a jobs-list entry, correctly
  // reporting the 'running' state (queued/running/progress/terminal parity).
  expect(sawRunning).toBe(true);
  expect(sawJobId).toBe(-(runId as number));

  const after = await request.get(`/api/projects/${pid}/actions/jobs`);
  const afterJobs = ((await after.json()).jobs ?? []) as Array<
    WireJob & {
      action_kind: string | null;
      result_summary: Record<string, unknown>;
      payload_ref: Record<string, unknown>;
    }
  >;
  const finalEntry = afterJobs.find((j) => j.run_id === runId);
  expect(finalEntry).toBeTruthy();
  expect(finalEntry?.job_id).toBe(-(runId as number));
  expect(finalEntry?.status).toBe('done'); // terminal
  expect(finalEntry?.action_kind).toBe('map.template');
  expect(finalEntry?.payload_ref).toEqual({ kind: 'run', run_id: runId });
  expect(finalEntry?.result_summary.total).toBe(rowCount);
  expect(finalEntry?.result_summary.completed).toBe(rowCount); // progress
  expect(finalEntry?.result_summary.failed).toBe(0);

  // A run already covered by a real queue job is never double-projected — proven at the unit level in
  // tests/server/test_runs_are_jobs_parity.py; here, re-fetching by the SAME run_id
  // never yields two entries.
  expect(afterJobs.filter((j) => j.run_id === runId)).toHaveLength(1);
});

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
