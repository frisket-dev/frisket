// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { webcrypto } from 'node:crypto';

import { createProjectApi } from '../../src/api/real';

let api = createProjectApi('test-project');
beforeEach(() => vi.stubGlobal('crypto', webcrypto));
import type { RegisteredActionRequest, DeriveCompositeRequest } from '../../src/api/types';
import type { ClassifyParams } from '../../src/generated/actionTypes';

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

function sheetDataWire(columnName = 'result') {
  return {
    columns: [
      {
        id: 1,
        name: 'source',
        type: 'text',
        ai_generated: false,
        format: null,
        current_run_id: null,
        transcript_status: null,
        media_download_candidate: null,
      },
      {
        id: 2,
        name: columnName,
        type: 'text',
        ai_generated: true,
        format: null,
        current_run_id: 77,
        transcript_status: null,
        media_download_candidate: null,
      },
    ],
    rows: [],
    total: 0,
  };
}

function actionResult(runId = 77, status = 'completed', kind = 'map.classify') {
  return {
    schema_version: 'frisket.action_result.v1',
    action: { kind, action_id: `action-${runId}` },
    status,
    run_id: runId,
    receipt_id: `receipt-${runId}`,
    outputs: [],
    errors: [],
  };
}

function runProgressWire(projectId: string, runId = 77, status = 'running') {
  return {
    schema_version: 'frisket.actions.v1',
    action: 'run_status',
    project_id: projectId,
    run: {
      id: runId,
      sheet_id: 7,
      action_kind: 'map.extract',
      action_name: '',
      status,
      total_rows: 2,
      completed_rows: status === 'running' ? 0 : 2,
      failed_rows: 0,
      cost_actual: 0,
      cost_estimate: 0,
      public_status: {
        run_id: runId,
        action_kind: 'map.extract',
        action_name: '',
        status,
        total: 2,
        completed: status === 'running' ? 0 : 2,
        failed: 0,
        cost: 0,
        live: status === 'running',
        timing: {
          started_at: null,
          finished_at: null,
          elapsed_seconds: null,
          processed_rows: status === 'running' ? 0 : 2,
          remaining_rows: status === 'running' ? 2 : 0,
          processed_rows_per_second: null,
          eta_seconds: null,
          estimated_finish_at: null,
          queue_wait_seconds: null,
          job_elapsed_seconds: null,
        },
      },
    },
  };
}

function classifyRequest(
  label: string,
  sheetId = '7',
  targetColumnId = 'result',
  rowIds = [1],
): RegisteredActionRequest {
  return {
    action_id: 'map.classify',
    scope: { kind: 'sheet_rows', sheet_id: Number(sheetId), row_ids: rowIds },
    params: {
      source: ['source'],
      engine: 'llm',
      model: `anthropic/model-${label}`,
      fields: [{ name: 'label', type: 'category', labels: ['one', 'two'] }],
    } satisfies ClassifyParams,
    output_names: { label: targetColumnId },
    idempotency_key: `classify-${label}`,
  };
}

function columnRunsWire(label: string, target: string, runId: number) {
  const run = {
    run_id: runId, action_kind: 'map.classify', action_name: `Classify ${label}`,
    model: `model-${label}`, status: 'completed', spec: classifyRequest(label),
    total_rows: 1, completed_rows: 1, failed_rows: 0, cost_actual: 0,
    started_at: null, finished_at: null, duration_ms: 0, tokens_in: 0, tokens_out: 0,
    current: true,
    human_score: { passed: 0, graded: 0 },
    judge_scores: [],
  };
  return {
    column: { id: 2, name: target, type: 'text', ai_generated: true, current_run_id: runId },
    offset: 0, limit: 20, total: 1, has_more: false, next_offset: null,
    current_run: run, current_run_loaded: true, runs: [run],
  };
}

function deriveRequest(): DeriveCompositeRequest {
  return {
    intent: 'derive_from_extraction', itemField: 'items', sheet_name: 'Derived', extraction: {
      action_id: 'map.extract', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: ['source'], model: 'model-derive', instruction: 'Extract a list of items.',
        fields: [{ name: 'items', type: 'list', items: { type: 'object', properties: { name: { type: 'string' } } } }] },
      output_names: { items: 'items' }, idempotency_key: 'derive-extract-request',
    },
  };
}

function visualCutsRequest(targetColumnId: string) {
  return {
    action_id: 'map.find_visual_cuts',
    scope: { kind: 'sheet_rows' as const, sheet_id: 7 },
    params: { source: 'source' },
    output_names: { cuts: targetColumnId },
    idempotency_key: 'local-visual-cuts',
  };
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('RealApi action invocation safety (WEB-03-4A)', () => {
  it('keeps a pending typed launch on captured project A after ambient binding switches to B', async () => {
    const projectA = 'web03-4a-capture-a';
    const projectB = 'web03-4a-capture-b';
    const response = deferred<Response>();
    const posts: string[] = [];
    const bodies: unknown[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/actions/v1/run')) {
        posts.push(path);
        bodies.push(JSON.parse(String(init?.body)));
        return response.promise;
      }
      if (path === `/api/projects/${projectA}/actions/runs/77/status`) {
        return jsonResponse(runProgressWire(projectA));
      }
      throw new Error(`Unexpected request: ${path}`);
    }));

    const captured = createProjectApi(projectA);
    api = captured;
    const request = classifyRequest('capture');
    const launch = api.runAction(request);
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    api = createProjectApi(projectB);
    response.resolve(jsonResponse(actionResult()));

    await expect(launch).resolves.toMatchObject({ runId: '77' });
    expect(posts).toEqual([`/api/projects/${projectA}/actions/v1/run`]);
    expect(bodies).toEqual([request]);
    expect(await captured.getRunProgress('77')).toMatchObject({
      actionName: 'map.classify', targetColumnId: 'result', targetRowIds: ['1'],
    });
  });

  it('tracks the renamed extraction output in run progress and column metadata', async () => {
    const projectId = 'derive-output-metadata';
    const request = deriveRequest();
    request.extraction.output_names = { items: 'found_items' };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/actions/v1/run')) {
        const body = JSON.parse(String(init?.body)) as RegisteredActionRequest;
        return jsonResponse({
          ...actionResult(77, 'completed', body.action_id),
          outputs: body.action_id === 'map.extract'
            ? [{ kind: 'named_result', name: 'items', ref: {
                sheet_id: 7, column_id: 2, run_id: 77, route: 'items', schema: 'items_list',
              } }]
            : [{ kind: 'sheet', sheet_id: 9 }],
        });
      }
      if (path.endsWith('/actions/runs/77/status')) return jsonResponse(runProgressWire(projectId));
      if (path.includes('/sheets/7/data')) return jsonResponse(sheetDataWire('found_items'));
      throw new Error(`Unexpected request: ${path}`);
    }));
    const projectApi = createProjectApi(projectId);
    await projectApi.runAction(request);
    expect(await projectApi.getRunProgress('77')).toMatchObject({
      targetColumnId: 'found_items', actionName: 'Derive rows',
    });
    const data = await projectApi.getSheetData('7');
    expect(data.columns.find((column) => column.name === 'found_items')?.ai).toMatchObject({
      actionName: 'Derive rows', prompt: 'Extract a list of items.', model: 'model-derive',
    });
  });

  it('keeps every derive read and both POSTs on captured project A after switching ambient to B', async () => {
    const projectA = 'web03-4a-derive-capture-a';
    const projectB = 'web03-4a-derive-capture-b';
    const data = deferred<Response>();
    const urls: string[] = [];
    const requestSignals: Array<AbortSignal | null | undefined> = [];
    const postKinds: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      urls.push(path);
      requestSignals.push(init?.signal);
      if (path === `/api/projects/${projectA}/sheets/7/data?offset=0&limit=0`) {
        return data.promise;
      }
      if (path.endsWith('/actions/v1/run')) {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        postKinds.push(String(body.action_id ?? body.kind));
        if (body.action_id === 'map.extract') {
          return jsonResponse({
            ...actionResult(9901, 'queued'),
            action: { kind: 'map.extract', action_id: 'extract-9901' },
            receipt_id: 'receipt-9901',
          });
        }
        return jsonResponse({
          ...actionResult(9902),
          action: { kind: 'derive.table_from_list', action_id: 'derive-9902' },
          run_id: null,
        });
      }
      if (path.endsWith('/actions/runs/9901/status')) {
        const requestProject = path.includes(projectA) ? projectA : projectB;
        return jsonResponse(runProgressWire(requestProject, 9901, 'completed'));
      }
      if (path.endsWith('/actions/v1/receipts/receipt-9901')) {
        const requestProject = path.includes(projectA) ? projectA : projectB;
        return jsonResponse({
          schema_version: 'frisket.receipt.v1',
          receipt_id: 'receipt-9901',
          project_id: requestProject,
          action_id: 'extract-9901',
          action_kind: 'map.extract',
          run_id: 9901,
          op_ids: [91],
          idempotency_key: 'extract-key',
          params_hash: 'sha256:extract',
          status: 'completed',
          inputs: [],
          outputs: [{
            name: 'items',
            ref: {
              kind: 'named_result',
              sheet_id: 7,
              column_id: 44,
              run_id: 9901,
              op_id: 91,
              route: 'items',
              schema: 'items_list',
              item_schema: {
                type: 'object',
                properties: { name: { type: 'string' } },
              },
              source_action_kind: 'map.extract',
              row_ids: [1, 2],
              may_feed: ['derive.table_from_list'],
            },
          }],
          provider_use: [],
          evidence: [],
          errors: [],
        });
      }
      throw new Error(`Unexpected request: ${path}`);
    }));

    api = createProjectApi(projectA);
    const controller = new AbortController();
    const launch = api.runAction(deriveRequest(), {
      projectId: projectA,
      signal: controller.signal,
    });
    api = createProjectApi(projectB);
    data.resolve(jsonResponse(sheetDataWire()));

    await expect(launch).resolves.toMatchObject({ runId: '9901' });
    expect(postKinds).toEqual(['map.extract', 'derive.table_from_list']);
    expect(urls.every((url) => url.includes(`/projects/${projectA}/`))).toBe(true);
    expect(requestSignals).toHaveLength(4);
    expect(requestSignals.every((signal) => signal === controller.signal)).toBe(true);
  });

  it('stops a derive chain after one accepted extract POST and before any later request', async () => {
    const projectA = 'web03-4a-derive-abort-a';
    const projectB = 'web03-4a-derive-abort-b';
    const extract = deferred<Response>();
    const retryExtract = deferred<Response>();
    const posts: string[] = [];
    const postBodies: Array<Record<string, unknown>> = [];
    const progressReads: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === `/api/projects/${projectA}/sheets/7/data?offset=0&limit=0`) {
        return jsonResponse(sheetDataWire());
      }
      if (path.endsWith('/actions/v1/run')) {
        posts.push(path);
        postBodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        return posts.length === 1 ? extract.promise : retryExtract.promise;
      }
      if (path.includes('/actions/runs/77/status')) {
        progressReads.push(path);
        return jsonResponse({ detail: 'retired derive reached progress' }, 500);
      }
      throw new Error(`Unexpected request: ${path}`);
    }));
    const controller = new AbortController();
    const abortError = new DOMException('retired after extract acceptance', 'AbortError');

    const projectApi = createProjectApi(projectA);
    const launch = projectApi.runAction(deriveRequest(), {
      projectId: projectA,
      signal: controller.signal,
    });
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    controller.abort(abortError);
    api = createProjectApi(projectB);
    extract.resolve(jsonResponse(actionResult(77, 'queued')));

    await expect(launch).rejects.toBe(abortError);
    expect(posts).toEqual([`/api/projects/${projectA}/actions/v1/run`]);
    expect(progressReads).toEqual([]);

    const retryController = new AbortController();
    const retryAbort = new DOMException('retire replay probe', 'AbortError');
    const retry = projectApi.runAction(deriveRequest(), {
      projectId: projectA,
      signal: retryController.signal,
    });
    await vi.waitFor(() => expect(posts).toHaveLength(2));
    retryController.abort(retryAbort);
    retryExtract.resolve(jsonResponse(actionResult(77, 'queued')));

    await expect(retry).rejects.toBe(retryAbort);
    expect(postBodies[1]?.idempotency_key).toBe(postBodies[0]?.idempotency_key);
    expect(progressReads).toEqual([]);
  });

  it('aborts the derive completion delay, clears its timer, and issues no next progress GET', async () => {
    vi.useFakeTimers();
    const projectId = 'web03-4a-derive-delay';
    let progressReads = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === `/api/projects/${projectId}/sheets/7/data?offset=0&limit=0`) {
        return jsonResponse(sheetDataWire());
      }
      if (path === `/api/projects/${projectId}/actions/v1/run`) {
        return jsonResponse(actionResult(77, 'queued'));
      }
      if (path === `/api/projects/${projectId}/actions/runs/77/status`) {
        progressReads += 1;
        return progressReads === 1
          ? jsonResponse(runProgressWire(projectId, 77, 'running'))
          : jsonResponse({ detail: 'timer survived abort' }, 500);
      }
      throw new Error(`Unexpected request: ${path}`);
    }));
    const controller = new AbortController();
    const abortError = new DOMException('retired during completion delay', 'AbortError');

    api = createProjectApi(projectId);
    const launch = api.runAction(deriveRequest(), {
      projectId,
      signal: controller.signal,
    });
    const outcome = launch.then(
      (value) => ({ value, error: null as unknown }),
      (error: unknown) => ({ value: null, error }),
    );
    let outcomeSettled = false;
    void outcome.then(() => {
      outcomeSettled = true;
    });
    await vi.waitFor(() => {
      expect(progressReads).toBe(1);
      expect(vi.getTimerCount()).toBeGreaterThan(0);
    });

    controller.abort(abortError);
    expect(vi.getTimerCount()).toBe(0);
    for (let turn = 0; turn < 10 && !outcomeSettled; turn += 1) {
      await Promise.resolve();
    }
    expect(outcomeSettled).toBe(true);
    await vi.advanceTimersByTimeAsync(1_001);

    expect((await outcome).error).toBe(abortError);
    expect(progressReads).toBe(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('keeps same-numbered run and column metadata isolated by captured project', async () => {
    const projectA = 'web03-4a-meta-a';
    const projectB = 'web03-4a-meta-b';
    const target = 'web03_4a_result';
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/sheets/741/data?offset=0&limit=0')) {
        return jsonResponse(sheetDataWire(target));
      }
      if (path.endsWith('/actions/v1/run')) return jsonResponse(actionResult(741));
      if (path.endsWith('/columns/2/runs?offset=0&limit=20')) {
        return jsonResponse(columnRunsWire(
          path.includes(projectA) ? 'project-a' : 'project-b', target, 741,
        ));
      }
      if (path.endsWith('/actions/runs/741/status')) {
        const projectId = path.includes(projectA) ? projectA : projectB;
        return jsonResponse(runProgressWire(projectId, 741, 'running'));
      }
      throw new Error(`Unexpected request: ${path}`);
    }));

    const projectApiA = createProjectApi(projectA);
    const projectApiB = createProjectApi(projectB);
    await projectApiA.runAction(classifyRequest('project-a', '741', target, [1]));
    await projectApiA.runAction(classifyRequest('project-b', '741', target, [2]), {
      projectId: projectB,
    });
    // Typed launches do not invent column model metadata from arbitrary Params.
    // Authoritative history fills the project-keyed same-numbered column cache.
    await projectApiA.getColumnRuns('2');
    await projectApiB.getColumnRuns('2');

    const runA = await projectApiA.getRunProgress('741');
    const sheetA = await projectApiA.getSheetData('741', 0, 0);
    const runB = await projectApiA.getRunProgress('741', { projectId: projectB });
    const sheetB = await projectApiB.getSheetData('741', 0, 0);

    expect(runA).toMatchObject({ actionName: 'map.classify', targetColumnId: target, targetRowIds: ['1'] });
    expect(runB).toMatchObject({ actionName: 'map.classify', targetColumnId: target, targetRowIds: ['2'] });
    expect(sheetA.columns.find((column) => column.name === target)?.ai?.model).toBe(
      'model-project-a',
    );
    expect(sheetB.columns.find((column) => column.name === target)?.ai?.model).toBe(
      'model-project-b',
    );
  });

  it('keeps canonical local run identity without inventing a model for its column', async () => {
    const projectId = 'model-provenance-local-run';
    const target = 'local_cuts';
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/sheets/7/data?offset=0&limit=0')) {
        return jsonResponse(sheetDataWire(target));
      }
      if (path.endsWith('/actions/v1/run')) {
        return jsonResponse(actionResult(742, 'completed', 'map.find_visual_cuts'));
      }
      if (path.endsWith('/actions/runs/742/status')) {
        const wire = runProgressWire(projectId, 742, 'completed');
        wire.run.action_kind = 'map.find_visual_cuts';
        wire.run.public_status.action_kind = 'map.find_visual_cuts';
        return jsonResponse(wire);
      }
      throw new Error(`Unexpected request: ${path}`);
    }));

    api = createProjectApi(projectId);
    await api.runAction(visualCutsRequest(target));
    expect(await api.getRunProgress('742')).toMatchObject({
      actionName: 'map.find_visual_cuts', targetColumnId: target,
    });
    const sheet = await api.getSheetData('7', 0, 0);

    expect(sheet.columns.find((column) => column.name === target)?.ai).toMatchObject({
      model: '',
    });
  });
});

describe('RealApi job invocation safety (WEB-03-4B AMEND-01)', () => {
  it('forwards one explicit project and exact signal through all five job methods', async () => {
    const projectA = 'web03-4b-job-options-a';
    const projectB = 'web03-4b-job-options-b';
    const controller = new AbortController();
    const requests: Array<{ path: string; signal: AbortSignal | null | undefined }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ path: String(input), signal: init?.signal });
      return jsonResponse({ detail: 'intentional transport pin' }, 500);
    }));

    api = createProjectApi(projectB);
    const options = { projectId: projectA, signal: controller.signal };
    const calls = [
      api.listActionJobs(null, 25, options),
      api.getActionJob!(5, options),
      api.getRunProgress('77', options),
      api.cancelRun('77', options),
      api.getReceipt('receipt-77', options),
    ];
    await Promise.allSettled(calls);

    expect(requests).toHaveLength(5);
    expect(requests.every(({ path }) => path.includes(`/projects/${projectA}/`))).toBe(true);
    expect(requests.every(({ signal }) => signal === controller.signal)).toBe(true);
  });

  it('uses the captured project for run metadata after the ambient project switches', async () => {
    const projectA = 'web03-4b-run-meta-a';
    const projectB = 'web03-4b-run-meta-b';
    const target = 'web03_4b_result';
    const progressResponse = deferred<Response>();
    let statusSignal: AbortSignal | null | undefined;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path.endsWith('/sheets/77/data?offset=0&limit=0')) {
        return jsonResponse(sheetDataWire(target));
      }
      if (path.endsWith('/actions/v1/run')) return jsonResponse(actionResult(77));
      if (path === `/api/projects/${projectA}/actions/runs/77/status`) {
        statusSignal = init?.signal;
        return progressResponse.promise;
      }
      throw new Error(`Unexpected request: ${path}`);
    }));

    api = createProjectApi(projectA);
    await api.runAction(classifyRequest('job-meta-a', '77', target));
    const controller = new AbortController();
    const progress = api.getRunProgress('77', {
      projectId: projectA,
      signal: controller.signal,
    });
    api = createProjectApi(projectB);
    progressResponse.resolve(jsonResponse(runProgressWire(projectA, 77, 'running')));

    await expect(progress).resolves.toMatchObject({
      actionName: 'map.classify',
      targetColumnId: target,
      targetRowIds: ['1'],
    });
    expect(statusSignal).toBe(controller.signal);
  });

  it('aborts between cancel POST and status GET without recapturing the ambient project', async () => {
    const projectA = 'web03-4b-cancel-abort-a';
    const projectB = 'web03-4b-cancel-abort-b';
    const cancelResponse = deferred<Response>();
    const paths: string[] = [];
    const signals: Array<AbortSignal | null | undefined> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      paths.push(String(input));
      signals.push(init?.signal);
      if (String(input) === `/api/projects/${projectA}/actions/runs/77/cancel`) {
        return cancelResponse.promise;
      }
      throw new Error(`Unexpected request: ${String(input)}`);
    }));
    const controller = new AbortController();
    const abortError = new DOMException('retired cancel', 'AbortError');

    api = createProjectApi(projectB);
    const cancelled = api.cancelRun('77', {
      projectId: projectA,
      signal: controller.signal,
    });
    controller.abort(abortError);
    api = createProjectApi(projectB);
    cancelResponse.resolve(jsonResponse({
      completed: 0,
      queue_cancelled: false,
      queue_job_id: null,
      run_id: 77,
      status: 'cancelled',
      total: 2,
    }));

    await expect(cancelled).rejects.toBe(abortError);
    expect(paths).toEqual([`/api/projects/${projectA}/actions/runs/77/cancel`]);
    expect(signals).toEqual([controller.signal]);
  });

  it('keeps a live cancel POST and follow-up GET on one captured project and signal', async () => {
    const projectA = 'web03-4b-cancel-live-a';
    const projectB = 'web03-4b-cancel-live-b';
    const controller = new AbortController();
    const requests: Array<{ path: string; signal: AbortSignal | null | undefined }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      requests.push({ path, signal: init?.signal });
      if (path.endsWith('/cancel')) {
        return jsonResponse({
          completed: 0,
          queue_cancelled: false,
          queue_job_id: null,
          run_id: 77,
          status: 'cancelled',
          total: 2,
        });
      }
      if (path.endsWith('/status')) {
        return jsonResponse(runProgressWire(projectA, 77, 'cancelled'));
      }
      throw new Error(`Unexpected request: ${path}`);
    }));

    api = createProjectApi(projectB);
    await expect(api.cancelRun('77', {
      projectId: projectA,
      signal: controller.signal,
    })).resolves.toMatchObject({ runId: '77', status: 'cancelled' });

    expect(requests.map(({ path }) => path)).toEqual([
      `/api/projects/${projectA}/actions/runs/77/cancel`,
      `/api/projects/${projectA}/actions/runs/77/status`,
    ]);
    expect(requests.every(({ signal }) => signal === controller.signal)).toBe(true);
  });
});
