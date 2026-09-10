// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, type GoogleSheetsExportInput } from '../../src/api/open';
import { createProjectApi } from '../../src/api/real';

let api = createProjectApi('test-project');

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function actionResult(
  status: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    schema_version: 'frisket.action_result.v1',
    action: {
      kind: 'export.google_sheets',
      action_id: 'google-export-action',
    },
    status,
    project_id: 'google-export-admission',
    run_id: null,
    job_id: status === 'queued' || status === 'running' ? 17 : null,
    receipt_id: status === 'queued' || status === 'running' ? 'receipt-google-export' : null,
    outputs: [],
    errors: [],
    ...overrides,
  };
}

function receipt(
  status: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    schema_version: 'frisket.receipt.v1',
    receipt_id: 'receipt-google-export',
    project_id: 'google-export-admission',
    action_id: 'google-export-action',
    action_kind: 'export.google_sheets',
    run_id: null,
    op_ids: [],
    idempotency_key: 'server-does-not-rewrite-client-key',
    params_hash: 'sha256:google-export',
    status,
    inputs: [],
    outputs: [],
    provider_use: [],
    evidence: [],
    errors: [],
    ...overrides,
  };
}

function completedActionResult(spreadsheetId: string): Record<string, unknown> {
  return actionResult('completed', {
    outputs: [{
      kind: 'external',
      name: 'google_sheets',
      ref: {
        spreadsheet_id: spreadsheetId,
        spreadsheet_url: `https://docs.google.test/${spreadsheetId}`,
        updated_tabs: [],
      },
    }],
  });
}

function requestBody(init: RequestInit | undefined): Record<string, unknown> {
  return JSON.parse(String(init?.body)) as Record<string, unknown>;
}

const INPUT: GoogleSheetsExportInput = {
  connectionId: 'google-connection-06b',
  sourceKind: 'current_sheet',
  sheetId: '7',
  destinationKind: 'new_spreadsheet',
  spreadsheetTitle: 'ACTION-06B export',
};

beforeEach(() => {
  api = createProjectApi('google-export-admission');
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('RealApi Google Sheets confirmation and receipt lifecycle', () => {
  describe.each([
    {
      sourceKind: 'current_sheet' as const,
      source: { kind: 'current_sheet', sheet_id: 7 },
    },
    {
      sourceKind: 'current_view' as const,
      source: {
        kind: 'current_view', sheet_id: 7,
        query: {
          schema_version: 'frisket.query.v1', kind: 'sheet.filter',
          scope: { kind: 'sheet', sheet_id: 7 },
          filter: { city: { eq: 'Paris' } },
          sort: [{ column: 'name', dir: 'asc' }],
        },
      },
    },
    {
      sourceKind: 'all_sheets' as const,
      source: { kind: 'all_sheets' },
    },
  ])('$sourceKind canonical export', ({ sourceKind, source }) => {
    it.each([
      {
        destinationKind: 'new_spreadsheet' as const,
        destination: {
          kind: 'google_sheets', mode: 'new_spreadsheet',
          spreadsheet_title: 'ACTION-06B export',
        },
      },
      {
        destinationKind: 'update_existing' as const,
        destination: {
          kind: 'google_sheets', mode: 'update_existing',
          spreadsheet_id: 'existing-sheet',
        },
      },
    ])('posts only the typed envelope for $destinationKind', async ({ destinationKind, destination }) => {
      const posts: Record<string, unknown>[] = [];
      vi.stubGlobal('fetch', vi.fn(async (raw: RequestInfo | URL, init?: RequestInit) => {
        expect(String(raw)).toBe('/api/projects/google-export-admission/actions/v1/run');
        posts.push(requestBody(init));
        return jsonResponse(completedActionResult('sheet-exact-wire'));
      }));

      await api.exportGoogleSheets({
        ...INPUT,
        connectionId: `  ${INPUT.connectionId}  `,
        sourceKind,
        sheetId: sourceKind === 'all_sheets' ? null : INPUT.sheetId,
        currentView: {
          filter: { city: { eq: 'Paris' } },
          sort: [{ column: 'name', dir: 'asc' }],
        },
        destinationKind,
        spreadsheetId: '  existing-sheet  ',
      });

      expect(posts).toEqual([{
        action_id: 'export.google_sheets',
        scope: { kind: 'project' },
        params: {
          connection_id: INPUT.connectionId,
          source,
          destination,
          write_policy: 'replace_managed_tabs',
        },
        output_names: {},
        idempotency_key: expect.any(String),
      }]);
    });
  });

  it('keeps runless receipt polling on the project captured before its first await', async () => {
    const projectA = 'google-export-project-a';
    const projectB = 'google-export-project-b';
    let resolveLaunch!: (response: Response) => void;
    const deferredLaunch = new Promise<Response>((resolve) => {
      resolveLaunch = resolve;
    });
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (raw: RequestInfo | URL, init?: RequestInit) => {
      const path = String(raw);
      requests.push(`${init?.method} ${path}`);
      if (path.endsWith('/actions/v1/run')) return deferredLaunch;
      if (path.endsWith('/actions/v1/receipts/receipt-google-export')) {
        return jsonResponse(receipt('completed', {
          project_id: projectA,
          outputs: [{
            name: 'google_sheets',
            ref: {
              spreadsheet_id: '  sheet-project-a  ',
              spreadsheet_url: 'https://docs.google.test/sheet-project-a',
              updated_tabs: [],
            },
          }],
        }));
      }
      throw new Error(`unexpected Google Sheets project-capture request: ${path}`);
    }));

    api = createProjectApi(projectA);
    const pending = api.exportGoogleSheets({
      ...INPUT,
      spreadsheetTitle: 'captured project export',
    });
    api = createProjectApi(projectB);
    resolveLaunch(jsonResponse(actionResult('queued', { project_id: projectA })));

    await expect(pending).resolves.toMatchObject({
      spreadsheetId: '  sheet-project-a  ',
      receiptId: 'receipt-google-export',
    });
    expect(requests).toEqual([
      `POST /api/projects/${projectA}/actions/v1/run`,
      `GET /api/projects/${projectA}/actions/v1/receipts/receipt-google-export`,
    ]);
    expect(requests.some((request) => request.includes(projectB))).toBe(false);
    expect(requests.some((request) => request.includes('/runs/'))).toBe(false);
  });

  it.each([
    ['google_sheets output', []],
    ['string spreadsheet_id', [{
      name: 'google_sheets',
      ref: { spreadsheet_url: 'https://docs.google.test/missing-id' },
    }]],
    ['non-empty spreadsheet_id (empty)', [{
      name: 'google_sheets',
      ref: { spreadsheet_id: '' },
    }]],
    ['non-empty spreadsheet_id (whitespace)', [{
      name: 'google_sheets',
      ref: { spreadsheet_id: '   ' },
    }]],
  ])('rejects a completed receipt missing its %s', async (_missing, outputs) => {
    vi.stubGlobal('fetch', vi.fn(async (raw: RequestInfo | URL) => {
      const path = String(raw);
      if (path.endsWith('/actions/v1/run')) return jsonResponse(actionResult('queued'));
      if (path.endsWith('/actions/v1/receipts/receipt-google-export')) {
        return jsonResponse(receipt('completed', { outputs }));
      }
      throw new Error(`unexpected malformed Google Sheets receipt request: ${path}`);
    }));

    await expect(api.exportGoogleSheets({
      ...INPUT,
      spreadsheetTitle: `missing ${_missing}`,
    })).rejects.toMatchObject({
      name: 'ApiError',
      status: 500,
      message: 'Google Sheets export completed without a spreadsheet id',
    });
  });

  it('holds one semantic key through 402, exact retry, queued/running receipt polling, and terminal replay', async () => {
    const posts: Record<string, unknown>[] = [];
    let receiptReads = 0;
    vi.stubGlobal('fetch', vi.fn(async (raw: RequestInfo | URL, init?: RequestInit) => {
      const path = String(raw);
      if (path.endsWith('/actions/v1/run')) {
        const body = requestBody(init);
        posts.push(body);
        if (posts.length === 1) {
          return jsonResponse(actionResult('needs_confirmation', {
            errors: [{
              code: 'irreversible_external_requires_confirmation',
              message: 'Google Sheets can be changed outside Frisket.',
              field: 'confirmation',
              details: {
                reason: 'irreversible_external',
                claims: [
                  { field: 'egress_class', display: 'Data is sent to Google Sheets.' },
                ],
                promise_set_hash: 'sha256:promise-google-1',
              },
            }],
          }), 402);
        }
        if (posts.length === 2) return jsonResponse(actionResult('queued'));
        return jsonResponse(actionResult('completed', {
          receipt_id: 'receipt-google-export',
          outputs: [{
            kind: 'external',
            name: 'google_sheets',
            ref: {
              spreadsheet_id: 'sheet-terminal-replay',
              spreadsheet_url: 'https://docs.google.test/sheet-terminal-replay',
              updated_tabs: [],
            },
          }],
        }));
      }
      if (path.endsWith('/actions/v1/receipts/receipt-google-export')) {
        receiptReads += 1;
        if (receiptReads === 1) return jsonResponse(receipt('running'));
        return jsonResponse(receipt('completed', {
          outputs: [{
            name: 'google_sheets',
            ref: {
              spreadsheet_id: 'sheet-06b',
              spreadsheet_url: 'https://docs.google.test/sheet-06b',
              updated_tabs: [{ title: 'People', rows: 3 }],
            },
          }],
        }));
      }
      throw new Error(`unexpected Google Sheets request: ${path}`);
    }));

    await expect(api.exportGoogleSheets(INPUT)).rejects.toMatchObject({
      name: 'ConfirmationRequiredError',
      reason: 'irreversible_external',
      estimate: {
        promise_set_hash: 'sha256:promise-google-1',
        claims: [{ field: 'egress_class', display: 'Data is sent to Google Sheets.' }],
      },
    });

    const completion = api.exportGoogleSheets({
      ...INPUT,
      confirmation: 'sha256:promise-google-1',
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(receiptReads).toBe(1);
    await vi.advanceTimersByTimeAsync(1000);
    await expect(completion).resolves.toEqual({
      spreadsheetId: 'sheet-06b',
      spreadsheetUrl: 'https://docs.google.test/sheet-06b',
      updatedTabs: [{ title: 'People', rows: 3 }],
      receiptId: 'receipt-google-export',
    });

    const firstParams = posts[0]!.params as Record<string, unknown>;
    expect(firstParams).not.toHaveProperty('confirmed');
    expect(firstParams).not.toHaveProperty('consented_promise_set_hash');
    expect(posts[0]).not.toHaveProperty('confirmation');
    expect(posts[1]!.params).toEqual(firstParams);
    expect(posts[1]!.confirmation).toBe('sha256:promise-google-1');
    expect(posts[1]!.idempotency_key).toBe(posts[0]!.idempotency_key);

    // Completion uses the ordinary replay-window release: an immediate
    // duplicate remains the same attempt, then expires into a fresh one.
    await api.exportGoogleSheets(INPUT);
    expect(posts[2]!.idempotency_key).toBe(posts[0]!.idempotency_key);
    await vi.advanceTimersByTimeAsync(5001);
    await api.exportGoogleSheets(INPUT);
    expect(posts[3]!.idempotency_key).not.toBe(posts[0]!.idempotency_key);
  });

  it('retains the accepted key when receipt polling fails before terminal', async () => {
    const posts: Record<string, unknown>[] = [];
    let receiptFailed = false;
    vi.stubGlobal('fetch', vi.fn(async (raw: RequestInfo | URL, init?: RequestInit) => {
      const path = String(raw);
      if (path.endsWith('/actions/v1/run')) {
        posts.push(requestBody(init));
        return posts.length === 1
          ? jsonResponse(actionResult('queued'))
          : jsonResponse(completedActionResult('sheet-poll-retry'));
      }
      if (path.endsWith('/actions/v1/receipts/receipt-google-export') && !receiptFailed) {
        receiptFailed = true;
        throw new Error('receipt transport interrupted');
      }
      throw new Error(`unexpected Google Sheets request: ${path}`);
    }));

    await expect(api.exportGoogleSheets({ ...INPUT, spreadsheetTitle: 'poll interruption' }))
      .rejects.toThrow('receipt transport interrupted');
    await api.exportGoogleSheets({ ...INPUT, spreadsheetTitle: 'poll interruption' });
    expect(posts[1]!.idempotency_key).toBe(posts[0]!.idempotency_key);
  });

  it('mints a fresh key after a preaccept non-402 failure', async () => {
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_raw: RequestInfo | URL, init?: RequestInit) => {
      posts.push(requestBody(init));
      return posts.length === 1
        ? jsonResponse({ detail: 'temporarily unavailable' }, 503)
        : jsonResponse(completedActionResult('sheet-preaccept-retry'));
    }));

    const input = { ...INPUT, spreadsheetTitle: 'preaccept failure' };
    await expect(api.exportGoogleSheets(input)).rejects.toBeInstanceOf(ApiError);
    await api.exportGoogleSheets(input);
    expect(posts[1]!.idempotency_key).not.toBe(posts[0]!.idempotency_key);
  });

  it('mints a fresh key after a stored terminal failure', async () => {
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_raw: RequestInfo | URL, init?: RequestInit) => {
      posts.push(requestBody(init));
      return posts.length === 1
        ? jsonResponse(actionResult('failed', {
            errors: [{ code: 'google_export_failed', message: 'provider rejected export' }],
          }))
        : jsonResponse(completedActionResult('sheet-terminal-retry'));
    }));

    const input = { ...INPUT, spreadsheetTitle: 'terminal failure' };
    await expect(api.exportGoogleSheets(input)).rejects.toThrow('provider rejected export');
    await api.exportGoogleSheets(input);
    expect(posts[1]!.idempotency_key).not.toBe(posts[0]!.idempotency_key);
  });
});
