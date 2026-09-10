import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import {
  createHistoryReviewApi,
  createHistoryReviewDomainApi,
} from '../../src/api/historyReview';
import { createV1ActionSession } from '../../src/api/v1ActionSession';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

class MappedContractError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super(`mapped history-review error ${status}`);
    this.name = 'MappedContractError';
    this.status = status;
    this.payload = payload;
  }
}

const columnRun = {
  run_id: 9,
  action_kind: 'map.classify',
  action_name: 'Classify',
  model: 'model-1',
  status: 'completed',
  spec: { prompt: 'classify this' },
  total_rows: 3,
  completed_rows: 3,
  failed_rows: 0,
  cost_actual: 0.12,
  started_at: '2026-08-11 00:00:00',
  finished_at: null,
  duration_ms: 12,
  tokens_in: 13,
  tokens_out: 14,
  current: true,
  human_score: { passed: 8, graded: 10 },
  judge_scores: [{
    run_id: 12,
    model: 'judge-1',
    started_at: '2026-08-11 01:00:00',
    verdict_column_id: 8,
    passed: 7,
    graded: 9,
    compared: 6,
    disagreement_count: 1,
  }],
};

const columnRunsFixture = {
  column: {
    id: 5,
    name: 'Label',
    type: 'text',
    ai_generated: true,
    current_run_id: 9,
  },
  offset: 2,
  limit: 3,
  total: 4,
  has_more: false,
  next_offset: null,
  current_run: columnRun,
  current_run_loaded: true,
  runs: [columnRun],
};

const historyFixture = {
  schema_version: 'frisket.history_page.v1',
  order: 'asc',
  offset: 0,
  limit: 4,
  total: 1,
  has_more_before: false,
  has_more_after: false,
  prev_offset: null,
  next_offset: null,
  cursor_index: 0,
  cursor_op_loaded: true,
  undo_target: null,
  redo_target: null,
  revision: { total: 1, max_op_id: 12, op_cursor: 12 },
  ops: [
    {
      id: 12,
      index: 0,
      kind: 'map',
      label: 'Classify',
      status: 'completed',
      barrier: false,
      at_cursor: true,
      created_at: '2026-08-11 00:00:00',
      run: {
        run_id: 9,
        status: 'completed',
        action_kind: 'map.classify',
        model: 'model-1',
        params: { labels: ['yes', 'no'] },
        total_rows: 3,
        completed_rows: 2,
        failed_rows: 1,
        cost_estimate: null,
        cost_actual: 0.12,
        started_at: '2026-08-11 00:00:00',
        finished_at: null,
        worker_version: null,
        output_columns: [{ id: 5, name: 'Label' }],
        row_errors: {
          total_failed_rows: 1,
          groups: [{
            message: 'bad row', count: 1, code: null, row_ids: [7],
          }],
        },
      },
    },
  ],
};

const reviewItem = {
  run_id: 9,
  row_id: 7,
  column_id: 5,
  column_name: 'Label',
  column_type: 'text',
  sheet_id: 3,
  value: 'yes',
  confidence: 0.9,
  justification: 'because',
  review_note: 'Check the source date.',
  review_decision: 'edit',
  error: null,
  review_state: 'unreviewed',
  role: 'field',
  chore: false,
};

const reviewBundlesFixture = {
  schema_version: 'frisket.review_bundles_page.v1',
  offset: 5,
  limit: 6,
  total: 1,
  has_more: false,
  next_offset: null,
  bundles: [{
    id: 'bundle-9-7',
    run_id: 9,
    row_id: 7,
    sheet_id: 3,
    sheet_name: 'Articles',
    action_kind: 'map.classify',
    action_name: 'Classify',
    model: 'model-1',
    confidence: 0.9,
    source: { headline: 'Example' },
    fields: [reviewItem],
    evidence: [],
    items: [reviewItem],
  }],
};

/** A history page at a given cursor, with whichever step targets the server
 * would offer there. */
const historyPage = (state: {
  cursor: number;
  undo?: number | null;
  redo?: number | null;
}) => ({
  ...historyFixture,
  cursor_index: state.cursor,
  undo_target: state.undo == null
    ? null
    : { id: state.undo, index: state.cursor - 1, barrier: false },
  redo_target: state.redo == null
    ? null
    : { id: state.redo, index: state.cursor + 1, barrier: false },
});

const actionResult = (
  kind: string,
  status: string,
  errors: Array<{ code?: string; message: string }> = [],
) => ({
  schema_version: 'frisket.action_result.v1',
  action: { kind, action_id: `${kind}-result` },
  status,
  project_id: null,
  run_id: null,
  receipt_id: null,
  errors,
});

type RecordedCall = [string, string | undefined];

/** Answers history reads and v1 action posts from two queues (the last entry
 * repeats, for walks that step the same way many times) and records what the
 * domain actually sent. */
function stubHistoryAndPosts(
  histories: unknown[],
  posts: Array<{ status?: number; result: unknown }>,
): { calls(): RecordedCall[]; bodies(): Record<string, unknown>[] } {
  const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
  let historyIndex = 0;
  let postIndex = 0;
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    requests.push({ input, init });
    const request = String(input);
    if (request.includes('/history?')) {
      const page = histories[Math.min(historyIndex++, histories.length - 1)];
      if (page === undefined) throw new Error(`unexpected history read ${request}`);
      return Promise.resolve(jsonResponse(page));
    }
    const post = posts[Math.min(postIndex++, posts.length - 1)];
    if (post === undefined) throw new Error(`unexpected POST to ${request}`);
    return Promise.resolve(jsonResponse(post.result, post.status ?? 200));
  }));
  return {
    calls: () => requests.map(({ input, init }): RecordedCall => [String(input), init?.method]),
    bodies: () => requests
      .filter(({ init }) => init?.method === 'POST')
      .map(({ init }) => JSON.parse(String(init?.body)) as Record<string, unknown>),
  };
}

/** The domain under test wired the way RealApi wires it: one V1 action session,
 * narrowed to the three methods the mutations use. */
const historyReviewDomain = (projectId: string) => createHistoryReviewDomainApi(
  (status, payload) => new MappedContractError(status, payload),
  {
    v1ActionSession: createV1ActionSession(projectId),
  },
  projectId,
);

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('history and review generated HTTP reads', () => {
  it('preserves paths, query omission, encoding, headers, abort, and raw responses', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses = [columnRunsFixture, historyFixture, reviewBundlesFixture, { count: 4 }];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(responses.shift());
    }));
    const signal = new AbortController().signal;
    const api = createHistoryReviewApi(
      (status, payload) => new MappedContractError(status, payload),
      '101',
    );
    const options = { signal, headers: { Authorization: 'Bearer history-review' } };

    await expect(api.getColumnRuns('column/a', 2, 3, options)).resolves.toEqual(columnRunsFixture);
    await expect(api.getHistory(null, 4, options)).resolves.toEqual(historyFixture);
    await expect(api.getReviewBundles(5, 6, undefined, false, options)).resolves.toEqual(reviewBundlesFixture);
    await expect(api.getReviewCount(undefined, options)).resolves.toEqual({ count: 4 });

    expect(requests.map((request) => [request.input, request.init?.method])).toEqual([
      ['/api/projects/101/columns/column%2Fa/runs?offset=2&limit=3', 'GET'],
      ['/api/projects/101/history?limit=4', 'GET'],
      ['/api/projects/101/review/bundles?offset=5&limit=6', 'GET'],
      ['/api/projects/101/review/count', 'GET'],
    ]);
    for (const request of requests) {
      expect(request.init?.signal).toBe(signal);
      expect(new Headers(request.init?.headers).get('authorization')).toBe('Bearer history-review');
    }
  });

  it('scopes a reviewed queue and its pending count without changing the default inbox bytes', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(requests.length === 1 ? reviewBundlesFixture : { count: 2 });
    }));
    const api = createHistoryReviewApi(
      (status, payload) => new MappedContractError(status, payload),
      'run-filter',
    );

    await api.getReviewBundles(0, 25, '9', true);
    await api.getReviewCount('9');

    expect(requests.map((request) => String(request.input))).toEqual([
      '/api/projects/run-filter/review/bundles?offset=0&limit=25&run_id=9&include_reviewed=true',
      '/api/projects/run-filter/review/count?run_id=9',
    ]);
  });

  it.each([401, 403, 404, 422, 500])('maps HTTP %i through the provided error factory', async (status) => {
    const payload = { detail: `history-review-${status}` };
    const errorFactory = vi.fn(
      (actualStatus: number, actualPayload: unknown) =>
        new MappedContractError(actualStatus, actualPayload),
    );
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(payload, status)));
    const api = createHistoryReviewApi(errorFactory, '102');

    await expect(api.getHistory()).rejects.toMatchObject({
      name: 'MappedContractError', status, payload,
    });
    expect(errorFactory).toHaveBeenCalledWith(status, payload);
  });

  it('forwards native abort identity through the raw transport', async () => {
    const controller = new AbortController();
    let forwardedSignal: AbortSignal | null | undefined;
    vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      forwardedSignal = init?.signal;
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), {
          once: true,
        });
      });
    }));
    const api = createHistoryReviewApi(
      (status, payload) => new MappedContractError(status, payload),
      '103',
    );
    const pending = api.getHistory(undefined, 50, { signal: controller.signal });
    const abortError = new DOMException('history cancelled', 'AbortError');
    controller.abort(abortError);

    await expect(pending).rejects.toBe(abortError);
    expect(forwardedSignal).toBe(controller.signal);
  });

  it('maps all four reads through the direct domain factory with stable defaults', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses = [columnRunsFixture, historyFixture, reviewBundlesFixture, { count: 4 }];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(responses.shift());
    }));
    const api = historyReviewDomain('104');

    await expect(api.getColumnRuns('5')).resolves.toMatchObject({
      columnName: 'Label', offset: 2, limit: 3, totalRuns: 4,
      currentRunLoaded: true,
      runs: [{
        runId: '9', actionName: 'Classify', startedAt: '2026-08-11T00:00:00Z',
        finishedAt: null, cost: 0.12, spec: { prompt: 'classify this' },
        humanScore: { passed: 8, graded: 10 },
        judgeScores: [{
          runId: '12', model: 'judge-1', startedAt: '2026-08-11 01:00:00',
          verdictColumnId: '8', passed: 7, graded: 9, compared: 6, disagreementCount: 1,
        }],
      }],
    });
    await expect(api.getHistory()).resolves.toMatchObject({
      cursorIndex: 0,
      revision: { total: 1, maxOpId: '12', opCursor: '12' },
      ops: [{
        id: '12', kind: 'map', rowsAffected: 3, cost: 0.12,
        run: {
          outputColumns: [{ id: '5', name: 'Label' }],
          rowErrors: {
            totalFailedRows: 1,
            groups: [{ rowIds: ['7'], outcome: null, terminal: false }],
          },
        },
      }],
    });
    await expect(api.getReviewBundles()).resolves.toMatchObject({
      schemaVersion: 'frisket.review_bundles_page.v1',
      bundles: [{
        id: 'bundle-9-7', source: { headline: 'Example' },
        fields: [{
          id: '9:7:5',
          columnType: 'text',
          note: 'Check the source date.',
          reviewDecision: 'edit',
          reviewState: 'unreviewed',
        }],
      }],
    });
    await expect(api.getReviewCount()).resolves.toBe(4);

    expect(requests.map((request) => [request.input, request.init?.method])).toEqual([
      ['/api/projects/104/columns/5/runs?offset=0&limit=20', 'GET'],
      ['/api/projects/104/history?limit=50', 'GET'],
      ['/api/projects/104/review/bundles?offset=0&limit=25', 'GET'],
      ['/api/projects/104/review/count', 'GET'],
    ]);
  });

  it('keeps managed history current-free while retaining an explicit latest action family', async () => {
    const latest = { ...columnRun, current: false };
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      ...columnRunsFixture,
      column: {
        ...columnRunsFixture.column,
        current_run_id: null,
        latest_run_id: 9,
        mixed_origins: true,
      },
      current_run: null,
      current_run_loaded: false,
      latest_run: latest,
      latest_run_loaded: true,
      runs: [latest],
    })));
    const info = await historyReviewDomain('mixed-history').getColumnRuns('5');

    expect(info).toMatchObject({
      currentRun: null,
      currentRunLoaded: false,
      latestRun: { runId: '9', current: false },
      latestRunLoaded: true,
      mixedOrigins: true,
      runs: [{ runId: '9', current: false }],
    });
  });

  it('preserves history and review fallback, omission, value, and truncation semantics', async () => {
    const temporal = {
      schema_version: 'frisket.timeline_point.v1',
      at_ms: 12,
      label: 'event',
    };
    const longTail = 'x'.repeat(420);
    const source = {
      headline: 'Example',
      metadata: { nested: true },
      timeline: temporal,
      tail: longTail,
    };
    const expectedSource = {
      headline: 'Example',
      metadata: '{"nested":true}',
      timeline: temporal,
      tail: longTail,
    };
    const fallbackColumnRun = {
      ...columnRun,
      run_id: 10,
      action_kind: 'map.future_action',
      action_name: '',
      model: null,
      spec: null,
      started_at: null,
      finished_at: null,
      duration_ms: null,
      tokens_in: null,
      tokens_out: null,
      current: false,
    };
    const fallbackColumnRuns = {
      ...columnRunsFixture,
      current_run: null,
      current_run_loaded: false,
      runs: [fallbackColumnRun],
    };
    const fallbackHistory = {
      ...historyFixture,
      total: 2,
      cursor_index: -1,
      cursor_op_loaded: false,
      revision: { total: 0, max_op_id: 0, op_cursor: -1 },
      ops: [
        {
          id: 20,
          index: 0,
          kind: 'future-kind',
          label: null,
          status: 'completed',
          barrier: false,
          at_cursor: false,
          created_at: '2026-08-11 01:02:03',
          run: null,
        },
        {
          id: 21,
          index: 1,
          kind: 'sort',
          label: 'Sort rows',
          status: 'completed',
          barrier: true,
          at_cursor: false,
          created_at: '2026-08-11T01:02:04Z',
          run: null,
        },
      ],
    };
    const fallbackField = {
      ...reviewItem,
      value: ['alpha', 'beta'],
      confidence: null,
      justification: null,
      review_note: null,
      review_decision: null,
      review_state: null,
      column_type: null,
      error: 'wire-only error',
    };
    const temporalEvidence = {
      ...reviewItem,
      column_id: 6,
      column_name: 'When',
      column_type: 'timeline_point',
      value: temporal,
      role: 'evidence',
    };
    const fallbackReviewBundles = {
      ...reviewBundlesFixture,
      bundles: [{
        ...reviewBundlesFixture.bundles[0],
        sheet_name: null,
        action_kind: 'map.future_action',
        action_name: '',
        model: null,
        confidence: null,
        source,
        fields: [fallbackField],
        evidence: [temporalEvidence],
        items: [fallbackField, temporalEvidence],
      }],
    };
    const responses = [
      fallbackColumnRuns,
      fallbackHistory,
      fallbackReviewBundles,
      { count: null },
    ];
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(responses.shift())));
    const api = historyReviewDomain('106');

    await expect(api.getColumnRuns('5')).resolves.toMatchObject({
      currentRun: null,
      currentRunLoaded: false,
      runs: [{
        runId: '10', actionName: 'Map Future Action', model: '', spec: {},
        startedAt: null, finishedAt: null, durationMs: null,
        tokensIn: null, tokensOut: null,
      }],
    });
    const history = await api.getHistory(null);
    expect(history).toMatchObject({
      cursorIndex: -1,
      revision: { total: 0, maxOpId: null, opCursor: null },
      ops: [
        {
          id: '20', label: 'future-kind', kind: 'edit',
          at: '2026-08-11T01:02:03Z', rowsAffected: 0,
          cost: undefined, run: undefined,
        },
        {
          id: '21', label: 'Sort rows', kind: 'sort',
          at: '2026-08-11T01:02:04Z', barrier: true,
        },
      ],
    });
    expect(history.ops[0]).not.toHaveProperty('barrier');

    const page = await api.getReviewBundles();
    const bundle = page.bundles[0];
    expect(bundle).toMatchObject({
      sheetName: 'sheet 3', rowIndex: 6,
      actionName: 'Map Future Action', model: '', confidence: 0,
      source: expectedSource,
      fields: [{
        id: '9:7:5', columnType: 'text', value: '["alpha","beta"]',
        confidence: 0, justification: '', note: null, reviewDecision: null, reviewState: 'unreviewed',
      }],
      evidence: [{ id: '9:7:6', value: temporal }],
    });
    expect(bundle.context).toBe(
      Object.entries(expectedSource)
        .map(([key, value]) => `${key}: ${String(value)}`)
        .join('  ·  ')
        .slice(0, 400),
    );
    expect(bundle.context).toHaveLength(400);
    expect(bundle).not.toHaveProperty('items');
    expect(bundle.fields[0]).not.toHaveProperty('error');
    await expect(api.getReviewCount()).resolves.toBe(0);
  });

  it('keeps realApi history, column-run, bundle, and count domain mappings stable', async () => {
    const real = await import('../../src/api/real');
    const projectApi = real.createProjectApi('105');
    const responses = [columnRunsFixture, historyFixture, reviewBundlesFixture, { count: 4 }];
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(responses.shift())));
    await expect(projectApi.getColumnRuns('5', 2, 3)).resolves.toMatchObject({
      columnName: 'Label', totalRuns: 4, currentRunLoaded: true,
      runs: [{ runId: '9', startedAt: '2026-08-11T00:00:00Z', cost: 0.12 }],
    });
    await expect(projectApi.getHistory(null, 4)).resolves.toMatchObject({
      cursorIndex: 0,
      revision: { total: 1, maxOpId: '12', opCursor: '12' },
      ops: [{
        id: '12', kind: 'map', rowsAffected: 3, cost: 0.12,
        run: { outputColumns: [{ id: '5', name: 'Label' }], rowErrors: {
          totalFailedRows: 1, groups: [{ rowIds: ['7'] }],
        } },
      }],
    });
    await expect(projectApi.getReviewBundles(5, 6)).resolves.toMatchObject({
      schemaVersion: 'frisket.review_bundles_page.v1',
      bundles: [{
        id: 'bundle-9-7', source: { headline: 'Example' },
        fields: [{ id: '9:7:5', columnType: 'text', reviewState: 'unreviewed' }],
      }],
    });
    await expect(projectApi.getReviewCount()).resolves.toBe(4);
  });

  it.each([
    {
      name: 'undo preserves its captured project when operation_mismatch is returned',
      invoke: (projectApi: ReturnType<typeof import('../../src/api/real').createProjectApi>) => projectApi.undo(),
      initial: { cursor: 1, undo: 12, redo: null },
      following: [{ cursor: 1, undo: 12, redo: null }],
      actions: [{ kind: 'operation.undo', expectedOpId: 12, mismatch: true }],
      expectedCursor: 1,
    },
    {
      name: 'redo preserves its captured project',
      invoke: (projectApi: ReturnType<typeof import('../../src/api/real').createProjectApi>) => projectApi.redo(),
      initial: { cursor: 0, undo: null, redo: 12 },
      following: [{ cursor: 1, undo: 12, redo: null }],
      actions: [{ kind: 'operation.redo', expectedOpId: 12, mismatch: false }],
      expectedCursor: 1,
    },
    {
      name: 'stepTo preserves its captured project through recursive history reads',
      invoke: (projectApi: ReturnType<typeof import('../../src/api/real').createProjectApi>) => projectApi.stepTo(2),
      initial: { cursor: 0, undo: null, redo: 12 },
      following: [
        { cursor: 1, undo: 12, redo: 13 },
        { cursor: 2, undo: 13, redo: null },
      ],
      actions: [
        { kind: 'operation.redo', expectedOpId: 12, mismatch: false },
        { kind: 'operation.redo', expectedOpId: 13, mismatch: false },
      ],
      expectedCursor: 2,
    },
  ])('$name across an ambient project switch', async (scenario) => {
    const real = await import('../../src/api/real');
    const projectA = 'history-capture-project-a';
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const histories = [
      historyPage(scenario.initial),
      ...scenario.following.map((state) => historyPage(state)),
    ];
    let historyIndex = 0;
    let postIndex = 0;
    let releaseFirstHistory: (() => void) | undefined;
    let firstHistoryRequested: (() => void) | undefined;
    const firstHistoryArrived = new Promise<void>((resolve) => {
      firstHistoryRequested = resolve;
    });
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      const request = String(input);
      if (request.includes('/history?')) {
        const payload = histories[historyIndex++];
        if (historyIndex === 1) {
          firstHistoryRequested?.();
          return new Promise<Response>((resolve) => {
            releaseFirstHistory = () => resolve(jsonResponse(payload));
          });
        }
        return Promise.resolve(jsonResponse(payload));
      }
      const action = scenario.actions[postIndex++];
      const result = {
        schema_version: 'frisket.action_result.v1',
        action: { kind: action.kind, action_id: `history-${postIndex}` },
        status: action.mismatch ? 'failed' : 'completed',
        project_id: projectA,
        run_id: null,
        receipt_id: null,
        errors: action.mismatch ? [{ code: 'operation_mismatch', message: 'history moved' }] : [],
      };
      return Promise.resolve(jsonResponse(result, action.mismatch ? 409 : 200));
    }));
    const pending = scenario.invoke(real.createProjectApi(projectA));
    await firstHistoryArrived;
    releaseFirstHistory?.();

    await expect(pending).resolves.toMatchObject({ cursorIndex: scenario.expectedCursor });
    expect(postIndex).toBe(scenario.actions.length);
    expect(requests.map(({ input, init }) => [input, init?.method])).toEqual([
      [`/api/projects/${projectA}/history?limit=50`, 'GET'],
      ...scenario.actions.flatMap((_, index) => [
        [`/api/projects/${projectA}/actions/v1/run`, 'POST'],
        ...((index < scenario.following.length)
          ? [[`/api/projects/${projectA}/history?limit=${scenario.actions.length > 1 ? 4 : 50}`, 'GET']]
          : []),
      ]),
    ]);
    const posts = requests.filter(({ init }) => init?.method === 'POST');
    expect(posts.map(({ init }) => JSON.parse(String(init?.body)))).toEqual(
      scenario.actions.map((action) => expect.objectContaining({
        action_id: action.kind,
        scope: { kind: 'project' },
        params: { expected_op_id: action.expectedOpId },
        output_names: {},
        idempotency_key: expect.stringMatching(/^web-operation\.(undo|redo):/),
      })),
    );
  });
});

describe('history and review mutations owned by the domain', () => {
  it.each([
    {
      mutation: 'undo' as const,
      kind: 'operation.undo',
      key: /^web-operation\.undo:/,
      before: { cursor: 1, undo: 12 },
      after: { cursor: 0, redo: 12 },
    },
    {
      mutation: 'redo' as const,
      kind: 'operation.redo',
      key: /^web-operation\.redo:/,
      before: { cursor: 0, redo: 12 },
      after: { cursor: 1, undo: 12 },
    },
  ])('$mutation posts one step envelope and returns the re-read history', async (scenario) => {
    const exchange = stubHistoryAndPosts(
      [historyPage(scenario.before), historyPage(scenario.after)],
      [{ result: actionResult(scenario.kind, 'completed') }],
    );
    const api = historyReviewDomain('110');

    await expect(api[scenario.mutation]()).resolves.toMatchObject({
      cursorIndex: scenario.after.cursor,
    });
    expect(exchange.calls()).toEqual([
      ['/api/projects/110/history?limit=50', 'GET'],
      ['/api/projects/110/actions/v1/run', 'POST'],
      ['/api/projects/110/history?limit=50', 'GET'],
    ]);
    expect(exchange.bodies()).toEqual([{
      action_id: scenario.kind,
      scope: { kind: 'project' },
      params: { expected_op_id: 12 },
      output_names: {},
      idempotency_key: expect.stringMatching(scenario.key),
    }]);
  });

  it('omits expected_op_id when the cursor offers no undo target', async () => {
    const exchange = stubHistoryAndPosts(
      [historyPage({ cursor: 0 }), historyPage({ cursor: 0 })],
      [{ result: actionResult('operation.undo', 'completed') }],
    );
    const api = historyReviewDomain('111');

    await expect(api.undo()).resolves.toMatchObject({ cursorIndex: 0, undoTarget: null });
    expect(exchange.bodies()).toEqual([
      expect.objectContaining({ action_id: 'operation.undo', params: {} }),
    ]);
  });

  it('rejects a non-positive operation ref before posting anything', async () => {
    const exchange = stubHistoryAndPosts([historyPage({ cursor: 1, undo: 0 })], []);
    const api = historyReviewDomain('112');

    await expect(api.undo()).rejects.toMatchObject({
      name: 'ApiError',
      status: 400,
      message: 'Operation 0 is not a valid v1 operation ref',
    });
    expect(exchange.calls()).toEqual([['/api/projects/112/history?limit=50', 'GET']]);
  });

  it.each([
    { code: 'operation_mismatch', status: 409, message: 'history moved underneath the step' },
    { code: 'operation_unavailable', status: 200, message: 'that operation is gone' },
    { code: 'irreversible_barrier', status: 200, message: 'the import cannot be undone' },
  ])('leaves the cursor where it is when a step reports $code', async (blocked) => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const exchange = stubHistoryAndPosts(
      [historyPage({ cursor: 0, redo: 12 })],
      [{
        status: blocked.status,
        result: actionResult('operation.redo', 'failed', [
          { code: blocked.code, message: blocked.message },
        ]),
      }],
    );
    const api = historyReviewDomain('113');

    await expect(api.stepTo(3)).resolves.toMatchObject({ cursorIndex: 0 });
    // A blocked step ends the walk: no further post, no re-read.
    expect(exchange.calls()).toEqual([
      ['/api/projects/113/history?limit=50', 'GET'],
      ['/api/projects/113/actions/v1/run', 'POST'],
    ]);
    expect(warn).toHaveBeenCalledWith('operation.redo blocked:', blocked.message);
  });

  it('fails the walk when a step reports an unrecognized error', async () => {
    stubHistoryAndPosts(
      [historyPage({ cursor: 0, redo: 12 })],
      [{
        result: actionResult('operation.redo', 'failed', [
          { code: 'operation_locked', message: 'locked by another writer' },
        ]),
      }],
    );
    const api = historyReviewDomain('114');

    await expect(api.stepTo(3)).rejects.toMatchObject({
      name: 'ApiError',
      status: 400,
      message: 'locked by another writer',
    });
  });

  it('stops without posting when the cursor has no target toward the requested index', async () => {
    const exchange = stubHistoryAndPosts([historyPage({ cursor: 0 })], []);
    const api = historyReviewDomain('115');

    await expect(api.stepTo(4)).resolves.toMatchObject({ cursorIndex: 0 });
    expect(exchange.calls()).toEqual([['/api/projects/115/history?limit=50', 'GET']]);
  });

  it('bounds a walk whose cursor never advances at 100 steps', async () => {
    const exchange = stubHistoryAndPosts(
      [historyPage({ cursor: 0, redo: 12 })],
      [{ result: actionResult('operation.redo', 'completed') }],
    );
    const api = historyReviewDomain('116');

    await expect(api.stepTo(9)).resolves.toMatchObject({ cursorIndex: 0 });
    const calls = exchange.calls();
    expect(calls.filter(([, method]) => method === 'POST')).toHaveLength(100);
    // The initial read plus one re-read per step, each re-read keeping the
    // page size the walk started with.
    expect(calls.filter(([, method]) => method === 'GET')).toHaveLength(101);
    expect(calls[calls.length - 1]).toEqual(['/api/projects/116/history?limit=4', 'GET']);
  });

  it('keeps one captured project across recursive reads and operation posts', async () => {
    const exchange = stubHistoryAndPosts(
      [
        historyPage({ cursor: 0, redo: 12 }),
        historyPage({ cursor: 1, undo: 12, redo: 13 }),
        historyPage({ cursor: 2, undo: 13 }),
      ],
      [
        { result: actionResult('operation.redo', 'completed') },
        { result: actionResult('operation.redo', 'completed') },
      ],
    );
    const api = historyReviewDomain('117-a');

    const pending = api.stepTo(2);
    await expect(pending).resolves.toMatchObject({ cursorIndex: 2 });
    expect(exchange.calls()).toEqual([
      ['/api/projects/117-a/history?limit=50', 'GET'],
      ['/api/projects/117-a/actions/v1/run', 'POST'],
      ['/api/projects/117-a/history?limit=4', 'GET'],
      ['/api/projects/117-a/actions/v1/run', 'POST'],
      ['/api/projects/117-a/history?limit=4', 'GET'],
    ]);
    expect(exchange.bodies().map((body) => body.params)).toEqual([
      { expected_op_id: 12 },
      { expected_op_id: 13 },
    ]);
  });

  it.each([
    { name: 'accept', action: 'accept' as const, edited: undefined, decision: { decision: 'accept' } },
    { name: 'reject', action: 'reject' as const, edited: undefined, decision: { decision: 'reject' } },
    {
      name: 'reject and clear',
      action: 'reject_clear' as const,
      edited: undefined,
      decision: { decision: 'reject_clear' },
    },
    {
      name: 'edit carrying a value',
      action: 'edit' as const,
      edited: 'corrected',
      decision: { decision: 'edit', value: 'corrected' },
    },
    {
      name: 'edit clearing the value',
      action: 'edit' as const,
      edited: undefined,
      decision: { decision: 'edit', value: null },
    },
  ])('posts a review.decision envelope for $name', async (scenario) => {
    const exchange = stubHistoryAndPosts(
      [],
      [{ result: actionResult('review.decision', 'completed') }],
    );
    const api = historyReviewDomain('118');

    await expect(api.reviewItem('9:7:5', scenario.action, scenario.edited, 'source disagrees'))
      .resolves.toBeUndefined();
    expect(exchange.calls()).toEqual([['/api/projects/118/actions/v1/run', 'POST']]);
    expect(exchange.bodies()).toEqual([{
      action_id: 'review.decision',
      scope: { kind: 'project' },
      params: {
        run_id: 9,
        row_id: 7,
        column_id: 5,
        ...scenario.decision,
        note: 'source disagrees',
      },
      output_names: {},
      idempotency_key: expect.stringMatching(/^web-review\.decision:/),
    }]);
  });

  it.each(['9:7', '9:7:5:1', '0:7:5', '9:0:5', '9:7:-5', 'a:b:c', '9:7.5:5', ''])(
    'rejects the malformed review ref %j without posting',
    async (itemId) => {
      const exchange = stubHistoryAndPosts([], []);
      const api = historyReviewDomain('119');

      await expect(api.reviewItem(itemId, 'accept')).rejects.toMatchObject({
        name: 'ApiError',
        status: 400,
        message: `Review item ${itemId} is not a valid v1 result-cell ref`,
      });
      expect(exchange.calls()).toEqual([]);
    },
  );

  it.each([
    {
      name: 'failed',
      status: 'failed',
      errors: [{ code: 'review_conflict', message: 'row already reviewed' }],
      message: 'row already reviewed',
    },
    { name: 'still running', status: 'running', errors: [], message: 'Review decision failed' },
  ])('rejects a $name review result', async (scenario) => {
    stubHistoryAndPosts(
      [],
      [{ result: actionResult('review.decision', scenario.status, scenario.errors) }],
    );
    const api = historyReviewDomain('120');

    await expect(api.reviewItem('9:7:5', 'accept')).rejects.toMatchObject({
      name: 'ApiError',
      status: 400,
      message: scenario.message,
    });
  });
});
