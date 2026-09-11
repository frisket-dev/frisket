import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  getColumnStatsContract,
  getSheetDataContract,
  listSheetsContract,
  locateSheetRowContract,
  updateSheetContract,
} from '../../src/api/httpContractRoutes';
import { ApiError, createProjectApi } from '../../src/api/real';
import { createSheetGridDomainApi } from '../../src/api/sheetGrid';
import { createV1ActionSession } from '../../src/api/v1ActionSession';

let realApi = createProjectApi('test-project');

const sheetColumnWire = {
  id: 9,
  name: 'Title',
  type: 'text',
  ai_generated: false,
  format: null,
  semantic_type: null,
  current_run_id: null,
  latest_run_id: null,
  generation_managed: false,
  mixed_origins: false,
  transcript_status: null,
  media_download_candidate: null,
  replay_pending_count: 0,
  default_hidden: false,
};

const sheetListWire = [{
  id: 7,
  name: 'Sheet One',
  parent_sheet_id: null,
  parent_op_id: null,
  rows: 0,
  title_column_id: null,
  cited_column_ids: [],
  annotated_text_column_ids: [],
  dependent_sheet_ids: [],
  columns: [sheetColumnWire],
}];

const sheetDataWire = {
  columns: [],
  rows: [],
  total: 0,
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function contractError(status: number, payload: unknown): Error {
  return new Error(
    'unexpected contract error ' + status + ': ' + JSON.stringify(payload),
  );
}

class MappedContractError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super('mapped contract error ' + status);
    this.name = 'MappedContractError';
    this.status = status;
    this.payload = payload;
  }
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('sheet and grid HTTP contracts', () => {
  it('preserves the canonical URL-download launcher hint from the sheet contract', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      total: 0, rows: [], columns: [{
        id: 9, name: 'URL', type: 'link', format: null, semantic_type: null,
        default_hidden: false, current_run_id: null, latest_run_id: null,
        generation_managed: false, mixed_origins: false, transcript_status: null,
        media_download_candidate: 'media.fetch_url', replay_pending_count: 0,
        ai_generated: false,
      }],
    })));
    const page = await realApi.getSheetData('7', 0, 25);
    expect(page.columns[0].mediaDownloadCandidate).toBe('media.fetch_url');
  });

  it('maps stats without inventing gated fields and sends force only when true', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const gatedWire = {
      schema_version: 'frisket.column_stats.v1',
      sheet_id: 7,
      column: { id: 9, name: 'score', type: 'number', format: null },
      row_count: 100001,
      threshold: 100000,
      computed: false,
      requires_manual_analyze: true,
    };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(gatedWire);
    }));

    const gated = await getColumnStatsContract(
      'tenant/project % snowman ☃',
      7,
      9,
      false,
      contractError,
      { headers: { 'X-Trace-Id': 'stats-1' } },
    );
    expect(gated).toEqual({
      schemaVersion: 'frisket.column_stats.v1',
      sheetId: '7',
      column: { id: '9', name: 'score', type: 'number', format: null },
      rowCount: 100001,
      threshold: 100000,
      computed: false,
      requiresManualAnalyze: true,
      topValues: [],
      numeric: null,
      text: null,
      date: null,
      file: null,
      jsonTypes: [],
    });
    expect(requests[0]?.input).toBe(
      '/api/projects/tenant%2Fproject%20%25%20snowman%20%E2%98%83/sheets/7/columns/9/stats',
    );
    expect(requests[0]?.init?.method).toBe('GET');
    expect(requests[0]?.init?.body).toBeUndefined();
    expect(new Headers(requests[0]?.init?.headers).has('content-type')).toBe(false);
    expect(new Headers(requests[0]?.init?.headers).get('x-trace-id')).toBe('stats-1');

    await getColumnStatsContract('project-1', 7, 9, true, contractError);
    expect(requests[1]?.input).toBe(
      '/api/projects/project-1/sheets/7/columns/9/stats?force=true',
    );
  });

  it('preserves stats numeric bytes, explicit nulls, and optional field presence', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      schema_version: 'frisket.column_stats.v1',
      sheet_id: 7,
      column: { id: 9, name: 'score', type: 'number', format: 'currency' },
      row_count: 2,
      threshold: 100000,
      computed: true,
      requires_manual_analyze: false,
      missing: 0,
      present: 2,
      distinct: 2,
      top_values: [],
      numeric: {
        count: 2,
        mean: 2.75,
        median: 2.75,
        min: 1,
        max: 4.5,
        histogram: [{ min: 0, max: 2.5, count: 1 }],
      },
      text: {
        count: 1,
        shortest: 'five',
        shortest_length: 4,
        longest: 'five',
        longest_length: 4,
        mean_length: 4,
        median_length: 4,
        length_histogram: [{ min: 4, max: 4, count: 1 }],
      },
      date: null,
      file: null,
      json_types: [],
    })));

    await expect(getColumnStatsContract(
      'project-1',
      7,
      9,
      false,
      contractError,
    )).resolves.toMatchObject({
      column: { id: '9', name: 'score', type: 'number', format: 'currency' },
      missing: 0,
      present: 2,
      distinct: 2,
      topValues: [],
      numeric: {
        min: 1,
        max: 4.5,
        histogram: [{ min: 0, max: 2.5, count: 1 }],
      },
      text: {
        count: 1,
        shortest: 'five',
        shortestLength: 4,
        longest: 'five',
        longestLength: 4,
        meanLength: 4,
        medianLength: 4,
        lengthHistogram: [{ min: 4, max: 4, count: 1 }],
      },
      date: null,
      file: null,
      jsonTypes: [],
    });
  });

  it('locates with default page size, false/null truth, and bodyless transport options', async () => {
    const controller = new AbortController();
    let forwardedInit: RequestInit | undefined;
    let forwardedInput: RequestInfo | URL | undefined;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      forwardedInput = input;
      forwardedInit = init;
      return jsonResponse({
        schema_version: 'frisket.sheet_row_location.v1',
        sheet_id: 7,
        row_id: 11,
        found: false,
        index: null,
        page_offset: null,
        page_size: 500,
      });
    }));

    await expect(locateSheetRowContract(
      'tenant/project 1',
      7,
      11,
      { page_size: 500 },
      contractError,
      { signal: controller.signal, headers: { 'X-Trace-Id': 'locate-1' } },
    )).resolves.toEqual({
      found: false,
      rowId: '11',
      index: null,
      pageOffset: null,
      pageSize: 500,
    });
    expect(forwardedInput).toBe(
      '/api/projects/tenant%2Fproject%201/sheets/7/rows/11/locate?page_size=500',
    );
    expect(forwardedInit?.method).toBe('GET');
    expect(forwardedInit?.body).toBeUndefined();
    expect(forwardedInit?.signal).toBe(controller.signal);
    expect(new Headers(forwardedInit?.headers).get('x-trace-id')).toBe('locate-1');
    expect(new Headers(forwardedInit?.headers).has('content-type')).toBe(false);
  });

  it('preserves locate abort rejection from the exact forwarded signal', async () => {
    const controller = new AbortController();
    vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => (
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), {
          once: true,
        });
      })
    )));
    const request = locateSheetRowContract(
      'project-1',
      7,
      11,
      { page_size: 500 },
      contractError,
      { signal: controller.signal },
    );
    const abortError = new DOMException('cancelled', 'AbortError');
    controller.abort(abortError);
    await expect(request).rejects.toBe(abortError);
  });

  it('RealApi decodes project once and preserves legacy rowIds scope precedence', async () => {
    realApi = createProjectApi('tenant/a % hostile');
    const requests: Array<RequestInfo | URL> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requests.push(input);
      return jsonResponse({
        schema_version: 'frisket.sheet_row_location.v1',
        sheet_id: 7,
        row_id: 11,
        found: true,
        index: 501,
        page_offset: 500,
        page_size: 500,
      });
    }));

    await realApi.locateSheetRow('7', '11', {
      parentRowId: '0',
      filter: { name: { contains: 'Ada' } },
      sort: [{ columnId: '9', direction: 'desc' }],
      rowIds: ['99', '100'],
    });
    const url = new URL(String(requests[0]), 'https://frisket.invalid');
    expect(url.pathname).toBe('/api/projects/tenant%2Fa%20%25%20hostile/sheets/7/rows/11/locate');
    expect(url.searchParams.get('page_size')).toBe('500');
    expect(url.searchParams.get('parent_row_id')).toBe('0');
    expect(url.searchParams.get('row_ids')).toBe('99,100');
    expect(url.searchParams.has('filter')).toBe(false);
    expect(url.searchParams.has('sort')).toBe(false);

    await realApi.locateSheetRow('7', '11', {
      parentRowId: '0',
      filter: { name: { contains: 'Ada' } },
      sort: [{ columnId: '9', direction: 'desc' }],
    });
    const viewUrl = new URL(String(requests[1]), 'https://frisket.invalid');
    expect(JSON.parse(viewUrl.searchParams.get('filter') ?? '')).toEqual({
      name: { contains: 'Ada' },
    });
    expect(JSON.parse(viewUrl.searchParams.get('sort') ?? '')).toEqual([
      { columnId: '9', direction: 'desc' },
    ]);
    expect(viewUrl.searchParams.has('row_ids')).toBe(false);
  });

  it('RealApi preserves empty rowIds and legacy message-only errors', async () => {
    realApi = createProjectApi('project-1');
    const requests: Array<RequestInfo | URL> = [];
    const responses = [
      jsonResponse({
        schema_version: 'frisket.sheet_row_location.v1',
        sheet_id: 7,
        row_id: 11,
        found: false,
        index: null,
        page_offset: null,
        page_size: 500,
      }),
      jsonResponse({ detail: 'column blocked' }, 409),
      jsonResponse({ detail: 'row missing' }, 404),
      jsonResponse({}, 500),
    ];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requests.push(input);
      return responses.shift()!;
    }));

    await realApi.locateSheetRow('7', '11', { filter: {}, sort: [], rowIds: [] });
    expect(String(requests[0])).toBe(
      '/api/projects/project-1/sheets/7/rows/11/locate?page_size=500&row_ids=',
    );

    const error = await realApi.getColumnStats('7', '9').catch((caught) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 409,
      message: 'column blocked',
      code: undefined,
      details: undefined,
    });

    await expect(realApi.getColumnStats('7', '9')).rejects.toMatchObject({
      status: 404,
      message: 'row missing',
      code: undefined,
      details: undefined,
    });
    await expect(realApi.getColumnStats('7', '9')).rejects.toMatchObject({
      status: 500,
      message: 'Request failed (HTTP 500)',
      code: undefined,
      details: undefined,
    });
  });

  it('percent-encodes paths and preserves zero query boundaries and row_ids', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(sheetDataWire);
    }));

    await expect(getSheetDataContract(
      'tenant/project 1',
      7,
      { offset: 0, limit: 0, row_ids: '9,3' },
      contractError,
    )).resolves.toMatchObject({ total: 0 });

    expect(requests).toHaveLength(1);
    expect(requests[0]?.input).toBe(
      '/api/projects/tenant%2Fproject%201/sheets/7/data?offset=0&limit=0&row_ids=9%2C3',
    );
    expect(requests[0]?.init?.method).toBe('GET');
  });

  it('sends the sheet update body and merges caller headers with JSON content type', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse({ id: 7, title_column_id: null });
    }));

    await expect(updateSheetContract(
      'tenant/project 1',
      7,
      null,
      contractError,
      { headers: { Authorization: 'Bearer token', 'X-Trace-Id': 'trace-1' } },
    )).resolves.toBeUndefined();

    expect(requests).toHaveLength(1);
    expect(requests[0]?.input).toBe('/api/projects/tenant%2Fproject%201/sheets/7');
    expect(requests[0]?.init?.method).toBe('PATCH');
    expect(requests[0]?.init?.body).toBe(JSON.stringify({ title_column_id: null }));
    const headers = new Headers(requests[0]?.init?.headers);
    expect(headers.get('authorization')).toBe('Bearer token');
    expect(headers.get('x-trace-id')).toBe('trace-1');
    expect(headers.get('content-type')).toBe('application/json');
  });

  it('maps only the route-specific declared statuses', async () => {
    const responses = [
      jsonResponse({ detail: 'schema mismatch' }, 409),
      jsonResponse({ detail: 'sheet not found' }, 404),
      jsonResponse({ detail: 'schema mismatch' }, 409),
      jsonResponse({ detail: 'forbidden' }, 403),
      jsonResponse({ detail: 'invalid row_ids' }, 400),
    ];
    vi.stubGlobal('fetch', vi.fn(async () => responses.shift()!));
    const errorFactory = vi.fn(
      (status: number, payload: unknown) => new MappedContractError(status, payload),
    );

    await expect(listSheetsContract('project-1', errorFactory)).rejects.toMatchObject({
      name: 'MappedContractError',
      status: 409,
    });

    await expect(updateSheetContract(
      'project-1',
      7,
      null,
      errorFactory,
    )).rejects.toMatchObject({
      name: 'MappedContractError',
      status: 404,
    });

    await expect(updateSheetContract(
      'project-1',
      7,
      null,
      errorFactory,
    )).rejects.toMatchObject({
      name: 'MappedContractError',
      status: 409,
    });

    await expect(updateSheetContract(
      'project-1',
      7,
      null,
      errorFactory,
    )).rejects.toMatchObject({
      name: 'MappedContractError',
      status: 403,
    });

    await expect(getSheetDataContract(
      'project-1',
      7,
      { row_ids: 'invalid' },
      errorFactory,
    )).rejects.toMatchObject({
      name: 'MappedContractError',
      status: 400,
    });
    expect(errorFactory).toHaveBeenCalledTimes(5);
  });

  it('maps an additional non-2xx status', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => (
      jsonResponse({ detail: 'teapot' }, 418)
    )));
    const errorFactory = vi.fn(
      (status: number, payload: unknown) => new MappedContractError(status, payload),
    );

    await expect(getSheetDataContract(
      'project-1',
      7,
      {},
      errorFactory,
    )).rejects.toMatchObject({
      name: 'MappedContractError',
      status: 418,
      payload: { detail: 'teapot' },
    });
    expect(errorFactory).toHaveBeenCalledOnce();
    expect(errorFactory).toHaveBeenCalledWith(418, { detail: 'teapot' });
  });

  it('forwards the exact signal and headers and preserves abort rejection', async () => {
    const controller = new AbortController();
    let forwardedInit: RequestInit | undefined;
    vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      forwardedInit = init;
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), {
          once: true,
        });
      });
    }));

    const request = listSheetsContract('project-1', contractError, {
      signal: controller.signal,
      headers: { 'X-Trace-Id': 'trace-list' },
    });
    const abortError = new DOMException('cancelled', 'AbortError');
    controller.abort(abortError);

    await expect(request).rejects.toBe(abortError);
    expect(forwardedInit?.signal).toBe(controller.signal);
    expect(new Headers(forwardedInit?.headers).get('x-trace-id')).toBe('trace-list');
  });

  it('keeps a composite sheet list on the project captured before its first await', async () => {
    let resolveList!: (response: Response) => void;
    const deferredList = new Promise<Response>((resolve) => {
      resolveList = resolve;
    });
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const request = String(input);
      requests.push(request);
      if (request === '/api/projects/project-a/sheets') return deferredList;
      return jsonResponse(sheetDataWire);
    }));
    realApi = createProjectApi('project-a');
    const pending = realApi.listSheets();
    resolveList(jsonResponse(sheetListWire));

    await expect(pending).resolves.toHaveLength(1);
    expect(requests).toEqual([
      '/api/projects/project-a/sheets',
    ]);
    expect(requests.some((request) => request.includes('/project-b/'))).toBe(false);
  });

  it('keeps sequential column actions and readbacks on the project captured before its first await', async () => {
    const projectA = 'update-column-project-a';
    let resolveTypeAction!: (response: Response) => void;
    const deferredTypeAction = new Promise<Response>((resolve) => {
      resolveTypeAction = resolve;
    });
    const requests: string[] = [];
    const actionParams: unknown[] = [];
    const signals: Array<AbortSignal | null | undefined> = [];
    const actionResult = (kind: string) => ({
      schema_version: 'frisket.action_result.v1',
      action: { kind, action_id: `update-column-${kind}` },
      status: 'completed',
      project_id: projectA,
      run_id: null,
      receipt_id: null,
      outputs: [{ kind: 'column', name: 'Updated', column_id: 9 }],
      errors: [],
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      const body = init?.body === undefined
        ? null
        : JSON.parse(String(init.body)) as {
          action_id?: unknown;
          kind?: unknown;
          params?: unknown;
        };
      const kind = typeof body?.action_id === 'string'
        ? body.action_id
        : (typeof body?.kind === 'string' ? body.kind : null);
      requests.push(`${init?.method} ${path}${kind === null ? '' : ` ${kind}`}`);
      signals.push(init?.signal);
      if (kind !== null) actionParams.push(body?.params);
      if (path.endsWith('/actions/v1/run') && kind === 'column.set_type') {
        return deferredTypeAction;
      }
      if (path.endsWith('/actions/v1/run') && kind === 'column.patch') {
        return jsonResponse(actionResult(kind));
      }
      if (path.endsWith('/sheets')) return jsonResponse([{
        ...sheetListWire[0],
        columns: [{ ...sheetColumnWire, name: 'Updated', type: 'number' }],
      }]);
      throw new Error(`Unexpected update-column request: ${path}`);
    }));
    realApi = createProjectApi(projectA);
    const pending = realApi.updateColumn('9', { type: 'number', format: null });
    resolveTypeAction(jsonResponse(actionResult('column.set_type')));

    await expect(pending).resolves.toMatchObject({
      id: '9',
      type: 'number',
      format: null,
    });
    expect(requests).toEqual([
      `POST /api/projects/${projectA}/actions/v1/run column.set_type`,
      `GET /api/projects/${projectA}/sheets`,
      `POST /api/projects/${projectA}/actions/v1/run column.patch`,
      `GET /api/projects/${projectA}/sheets`,
    ]);
    expect(actionParams).toEqual([
      {
        column_id: 9,
        type: 'number',
      },
      {
        column_id: 9,
        format: null,
      },
    ]);
    expect(signals.every((signal) => signal === undefined)).toBe(true);
  });

  it('owns direct grid mutations through its injected action session', async () => {
    const requests: Array<{ path: string; body: Record<string, unknown> }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({
        path: String(input),
        body: JSON.parse(String(init?.body)) as Record<string, unknown>,
      });
      return jsonResponse({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'row.add', action_id: 'row-add-1' },
        status: 'completed',
        project_id: 'grid-owner-project',
        run_id: null,
        receipt_id: null,
        outputs: [{
          kind: 'rows', name: 'added_rows', row_ids: [11], ref: { total: 4 },
        }],
        errors: [],
      });
    }));
    const api = createSheetGridDomainApi(contractError, {
      columnRunInfo: () => undefined,
      v1ActionSession: createV1ActionSession('grid-owner-project'),
    }, 'grid-owner-project');
    await expect(api.addRow('7', {
      9: 'Ada',
      10: { nested: ['JSON', { valid: true }] },
    })).resolves.toEqual({ rowId: '11', total: 4 });
    expect(requests).toEqual([{
      path: '/api/projects/grid-owner-project/actions/v1/run',
      body: expect.objectContaining({
        action_id: 'row.add',
        scope: { kind: 'project' },
        output_names: {},
        params: {
          sheet_id: 7,
          cells: { 9: 'Ada', 10: { nested: ['JSON', { valid: true }] } },
        },
      }),
    }]);
    expect(requests[0]?.body).not.toHaveProperty('kind');
    expect(requests[0]?.body).not.toHaveProperty('capabilities');
  });

  it('posts one typed cell-edit batch and rejects non-finite values before dispatch', async () => {
    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return jsonResponse({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'cell.edit', action_id: 'cell-edit-1' },
        status: 'completed', project_id: 'cell-edit-project', run_id: null,
        receipt_id: null, outputs: [], errors: [],
      });
    }));
    const api = createSheetGridDomainApi(contractError, {
      columnRunInfo: () => undefined,
      v1ActionSession: createV1ActionSession('cell-edit-project'),
    }, 'cell-edit-project');

    await expect(api.editCells([
      { rowId: '11', columnId: '9', value: null },
      { rowId: '12', columnId: '10', value: { nested: [1, true, 'kept'] } },
    ])).resolves.toBeUndefined();
    expect(bodies).toEqual([expect.objectContaining({
      action_id: 'cell.edit',
      scope: { kind: 'project' },
      output_names: {},
      params: {
        edits: [
          { row_id: 11, column_id: 9, value: null },
          { row_id: 12, column_id: 10, value: { nested: [1, true, 'kept'] } },
        ],
      },
    })]);
    expect(bodies[0]).not.toHaveProperty('kind');
    expect(bodies[0]).not.toHaveProperty('capabilities');

    for (const value of [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY]) {
      await expect(api.editCells([{ rowId: '11', columnId: '9', value }])).rejects.toThrow(
        'Cell edit values must be JSON-compatible',
      );
    }
    expect(bodies).toHaveLength(1);
  });

  it('posts replay decisions only through generated typed project envelopes', async () => {
    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return jsonResponse({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'replay.accept', action_id: 'replay-1' },
        status: 'completed', project_id: 'replay-project', run_id: null,
        receipt_id: 'receipt-1', outputs: [], errors: [],
      });
    }));
    const api = createSheetGridDomainApi(contractError, {
      columnRunInfo: () => undefined,
      v1ActionSession: createV1ActionSession('replay-project'),
    }, 'replay-project');

    await api.acceptReplayValue('7', '9', '11', `sha256:${'a'.repeat(64)}`, '5');
    await api.acceptReplayValuesInColumn('7', '9');
    await api.dismissReplayPending('7', '9', '11', `sha256:${'a'.repeat(64)}`, '5');

    expect(bodies.map((body) => body.action_id)).toEqual([
      'replay.accept', 'replay.accept_column', 'replay.dismiss',
    ]);
    expect(bodies.map((body) => body.params)).toEqual([
      {
        sheet_id: 7, row_id: 11, column_id: 9, run_id: 5,
        generated_value_hash: `sha256:${'a'.repeat(64)}`,
      },
      { sheet_id: 7, column_id: 9 },
      {
        sheet_id: 7, row_id: 11, column_id: 9, run_id: 5,
        generated_value_hash: `sha256:${'a'.repeat(64)}`,
      },
    ]);
    for (const body of bodies) {
      expect(body).toMatchObject({ scope: { kind: 'project' }, output_names: {} });
      expect(body).not.toHaveProperty('kind');
      expect(body).not.toHaveProperty('capabilities');
    }
  });

  it('rejects non-JSON nested row cell values before dispatch', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const api = createSheetGridDomainApi(contractError, {
      columnRunInfo: () => undefined,
      v1ActionSession: createV1ActionSession('grid-json-project'),
    }, 'grid-json-project');

    await expect(api.addRow('7', {
      9: { nested: ['valid', undefined] },
    })).rejects.toThrow('Row cell values must be JSON-compatible');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('preserves JSON object keys and rejects non-plain row cell values', async () => {
    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      bodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
      return jsonResponse({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'row.add', action_id: 'row-add-json' },
        status: 'completed', project_id: 'grid-json-project', run_id: null,
        receipt_id: null,
        outputs: [{ kind: 'rows', name: 'added_rows', row_ids: [11], ref: { total: 1 } }],
        errors: [],
      });
    }));
    const api = createSheetGridDomainApi(contractError, {
      columnRunInfo: () => undefined,
      v1ActionSession: createV1ActionSession('grid-json-project'),
    }, 'grid-json-project');
    const nested = JSON.parse('{"__proto__":{"kept":true},"safe":1}') as Record<string, unknown>;
    const sparse: unknown[] = [];
    sparse[1] = 'present';

    await expect(api.addRow('7', { 9: nested })).resolves.toEqual({ rowId: '11', total: 1 });
    expect((bodies[0]?.params as { cells: Record<string, unknown> }).cells['9']).toEqual(nested);
    await expect(api.addRow('7', { 9: { when: new Date() } })).rejects.toThrow(
      'Row cell values must be JSON-compatible',
    );
    await expect(api.addRow('7', { 9: { nested: sparse } })).rejects.toThrow(
      'Row cell values must be JSON-compatible',
    );
    expect(bodies).toHaveLength(1);
  });

  it('reuses the refresh action identity across its confirmation retry', async () => {
    const project = 'refresh-confirm-project';
    const bodies: Array<Record<string, unknown>> = [];
    let postCount = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe(`/api/projects/${project}/actions/v1/run`);
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
      bodies.push(body);
      postCount += 1;
      if (postCount === 1) {
        return jsonResponse({
          schema_version: 'frisket.action_result.v1',
          action: { kind: 'sheet.refresh', action_id: 'refresh-1' },
          status: 'needs_confirmation',
          project_id: project,
          run_id: null,
          receipt_id: null,
          outputs: [],
          errors: [{
            code: 'confirmation_required',
            field: 'confirmation',
            message: 'Confirm refresh',
            details: { estimate: { cost: 1.25, rows: 3 }, promise_set_hash: 'refresh-hash' },
          }],
        }, 402);
      }
      return jsonResponse({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'sheet.refresh', action_id: 'refresh-1' },
        status: 'completed', project_id: project, run_id: null, receipt_id: null,
        outputs: [], errors: [],
      });
    }));
    realApi = createProjectApi(project);
    await expect(realApi.refreshSheet('7')).rejects.toMatchObject({
      name: 'ConfirmationRequiredError',
      estimate: expect.objectContaining({ promise_set_hash: 'refresh-hash' }),
    });
    await expect(realApi.refreshSheet('7', 'refresh-hash')).resolves.toEqual({
      status: 'completed', sheetId: '7', needsConfirmation: false,
    });

    expect(bodies).toHaveLength(2);
    expect(bodies[0]).toMatchObject({
      action_id: 'sheet.refresh', scope: { kind: 'project' },
      params: { sheet_id: 7 },
    });
    expect(bodies[1]).toMatchObject({
      action_id: 'sheet.refresh', scope: { kind: 'project' },
      params: { sheet_id: 7 },
      confirmation: 'refresh-hash',
    });
    expect(bodies[0]?.idempotency_key).toBe(bodies[1]?.idempotency_key);
    expect(bodies[0]).not.toHaveProperty('confirmation');
    expect(bodies[1]?.params).toEqual({ sheet_id: 7 });
  });

  it('maps options, rows, columns, provenance, and remembered run metadata directly', async () => {
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requests.push(String(input));
      return jsonResponse({
        total: 1,
        columns: [{
          id: 9,
          name: 'AI score',
          type: 'text',
          format: null,
          semantic_type: null,
          default_hidden: false,
          current_run_id: 4,
          latest_run_id: 4,
          generation_managed: false,
          mixed_origins: false,
          transcript_status: null,
          media_download_candidate: null,
          replay_pending_count: 1,
          ai_generated: true,
        }],
        rows: [{
          id: 11,
          cells: { 9: { nested: true } },
          meta: {
            9: {
              state: 'error',
              error: 'bad value',
              outcome: 'invalid_output',
              confidence: 0.75,
              justification: 'review this',
              current_value_ref: {
                kind: 'run_result', row_id: 11, column_id: 9, run_id: 4, op_id: 5,
              },
              pending_value: {
                fresh_value: { nested: false },
                run_id: 6,
                generated_value_hash: 'hash-6',
              },
            },
          },
          parent_row_id: 3,
          child_count: 2,
        }],
      });
    }));
    const columnRunInfo = vi.fn(() => ({
      actionName: 'Remembered action',
      prompt: 'Remembered prompt',
      model: 'remembered-model',
    }));
    const api = createSheetGridDomainApi(contractError, {
      columnRunInfo,
      v1ActionSession: createV1ActionSession('project-a'),
    }, 'project-a');

    const page = await api.getSheetData('7', 5, 10, {
      parentRowId: '0',
      filter: { 9: { contains: 'ignored' } },
      sort: [{ column: '9', dir: 'desc' }],
      rowIds: [99, 100],
    });

    expect(requests).toEqual([
      '/api/projects/project-a/sheets/7/data?offset=5&limit=10&parent_row_id=0&row_ids=99%2C100',
    ]);
    expect(page).toEqual({
      total: 1,
      columns: [{
        id: '9', name: 'AI score', type: 'text', width: 240, format: null,
        semanticType: null, defaultHidden: false, currentRunId: '4',
        latestRunId: '4', generationManaged: false, mixedOrigins: false,
        transcriptStatus: null, mediaDownloadCandidate: null, replayPendingCount: 1,
        ai: {
          actionName: 'Remembered action', prompt: 'Remembered prompt',
          model: 'remembered-model', costSoFar: 0, versions: [],
        },
      }],
      rows: [{
        id: '11', index: 5, cells: { 9: '{"nested":true}' },
        provenance: {
          9: {
            currentValueRef: {
              kind: 'run_result', rowId: '11', columnId: '9', runId: '4', opId: '5',
            },
            model: 'remembered-model', actionName: 'Remembered action', cost: 0,
            confidence: 0.75, justification: 'review this', runId: '4',
          },
        },
        cellStates: { 9: 'error' },
        cellErrors: { 9: 'bad value' },
        cellOutcomes: { 9: 'invalid_output' },
        parentRowId: '3', childCount: 2,
        replayPending: {
          9: { freshValue: '{"nested":false}', runId: '6', generatedValueHash: 'hash-6' },
        },
      }],
    });
    expect(columnRunInfo).toHaveBeenCalledTimes(2);
    expect(columnRunInfo).toHaveBeenCalledWith(
      'project-a',
      '7',
      expect.objectContaining({ id: '9', name: 'AI score' }),
    );
  });

  it('maps managed mixed-origin columns without promoting the latest family to value authority', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      total: 2,
      columns: [{
        id: 9,
        name: 'AI score',
        type: 'text',
        format: null,
        semantic_type: null,
        default_hidden: false,
        current_run_id: null,
        latest_run_id: 6,
        generation_managed: true,
        mixed_origins: true,
        transcript_status: null,
        media_download_candidate: null,
        replay_pending_count: 0,
        ai_generated: true,
      }],
      rows: [{
        id: 11,
        cells: { 9: null },
        meta: { 9: {
          state: 'error',
          error: 'new exact error',
          outcome: 'model_error',
          current_value_ref: {
            kind: 'run_result', row_id: 11, column_id: 9, run_id: 6, op_id: 16,
          },
        } },
        parent_row_id: null,
        child_count: 0,
      }, {
        id: 12,
        cells: { 9: 'retained old exact value' },
        meta: { 9: {
          current_value_ref: {
            kind: 'run_result', row_id: 12, column_id: 9, run_id: 4, op_id: 14,
          },
        } },
        parent_row_id: null,
        child_count: 0,
      }],
    })));
    const api = createSheetGridDomainApi(contractError, {
      columnRunInfo: () => ({ actionName: 'AI run', prompt: '', model: '' }),
      v1ActionSession: createV1ActionSession('mixed-project'),
    }, 'mixed-project');

    const page = await api.getSheetData('7', 0, 20);

    expect(page.columns[0]).toMatchObject({
      currentRunId: null,
      latestRunId: '6',
      generationManaged: true,
      mixedOrigins: true,
      replayPendingCount: 0,
    });
    expect(page.rows).toMatchObject([{
      cells: { 9: null },
      cellStates: { 9: 'error' },
      cellErrors: { 9: 'new exact error' },
      provenance: { 9: { currentValueRef: { runId: '6', opId: '16' } } },
    }, {
      cells: { 9: 'retained old exact value' },
      provenance: { 9: { currentValueRef: { runId: '4', opId: '14' } } },
    }]);
  });

  it('resolves a column through the domain composite and preserves the local 404', async () => {
    const responses = [sheetListWire, sheetListWire];
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(responses.shift())));
    const api = createSheetGridDomainApi(contractError, {
      columnRunInfo: () => undefined,
      v1ActionSession: createV1ActionSession('project-a'),
    }, 'project-a');

    await expect(api.getColumnById('9')).resolves.toMatchObject({
      id: '9', name: 'Title', type: 'text', width: 240,
    });
    await expect(api.getColumnById('404')).rejects.toMatchObject({
      name: 'ApiError', status: 404, message: 'Column 404 was not found',
    });
  });
});
