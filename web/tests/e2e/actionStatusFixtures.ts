import type { Page } from '@playwright/test';

export interface V1ActionResultFixture {
  actionId?: string;
  actionKind?: string;
  errors?: Array<Record<string, unknown>>;
  jobId?: number | null;
  outputs?: Array<Record<string, unknown>>;
  projectId: string;
  receiptId?: string;
  runId: number | null;
  status?: string;
  warnings?: Array<Record<string, unknown>>;
}

export interface V1ActionStatusFixture {
  actionKind: string;
  actionName: string;
  completed?: number;
  cost?: number | null;
  error?: string | null;
  failed?: number;
  live?: boolean;
  projectId: string;
  runId: number;
  sheetId?: number;
  status?: string;
  total?: number;
}

export interface V1ActionRunRecorder {
  v1Posts: Array<Record<string, unknown>>;
}

export interface V1ActionStatusRecorder {
  statusPolls: string[];
}

export interface StubbedV1ActionRecorder extends V1ActionRunRecorder, V1ActionStatusRecorder {
  legacyPosts: Array<Record<string, unknown>>;
}

type StatusFactory =
  | V1ActionStatusFixture
  | null
  | ((runId: number) => V1ActionStatusFixture | null);

export function v1ActionResultPayload(
  fixture: V1ActionResultFixture,
  posted?: Record<string, unknown>,
): Record<string, unknown> {
  const actionKind = fixture.actionKind ?? String(posted?.kind ?? '');
  return {
    schema_version: 'frisket.action_result.v1',
    action: {
      kind: actionKind,
      action_id: fixture.actionId ?? `act-${fixture.runId ?? 'direct'}`,
    },
    status: fixture.status ?? 'queued',
    project_id: fixture.projectId,
    run_id: fixture.runId,
    ...(fixture.jobId !== undefined ? { job_id: fixture.jobId } : {}),
    receipt_id: fixture.receiptId ?? `receipt-${fixture.runId ?? 'direct'}`,
    op_ids: [],
    outputs: fixture.outputs ?? [],
    warnings: fixture.warnings ?? [],
    errors: fixture.errors ?? [],
  };
}

export function v1ActionStatusPayload(
  fixture: V1ActionStatusFixture,
): Record<string, unknown> {
  const status = fixture.status ?? 'queued';
  const total = fixture.total ?? 1;
  const completed = fixture.completed ?? 0;
  const failed = fixture.failed ?? 0;
  const cost = fixture.cost ?? 0;
  const live = fixture.live ?? false;
  return {
    schema_version: 'frisket.actions.v1',
    action: 'run_status',
    project_id: fixture.projectId,
    run: {
      id: fixture.runId,
      sheet_id: fixture.sheetId ?? 1,
      action_kind: fixture.actionKind,
      action_name: fixture.actionName,
      status,
      total_rows: total,
      completed_rows: completed,
      failed_rows: failed,
      cost_actual: cost,
      cost_estimate: cost,
      public_status: {
        run_id: fixture.runId,
        action_kind: fixture.actionKind,
        action_name: fixture.actionName,
        status,
        total,
        completed,
        failed,
        cost,
        live,
        // ActionRunPublicStatus requires `timing`; without it the frontend's
        // validateActionRunStatus rejects the poll response (the job store's own
        // run poll then silently drops it and the status bar never leaves its
        // optimistic 'queued'). Emit the full ActionRunTiming shape so mocked
        // status polls validate exactly like the real endpoint.
        timing: {
          started_at: null,
          finished_at: null,
          elapsed_seconds: null,
          processed_rows: completed,
          remaining_rows: Math.max(0, total - completed),
          processed_rows_per_second: null,
          eta_seconds: null,
          estimated_finish_at: null,
          queue_wait_seconds: null,
          job_elapsed_seconds: null,
        },
        ...(fixture.error !== undefined ? { error: fixture.error } : {}),
      },
    },
  };
}

export async function blockLegacyProjectRun(
  page: Page,
  projectId: string,
  message: string,
): Promise<{ legacyPosts: Array<Record<string, unknown>> }> {
  const legacyPosts: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${projectId}/run`, async (route) => {
    legacyPosts.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: message }),
    });
  });
  return { legacyPosts };
}

export async function routeV1ActionRun(
  page: Page,
  projectId: string,
  fixture: Omit<V1ActionResultFixture, 'projectId'>,
): Promise<V1ActionRunRecorder> {
  const v1Posts: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${projectId}/actions/v1/run`, async (route) => {
    const posted = route.request().postDataJSON() as Record<string, unknown>;
    v1Posts.push(posted);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(v1ActionResultPayload({ ...fixture, projectId }, posted)),
    });
  });
  return { v1Posts };
}

export async function routeV1ActionStatus(
  page: Page,
  projectId: string,
  fixture: StatusFactory,
): Promise<V1ActionStatusRecorder> {
  const statusPolls: string[] = [];
  await page.route(`**/api/projects/${projectId}/actions/runs/*/status`, async (route) => {
    const matchedRunId = route.request().url().match(/\/actions\/runs\/([^/]+)\/status$/)?.[1];
    const runId = Number(matchedRunId);
    const resolved = typeof fixture === 'function' ? fixture(runId) : fixture;
    if (!resolved || !Number.isFinite(runId) || resolved.runId !== runId) {
      await route.continue();
      return;
    }
    statusPolls.push(route.request().url());
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(v1ActionStatusPayload(resolved)),
    });
  });
  return { statusPolls };
}

export async function stubV1QueuedAction(
  page: Page,
  projectId: string,
  options: {
    actionKind: string;
    actionName: string;
    actionId?: string;
    jobId?: number | null;
    legacyError: string;
    receiptId: string;
    runId: number;
    status?: string;
    total?: number;
  },
): Promise<StubbedV1ActionRecorder> {
  const legacy = await blockLegacyProjectRun(page, projectId, options.legacyError);
  const actionResult: Omit<V1ActionResultFixture, 'projectId'> = {
    actionKind: options.actionKind,
    receiptId: options.receiptId,
    runId: options.runId,
    status: options.status ?? 'queued',
  };
  if (options.actionId !== undefined) actionResult.actionId = options.actionId;
  if (options.jobId !== undefined) actionResult.jobId = options.jobId;
  const run = await routeV1ActionRun(page, projectId, actionResult);
  const status = await routeV1ActionStatus(page, projectId, {
    actionKind: options.actionKind,
    actionName: options.actionName,
    projectId,
    runId: options.runId,
    status: options.status ?? 'queued',
    total: options.total,
  });
  return {
    legacyPosts: legacy.legacyPosts,
    statusPolls: status.statusPolls,
    v1Posts: run.v1Posts,
  };
}
