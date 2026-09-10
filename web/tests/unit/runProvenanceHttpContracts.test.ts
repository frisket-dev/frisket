import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createProjectApi } from '../../src/api/real';

let api = createProjectApi('test-project');



const RUN_ROW = {
  schema_version: 'frisket.actions.v1',
  action: 'run_trace_row',
  project_id: 'folder/child',
  run_id: 7,
  row_id: 9,
  column_id: null,
  recorded: true,
  status: 'recorded',
  state: 'recorded',
  run: {
    id: 7,
    sheet_id: 3,
    action_kind: 'map.classify',
    action_name: 'Classify rows',
    status: 'completed',
    model: 'gemini/test',
  },
  trace: {
    run_id: 7,
    trace_id: 'trace-7',
    action_kind: 'map.classify',
    action_name: 'Classify rows',
    model: 'gemini/test',
    created_at: '2026-06-19 12:00:00',
    row: {
      row_id: 9,
      prompt: [{ role: 'user', content: 'classify this' }],
      raw_response: '{"topic":"news"}',
      data: { topic: 'news' },
      retries: [{ event: 'attempt', outcome: 'error' }],
      calls: [{ tool: 'classify' }],
      cached: false,
    },
    record_count: 2,
  },
  cell: { value: 'news' },
  absence: null,
};

const MANIFEST = {
  project_id: 'folder/child',
  models: [{ model: 'gemini/test', provider: 'gemini', runs: 1, rows: 2, cost: 1.25 }],
  touched: ['gemini/test'],
  providers: ['gemini'],
  action_kinds: [{
    runs: 1, rows: 2, failed_rows: 0, cost: 1.25,
  }],
  runs: [{
    run_id: 7, sheet_id: 3, action_kind: 'map.classify',
    action_name: 'Classify rows', model: 'gemini/test', provider: 'gemini', status: 'completed',
    total_rows: 2, completed_rows: 2, failed_rows: 0, cost: null,
    started_at: '2026-06-19 12:00:00', finished_at: null,
  }],
  runs_page: {
    schema_version: 'frisket.provenance_runs_page.v1', order: 'desc', offset: 4,
    limit: 7, total: 12, has_more: true, next_offset: 11,
  },
  receipts: [{
    receipt_id: 'receipt-7', action_kind: 'map.classify', status: 'completed',
    run_id: 7, created_at: '2026-06-19 12:00:01',
  }],
  receipts_page: {
    schema_version: 'frisket.provenance_receipts_page.v1', order: 'desc', offset: 0,
    limit: 25, total: 0, has_more: false, next_offset: null,
  },
  total_cost: 1.25,
  has_unknown_costs: true,
  unknown_cost_runs: 1,
};

function stubFetch(...bodies: unknown[]) {
  const fetchMock = vi.fn(() => Promise.resolve({
    status: 200,
    statusText: '',
    json: () => Promise.resolve(bodies.shift()),
  } as Response));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

beforeEach(() => api = createProjectApi('folder/child'));
afterEach(() => vi.unstubAllGlobals());

describe('generated run/provenance transport', () => {
  it('renders a raw project id once, omits null column_id, and maps lossless row evidence', async () => {
    const fetchMock = stubFetch(RUN_ROW);
    const evidence = await api.getRunTraceRow('7', '9');

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/projects/folder%2Fchild/actions/runs/7/trace/rows/9',
      expect.objectContaining({ method: 'GET' }),
    );
    expect(evidence).toMatchObject({
      runId: '7', rowId: '9', columnId: null, status: 'recorded',
      trace: { recordCount: 2, row: { rawResponse: '{"topic":"news"}', retries: 1 } },
    });
  });

  it('keeps paging order/defaults and null costs honest while mapping provenance', async () => {
    const fetchMock = stubFetch(MANIFEST);
    const manifest = await api.getProvenanceManifest(4, 7, 0, 25);

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/projects/folder%2Fchild/provenance?runs_offset=4&runs_limit=7&receipts_offset=0&receipts_limit=25',
      expect.objectContaining({ method: 'GET' }),
    );
    expect(manifest).toMatchObject({
      projectId: 'folder/child', hasUnknownCosts: true, unknownCostRuns: 1,
      runs: [{ runId: '7', cost: null, startedAt: '2026-06-19T12:00:00Z' }],
      runsPage: { offset: 4, limit: 7, nextOffset: 11 },
    });
    expect(manifest.receipts).toEqual([{
      receiptId: 'receipt-7',
      actionKind: 'map.classify',
      status: 'completed',
      runId: '7',
      createdAt: '2026-06-19T12:00:01Z',
    }]);
    expect(Object.keys(manifest.receipts[0]).sort()).toEqual([
      'actionKind', 'createdAt', 'receiptId', 'runId', 'status',
    ]);
  });
});
