// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';

import { createActionCompletionApi } from '../../src/api/actionCompletion';
import type {
  ResolvedRunActionInvocation,
  V1ActionResult,
} from '../../src/api/v1ActionSession';

function deferred<T>(): { promise: Promise<T>; resolve(value: T): void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function queuedResult(): V1ActionResult {
  return {
    schema_version: 'frisket.action_result.v1',
    action: {
      kind: 'map.extract',
      action_id: 'action-completion-extract',
    },
    status: 'queued',
    project_id: 'completion-project-a',
    run_id: 91,
    job_id: 92,
    receipt_id: 'receipt-completion-91',
    outputs: [],
    errors: [],
  };
}

function completedProgress() {
  return {
    schema_version: 'frisket.actions.v1',
    action: 'run_status',
    project_id: 'completion-project-a',
    run: {
      id: 91,
      sheet_id: 7,
      action_kind: 'map.extract',
      action_name: 'Extract items',
      status: 'completed',
      total_rows: 2,
      completed_rows: 2,
      failed_rows: 0,
      cost_actual: 0,
      cost_estimate: 0,
      public_status: {
        run_id: 91,
        action_kind: 'map.extract',
        action_name: 'Extract items',
        status: 'completed',
        total: 2,
        completed: 2,
        failed: 0,
        cost: 0,
        error: null,
        row_errors: null,
        halted_code: null,
        halted_reason: null,
        stalled_reason: null,
        queue: null,
      },
    },
  };
}

function runningProgress() {
  const progress = completedProgress();
  return {
    ...progress,
    run: {
      ...progress.run,
      status: 'running',
      completed_rows: 0,
      public_status: {
        ...progress.run.public_status,
        status: 'running',
        completed: 0,
      },
    },
  };
}

function completedReceipt() {
  return {
    schema_version: 'frisket.receipt.v1',
    receipt_id: 'receipt-completion-91',
    project_id: 'completion-project-a',
    action_id: 'action-completion-extract',
    action_kind: 'map.extract',
    run_id: 91,
    op_ids: [17],
    idempotency_key: 'completion-key-91',
    params_hash: 'sha256:completion-91',
    status: 'completed',
    inputs: [],
    outputs: [{
      name: 'items',
      ref: {
        kind: 'named_result',
        sheet_id: 7,
        column_id: 44,
        row_ids: [1, 'ignored', 2],
        route: 'items',
      },
    }],
    provider_use: [],
    evidence: [],
    errors: [],
  };
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('run-backed action completion domain', () => {
  it('continues polling a healthy run past five minutes until it completes', async () => {
    vi.useFakeTimers();
    const projectId = 'completion-project-a';
    let statusReads = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === `/api/projects/${projectId}/actions/runs/91/status`) {
        statusReads += 1;
        return jsonResponse(statusReads > 300 ? completedProgress() : runningProgress());
      }
      if (path === `/api/projects/${projectId}/actions/v1/receipts/receipt-completion-91`) {
        return jsonResponse(completedReceipt());
      }
      throw new Error(`Unexpected action completion request: ${path}`);
    }));
    const controller = new AbortController();
    const completion = createActionCompletionApi(
      (status, payload) => new Error(`mapped ${status}: ${JSON.stringify(payload)}`),
    );
    const outcome = completion.waitForRunCompletion(
      queuedResult(),
      'map.extract did not complete',
      { projectId, signal: controller.signal },
    ).then(
      (value) => ({ value, error: null as unknown }),
      (error: unknown) => ({ value: null, error }),
    );

    await vi.advanceTimersByTimeAsync(301_000);

    const settled = await outcome;
    expect(settled.error).toBeNull();
    expect(settled.value).toMatchObject({ status: 'completed' });
    expect(statusReads).toBe(301);
  });

  it('keeps status and receipt reads on the captured invocation across an ambient project switch', async () => {
    const projectA = 'completion-project-a';
    const projectB = 'completion-project-b';
    const statusResponse = deferred<Response>();
    const requests: Array<{
      path: string;
      signal: AbortSignal | null | undefined;
    }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      requests.push({ path, signal: init?.signal });
      if (path === `/api/projects/${projectA}/actions/runs/91/status`) {
        return statusResponse.promise;
      }
      if (path === `/api/projects/${projectA}/actions/v1/receipts/receipt-completion-91`) {
        return jsonResponse(completedReceipt());
      }
      throw new Error(`Unexpected action completion request: ${path}`);
    }));
    const controller = new AbortController();
    const invocation: ResolvedRunActionInvocation = {
      projectId: projectA,
      signal: controller.signal,
    };
    const completion = createActionCompletionApi(
      (status, payload) => new Error(`mapped ${status}: ${JSON.stringify(payload)}`),
    );
    const pending = completion.waitForRunCompletion(
      queuedResult(),
      'map.extract did not complete',
      invocation,
    );
    statusResponse.resolve(jsonResponse(completedProgress()));

    await expect(pending).resolves.toEqual({
      ...queuedResult(),
      status: 'completed',
      outputs: [{
        kind: 'named_result',
        name: 'items',
        sheet_id: 7,
        column_id: 44,
        row_ids: [1, 2],
        ref: {
          kind: 'named_result',
          sheet_id: 7,
          column_id: 44,
          row_ids: [1, 'ignored', 2],
          route: 'items',
        },
      }],
      errors: [],
    });
    expect(requests.map(({ path }) => path)).toEqual([
      `/api/projects/${projectA}/actions/runs/91/status`,
      `/api/projects/${projectA}/actions/v1/receipts/receipt-completion-91`,
    ]);
    expect(requests.every(({ signal }) => signal === controller.signal)).toBe(true);
    expect(requests.some(({ path }) => path.includes(`/projects/${projectB}/`))).toBe(false);
  });
});
