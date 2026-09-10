import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import { ApiError, confirmImportRowsDraft, detectPastedRowsDraft, importCsv, importFiles, importPdf, importXlsx, importUrls, previewCsv, previewImportRowUpdates, seedSampleProject } from '../../src/api/open';
import {
  createOnboardingImportsApi,
  type OnboardingImportOptions,
} from '../../src/api/onboardingImports';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function pasteDraftResponse(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: 'frisket.import_draft.v2',
    draft_id: 'paste@sha256:example',
    source_kind: 'paste',
    sheet_name: 'Pasted rows',
    row_count: 1,
    columns: [{
      key: 'name',
      name: 'Name',
      type: 'text',
      include: true,
      format: null,
      sample_values: ['Ada'],
      producer_column_extension: { retained: { nested: [null, 'value'] } },
    }, {
      key: 'note',
      name: 'Note',
      type: 'text',
      include: true,
      sample_values: [null],
      producer_column_extension: { retained: null },
    }],
    preview_rows: [{ name: 'Ada', producer_row_extension: { retained: true } }],
    warnings: ['producer warning'],
    source: {
      kind: 'inline',
      label: 'Pasted rows',
      fingerprint: 'sha256:example',
      line_count: 2,
      producer_source_extension: { retained: true },
    },
    producer_extension: { retained: true },
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('onboarding/import generated HTTP contracts', () => {
  it('posts FollowTheMoney multipart fields through its generated contract', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse({
        schema_version: 'frisket.action_result.v1',
        status: 'completed',
        outputs: [{ kind: 'sheet', sheet_id: 88 }],
        value: { dataset_id: 'dataset-1' },
      });
    }));
    const api = createOnboardingImportsApi(
      (status, payload) => Object.assign(new Error(`mapped-${status}`), { status, payload }),
      'ftm/project',
    );
    const file = new File(['{"id":"x"}\n'], 'entities.jsonl', { type: 'application/json' });

    await expect(api.importFollowTheMoney(file, 'Entities')).resolves.toMatchObject({
      status: 'completed',
      outputs: [{ kind: 'sheet', sheet_id: 88 }],
    });
    expect(String(requests[0]?.input)).toBe('/api/projects/ftm%2Fproject/import/followthemoney');
    const body = requests[0]?.init?.body as FormData;
    expect(body).toBeInstanceOf(FormData);
    expect(body.get('file')).toBe(file);
    expect(body.get('dataset_name')).toBe('Entities');
    expect(new Headers(requests[0]?.init?.headers).get('content-type')).toBeNull();
  });

  it('plans bulk file import with aligned multipart paths and executes exact decisions in output order', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(requests.length === 1
        ? {
            plan_id: 'bulk-plan-7',
            questions: [{
              id: 'matching-csv',
              kind: 'csv_combine',
              default: 'combine',
              logical_paths: ['mail/inbox.csv', 'mail/sent.csv'],
            }],
            proposed_outputs: [{
              id: 'messages',
              kind: 'csv_group',
              sheet_name: 'Messages',
              logical_paths: ['mail/inbox.csv', 'mail/sent.csv'],
            }],
          }
        : {
            created: [
              {
                id: 'inbox', kind: 'sheet', sheet_id: 23, sheet_name: 'Inbox',
                logical_paths: ['mail/inbox.csv'],
              },
              {
                id: 'sent', kind: 'sheet', sheet_id: 29, sheet_name: 'Sent',
                logical_paths: ['mail/sent.csv'],
              },
            ],
            failed: [],
            first_sheet_id: 23,
            message: 'Imported 2 files.',
          });
    }));
    const api = createOnboardingImportsApi(
      (status, payload) => Object.assign(new Error(`mapped-${status}`), { status, payload }),
      'bulk/project',
    ) as unknown as {
      planBulkImport(
        files: File[], logicalPaths: string[], expandArchive: boolean, options?: OnboardingImportOptions,
      ): Promise<{ plan_id: string; questions: unknown[]; proposed_outputs: unknown[] }>;
      executeBulkImport(
        planId: string, decisions: Record<string, string>, options?: OnboardingImportOptions,
      ): Promise<{
        created: Array<{ id: string; kind: string; sheet_id: number; sheet_name: string; logical_paths: string[] }>;
        failed: unknown[];
        first_sheet_id: number;
        message: string;
        warnings: string[];
      }>;
    };
    const options = {
      headers: { Authorization: 'Bearer bulk-import' },
      signal: new AbortController().signal,
    };
    const inbox = new File(['subject,body\nhello,world\n'], 'inbox.csv', { type: 'text/csv' });
    const sent = new File(['subject,body\nreply,thanks\n'], 'sent.csv', { type: 'text/csv' });

    await expect(api.planBulkImport(
      [inbox, sent], ['mail/inbox.csv', 'mail/sent.csv'], false, options,
    )).resolves.toMatchObject({
      plan_id: 'bulk-plan-7',
      questions: [{
        id: 'matching-csv',
        kind: 'csv_combine',
        default: 'combine',
        logical_paths: ['mail/inbox.csv', 'mail/sent.csv'],
      }],
      proposed_outputs: [{
        id: 'messages',
        kind: 'csv_group',
        sheet_name: 'Messages',
        logical_paths: ['mail/inbox.csv', 'mail/sent.csv'],
      }],
    });
    const execution = await api.executeBulkImport('bulk-plan-7', {
      'matching-csv': 'separate',
    }, options);
    expect(execution.created).toEqual([
      { id: 'inbox', kind: 'sheet', sheet_id: 23, sheet_name: 'Inbox', logical_paths: ['mail/inbox.csv'] },
      { id: 'sent', kind: 'sheet', sheet_id: 29, sheet_name: 'Sent', logical_paths: ['mail/sent.csv'] },
    ]);
    expect(execution).toMatchObject({ failed: [], first_sheet_id: 23, message: 'Imported 2 files.' });
    expect(execution.warnings).toEqual([]);

    const base = '/api/projects/bulk%2Fproject/import/bulk';
    expect(requests.map(({ input, init }) => [String(input), init?.method])).toEqual([
      [`${base}/plan`, 'POST'],
      [`${base}/bulk-plan-7/execute`, 'POST'],
    ]);
    const planBody = requests[0]?.init?.body as FormData;
    expect(planBody).toBeInstanceOf(FormData);
    expect(planBody.getAll('files')).toEqual([inbox, sent]);
    expect(planBody.getAll('logical_paths')).toEqual(['mail/inbox.csv', 'mail/sent.csv']);
    expect(planBody.get('expand_archive')).toBe('false');
    expect(new Headers(requests[0]?.init?.headers).get('content-type')).toBeNull();
    expect(JSON.parse(String(requests[1]?.init?.body))).toEqual({
      decisions: {
        'matching-csv': 'separate',
      },
    });
    expect(new Headers(requests[1]?.init?.headers).get('content-type')).toBe('application/json');
    for (const request of requests) {
      expect(request.init?.signal).toBe(options.signal);
      expect(new Headers(request.init?.headers).get('authorization')).toBe('Bearer bulk-import');
    }
  });

  it('preserves string warnings from bulk execution while accepting older responses without them', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      created: [],
      failed: [],
      first_sheet_id: 17,
      message: 'Imported email.',
      warnings: ['mail/bad.eml: <img src="inert">'],
    })));
    const api = createOnboardingImportsApi(
      (status, payload) => Object.assign(new Error(`mapped-${status}`), { status, payload }),
      'bulk/project',
    );

    await expect(api.executeBulkImport('warning-plan', {})).resolves.toMatchObject({
      first_sheet_id: 17,
      warnings: ['mail/bad.eml: <img src="inert">'],
    });
  });

  it('preserves the grouped failure path-omission count from the generated bulk contract', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      created: [],
      failed: [{
        id: 'failed-emails',
        kind: 'email',
        logical_paths: ['mail/first.eml', 'mail/second.eml'],
        logical_paths_omitted: 37,
        error: 'Mailbox parsing failed.',
      }],
      failed_omitted: 0,
      first_sheet_id: null,
      message: '1 output failed.',
      warnings: [],
    })));
    const api = createOnboardingImportsApi(
      (status, payload) => Object.assign(new Error(`mapped-${status}`), { status, payload }),
      'bulk/project',
    );

    await expect(api.executeBulkImport('failed-email-plan', {})).resolves.toMatchObject({
      failed: [{
        logical_paths: ['mail/first.eml', 'mail/second.eml'],
        logical_paths_omitted: 37,
        error: 'Mailbox parsing failed.',
      }],
    });
  });

  it('uses exact routes, native multipart FormData, JSON encoding, headers, and abort', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(requests.length === 1
        ? {
            ok: true,
            project_id: 'hostile /%25 ☃',
            sheet_id: 3,
            sheet_name: 'Articles',
            blank_column: 'summary',
            rows: 600,
          }
        : requests.length === 2
          ? pasteDraftResponse()
          : requests.length === 3
            ? { sheet_id: 3, rows: 1, downloaded: 1, failed: 0 }
            : requests.length === 4
              ? { sheet_id: 4, rows: 1, columns: ['name'], encoding: 'utf-8' }
              : requests.length === 5
                ? { sheet_id: 5, rows: 1, columns: ['name'] }
                : requests.length === 6
                  ? { sheet_id: 6, rows: 1, pages: 1, columns: ['page', 'text'] }
                  : { sheet_id: 7, rows: 2 });
    }));
    const controller = new AbortController();
    const api = createOnboardingImportsApi((status, payload) =>
      Object.assign(new Error(`mapped-${status}`), { status, payload }),
      'hostile /%25 ☃',
    );
    const options = {
      signal: controller.signal,
      headers: { Authorization: 'Bearer onboarding', 'X-Trace-Id': 'onboarding-import' },
    };
    const file = new File(['name\nAda\n'], 'rows.csv', { type: 'text/csv' });
    await expect(api.seedSampleProject('hostile /%25 ☃', options)).resolves.toMatchObject({
      sheet_id: 3,
    });
    await expect(api.detectPastedRowsDraft('name\nAda\n', options)).resolves.toEqual(
      pasteDraftResponse(),
    );
    await expect(api.importUrls(['https://example.test/a.mp3'], options)).resolves.toEqual({
      sheet_id: 3, rows: 1, downloaded: 1, failed: 0,
    });
    await expect(api.importCsv(file, options)).resolves.toEqual({
      sheet_id: 4,
      rows: 1,
      columns: ['name'],
      encoding: 'utf-8',
    });
    await expect(api.importXlsx(file, options)).resolves.toMatchObject({ sheet_id: 5, rows: 1 });
    await expect(api.importPdf(file, options)).resolves.toMatchObject({ sheet_id: 6, rows: 1 });
    await expect(api.importFiles([file, new File(['two'], 'two.txt')], options)).resolves.toMatchObject({ sheet_id: 7, rows: 2 });

    const base = '/api/projects/hostile%20%2F%2525%20%E2%98%83';
    expect(requests.slice(0, 3).map(({ input, init }) => [String(input), init?.method, init?.body])).toEqual([
      [`${base}/seed-sample`, 'POST', undefined],
      [`${base}/import/drafts/paste`, 'POST', JSON.stringify({ raw: 'name\nAda\n' })],
      [`${base}/import/urls`, 'POST', JSON.stringify({ urls: ['https://example.test/a.mp3'] })],
    ]);
    expect(requests[3]?.input).toBe(`${base}/import/csv`);
    expect(requests[3]?.init?.body).toBeInstanceOf(FormData);
    expect((requests[3]?.init?.body as FormData).get('file')).toBe(file);
    expect(requests.slice(4).map(({ input }) => String(input))).toEqual([
      `${base}/import/xlsx`, `${base}/import/pdf`, `${base}/import/files`,
    ]);
    expect((requests[4]?.init?.body as FormData).get('file')).toBe(file);
    expect((requests[5]?.init?.body as FormData).get('file')).toBe(file);
    expect((requests[6]?.init?.body as FormData).getAll('files')).toEqual([
      file, expect.any(File),
    ]);
    for (const request of requests) {
      expect(request.init?.signal).toBe(controller.signal);
      const headers = new Headers(request.init?.headers);
      expect(headers.get('authorization')).toBe('Bearer onboarding');
      expect(headers.get('x-trace-id')).toBe('onboarding-import');
    }
    expect(new Headers(requests[0]?.init?.headers).get('content-type')).toBeNull();
    for (const request of requests.slice(1, 3)) {
      expect(new Headers(request.init?.headers).get('content-type')).toBe('application/json');
    }
    expect(new Headers(requests[3]?.init?.headers).get('content-type')).toBeNull();
    expect(fetch).toHaveBeenCalledTimes(7);
  });

  it('keeps legacy paste limits, URL body omission, bare v1 errors, and network identity stable', async () => {
    const fetch = vi.fn(async () => jsonResponse({
      schema_version: 'frisket.action_result.v1',
      action: { kind: 'import.urls', action_id: 'url-1' },
      status: 'failed',
      project_id: 'project/one',
      errors: [{ code: 'url_import_failed', message: 'download refused', details: { url: 'x' } }],
    }, 500));
    vi.stubGlobal('fetch', fetch);
    await expect(importUrls('project/one', ['  https://example.test/a.mp3  ', ' ', 'https://example.test/b.mp3']))
      .rejects.toMatchObject({
        name: 'ApiError', status: 500, code: 'url_import_failed', message: 'download refused',
      } satisfies Partial<ApiError>);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]?.[0]).toBe('/api/projects/project%2Fone/import/urls');
    expect(fetch.mock.calls[0]?.[1]?.body).toBe(JSON.stringify({
      urls: ['https://example.test/a.mp3', 'https://example.test/b.mp3'],
    }));

    await expect(detectPastedRowsDraft('project/one', 'x'.repeat(2_000_001))).rejects.toMatchObject({
      name: 'ApiError', status: 413,
    } satisfies Partial<ApiError>);
    expect(fetch).toHaveBeenCalledTimes(1);

    const network = new Error('network identity');
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(network)));
    await expect(seedSampleProject('network/project')).rejects.toBe(network);
  });

  it('sends raw paste and reviewed mapping to the captured project for server preparation', async () => {
    const fetch = vi.fn(async () => jsonResponse({ sheet_id: 17, rows: 1, columns: ['Name'] }));
    vi.stubGlobal('fetch', fetch);
    const raw = 'name,note\nAda,omit me\n';
    const draft = { ...pasteDraftResponse(), raw };
    await expect(confirmImportRowsDraft('project /one', draft, {
      sheetName: 'People',
      columns: [
        { key: 'name', name: 'Name', type: 'text', include: true },
        { key: 'note', name: 'Note', type: 'text', include: false },
      ],
    })).resolves.toEqual({ sheet_id: 17, rows: 1, columns: ['Name'] });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]?.[0]).toBe('/api/projects/project%20%2Fone/import/drafts/paste/confirm');
    expect(JSON.parse(String(fetch.mock.calls[0]?.[1]?.body))).toEqual({
      raw, draft_id: draft.draft_id, sheet_name: 'People',
      columns: [
        { source_name: 'name', name: 'Name', type: 'text' },
        { source_name: 'note', name: null, type: 'text' },
      ],
    });
  });

  it('sends the same reviewed source and mapping for update preview and confirmation', async () => {
    const preview = {
      matched: 1, unmatched: 0, blank_keys: 0, ambiguous: 0,
      changed_cells: 1, cleared_cells: 0, samples: [], confirmation: 'sha256:preview',
    };
    const fetch = vi.fn().mockResolvedValueOnce(jsonResponse(preview))
      .mockResolvedValueOnce(jsonResponse({ sheet_id: 23, rows: 1, columns: ['Email', 'Score'] }));
    vi.stubGlobal('fetch', fetch);
    const raw = 'email,score\nada@example.test,7\n';
    const columns = [
      { key: 'email', name: 'Email', type: 'text', include: true, updatePolicy: 'match' as const },
      { key: 'score', name: 'Score', type: 'integer', include: true, updatePolicy: 'update' as const },
    ];
    await expect(previewImportRowUpdates('project-a', {
      raw, draftId: 'paste@draft', destinationSheetId: 23, columns,
      keyColumns: ['Email'], keepExistingOnBlank: false,
    })).resolves.toEqual(preview);
    await confirmImportRowsDraft('project-a', { ...pasteDraftResponse(), raw, draft_id: 'paste@draft' }, {
      sheetName: 'Ignored', destination: { kind: 'existing_sheet', sheetId: 23 }, columns,
      update: { keyColumns: ['Email'], keepExistingOnBlank: false, confirmation: preview.confirmation },
    });
    const previewBody = JSON.parse(String(fetch.mock.calls[0]?.[1]?.body));
    const confirmBody = JSON.parse(String(fetch.mock.calls[1]?.[1]?.body));
    expect(previewBody).toEqual({
      raw, draft_id: 'paste@draft', destination_sheet_id: 23,
      columns: [
        { source_name: 'email', name: 'Email', type: 'text' },
        { source_name: 'score', name: 'Score', type: 'integer' },
      ],
      key_columns: ['Email'], keep_existing_on_blank: false,
    });
    expect(confirmBody).toMatchObject({ ...previewBody, confirmation: preview.confirmation });
    expect(confirmBody).not.toHaveProperty('rows');
    expect(confirmBody).not.toHaveProperty('idempotency_key');
  });

  it('normalizes the required paste-draft core while preserving recursive producer extensions and format omission', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(pasteDraftResponse())));

    const draft = await detectPastedRowsDraft('project/one', 'name\nAda\n');

    expect(draft).toMatchObject({
      schema_version: 'frisket.import_draft.v2',
      draft_id: 'paste@sha256:example',
      row_count: 1,
      columns: [{
        key: 'name',
        format: null,
        sample_values: ['Ada'],
        producer_column_extension: { retained: { nested: [null, 'value'] } },
      }, {
        key: 'note',
        sample_values: [null],
        producer_column_extension: { retained: null },
      }],
      preview_rows: [{ name: 'Ada', producer_row_extension: { retained: true } }],
      warnings: ['producer warning'],
      source: {
        label: 'Pasted rows',
        producer_source_extension: { retained: true },
      },
      producer_extension: { retained: true },
    });
    expect(draft.columns[1]).toMatchObject({
      key: 'note',
      sample_values: [null],
      producer_column_extension: { retained: null },
    });
    expect(draft.columns[1]).not.toHaveProperty('format');
  });

  it('turns a malformed successful paste-draft response into a 500 ApiError', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(pasteDraftResponse({ row_count: 1.5 }))));

    await expect(detectPastedRowsDraft('project/one', 'name\nAda\n')).rejects.toMatchObject({
      name: 'ApiError', status: 500,
    } satisfies Partial<ApiError>);
  });

  it('requires the generated source and sample_values response fields', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(pasteDraftResponse({
      source: undefined,
    }))));

    await expect(detectPastedRowsDraft('project/one', 'name\nAda\n')).rejects.toMatchObject({
      name: 'ApiError', status: 500,
    } satisfies Partial<ApiError>);

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(pasteDraftResponse({
      columns: [{
        key: 'name', name: 'Name', type: 'text', include: true,
      }],
    }))));
    await expect(detectPastedRowsDraft('project/one', 'name\nAda\n')).rejects.toMatchObject({
      name: 'ApiError', status: 500,
    } satisfies Partial<ApiError>);
  });

  it('routes CSV preview and import encoding through generated multipart contracts', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => jsonResponse(
      String(input).includes('/preview')
        ? {
            encoding: 'cp1252',
            delimiter: ',',
            decimal_separator: '.',
            row_count: 1,
            columns: [{ name: 'name', type: 'text', format: null }],
            preview_rows: [{ name: 'Muñoz' }],
            truncated: false,
          }
        : { sheet_id: 4, rows: 1, columns: ['name'], encoding: 'cp1252' },
    )));

    const file = new File(['name\nMuñoz\n'], 'rows.csv');
    await expect(previewCsv('project/one', file, 'cp1252')).resolves.toMatchObject({
      encoding: 'cp1252', preview_rows: [{ name: 'Muñoz' }],
    });

    await expect(importCsv('project/one', file, 'cp1252')).resolves.toEqual({
      sheet_id: 4, rows: 1, columns: ['name'], encoding: 'cp1252',
    });
    expect(vi.mocked(fetch).mock.calls.map(([input]) => String(input))).toEqual([
      '/api/projects/project%2Fone/import/csv/preview?encoding=cp1252',
      '/api/projects/project%2Fone/import/csv?encoding=cp1252',
    ]);
    for (const [, init] of vi.mocked(fetch).mock.calls) {
      expect(init).toEqual(expect.objectContaining({ body: expect.any(FormData), method: 'POST' }));
      expect((init?.body as FormData).get('file')).toBe(file);
    }
  });

  it('routes the public XLSX, PDF, and files wrappers through generated multipart contracts', async () => {
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(_input);
      if (path.endsWith('/xlsx')) return jsonResponse({ sheet_id: 5, rows: 1, columns: ['name'] });
      if (path.endsWith('/pdf')) return jsonResponse({ sheet_id: 6, rows: 1, pages: 1, columns: ['page', 'text'] });
      expect((init?.body as FormData).getAll('files')).toHaveLength(2);
      return jsonResponse({ sheet_id: 7, rows: 2 });
    }));
    const first = new File(['one'], 'one.txt');
    const second = new File(['two'], 'two.txt');
    await expect(importXlsx('project/one', first)).resolves.toEqual({ sheet_id: 5, rows: 1, columns: ['name'] });
    await expect(importPdf('project/one', first)).resolves.toEqual({ sheet_id: 6, rows: 1, pages: 1, columns: ['page', 'text'] });
    await expect(importFiles('project/one', [first, second])).resolves.toEqual({ sheet_id: 7, rows: 2 });
  });
});
