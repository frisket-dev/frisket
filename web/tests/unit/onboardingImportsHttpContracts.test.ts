import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

import { cancelImportSession, chunkImportFiles, createImportSession, importCsv, importFiles, importXlsx, listImportSessions, prepareImportFiles, prepareImportUrls, resolveImportSession, resumeImportSession, sealImportSession, seedSampleProject, snapshotImportFiles, uploadFilesToImportSession, uploadImportSessionFiles } from '../../src/api/open';
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

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('onboarding/import generated HTTP contracts', () => {
  it('prepares URL inventory without invoking the publishing URL import route', async () => {
    const prepared = { kind: 'pdf_import', draft: { draft_id: 'url-draft' }, downloaded: 1,
      failed: 1, warnings: ['One URL failed.'] };
    const fetch = vi.fn(async () => jsonResponse(prepared));
    vi.stubGlobal('fetch', fetch);
    const controller = new AbortController();
    await expect(prepareImportUrls('project/one', ['https://example.test/report.pdf', 'not-a-url'],
      { signal: controller.signal })).resolves.toEqual(prepared);
    expect(fetch).toHaveBeenCalledOnce();
    const [path, init] = fetch.mock.calls[0];
    expect(path).toBe('/api/projects/project%2Fone/import/urls/prepare');
    expect(init?.method).toBe('POST');
    expect(init?.signal).toBe(controller.signal);
    expect(JSON.parse(String(init?.body))).toEqual({ urls: ['https://example.test/report.pdf', 'not-a-url'] });
  });

  it('chunks resumable file admission by count and byte budget while allowing one oversized file', () => {
    const oversized = new File(['x'], 'oversized.bin');
    Object.defineProperty(oversized, 'size', { value: 65 * 1024 * 1024 });
    const files = [
      oversized,
      ...Array.from({ length: 257 }, (_, index) => new File(['x'], `${index}.txt`)),
    ];
    const paths = files.map((file) => `folder/${file.name}`);
    files.forEach((file, index) => {
      Object.defineProperty(file, 'webkitRelativePath', { value: paths[index] });
    });

    const chunks = Array.from(chunkImportFiles(files));

    expect(chunks.map((chunk) => chunk.files.length)).toEqual([1, 256, 1]);
    expect(chunks.map((chunk) => chunk.start)).toEqual([0, 1, 257]);
    expect(chunks.flatMap((chunk) => chunk.logicalPaths)).toEqual(paths);
  });

  it('continues chunking after the server inventory cursor without resending admitted files', () => {
    const files = Array.from({ length: 5 }, (_, index) => new File(['x'], `${index}.txt`));
    const chunks = Array.from(chunkImportFiles(files, 3));
    expect(chunks).toHaveLength(1);
    expect(chunks[0].start).toBe(3);
    expect(chunks[0].files.map((file) => file.name)).toEqual(['3.txt', '4.txt']);
  });

  it('snapshots a transient 100k-file picker once before lazy bounded upload', () => {
    let available = true;
    let reads = 0;
    const pickerFiles = new Proxy(
      { length: 100_000 } as FileList,
      {
        get(target, property, receiver) {
          if (typeof property === 'string' && /^\d+$/.test(property)) {
            const index = Number(property);
            reads += 1;
            return available ? { name: `${index}.txt`, size: 1 } as File : undefined;
          }
          return Reflect.get(target, property, receiver);
        },
      },
    );
    const files = snapshotImportFiles(pickerFiles);
    available = false; // Clearing a real input invalidates its browser-owned FileList.
    const iterator = chunkImportFiles(files);

    const first = iterator.next();

    expect(reads).toBe(100_000);
    expect(files).toHaveLength(100_000);
    expect(first.done).toBe(false);
    expect(first.value?.files).toHaveLength(256);
    expect(first.value?.start).toBe(0);
    expect(first.value?.files[0].name).toBe('0.txt');
    expect(first.value?.files[255].name).toBe('255.txt');
  });

  it('stops resumable admission after an aborted chunk without constructing later chunks', async () => {
    const controller = new AbortController();
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      controller.abort();
      throw new DOMException('cancelled', 'AbortError');
    }));
    const files = Array.from({ length: 600 }, (_, index) => new File(['x'], `${index}.txt`));
    const session = {
      import_ref: 'import-cancel', state: 'admitting' as const, admitted_files: 0,
      admitted_bytes: 0, through: 0, committed_rows: 0, committed_bytes: 0,
      sheet_id: null, sheet_name: 'Files', cancel_requested: false, sealed: false, error: null,
    };

    await expect(uploadFilesToImportSession(
      'project-1', session, files, { signal: controller.signal },
    )).rejects.toMatchObject({ name: 'AbortError' });

    expect(requests).toHaveLength(1);
    const body = requests[0].init?.body as FormData;
    expect(body.getAll('files')).toHaveLength(256);
    expect(body.get('batch_id')).toBe('import-cancel:1-256');
  });

  it('reports each acknowledged upload chunk and the sealed session', async () => {
    const files = Array.from({ length: 300 }, (_, index) => new File(['x'], `${index}.txt`));
    const initial = {
      import_ref: 'import-progress', state: 'admitting' as const, admitted_files: 0,
      admitted_bytes: 0, through: 0, committed_rows: 0, committed_bytes: 0,
      sheet_id: null, sheet_name: 'Files', cancel_requested: false, sealed: false,
      error: null, search_preparation_status: 'pending' as const,
      search_preparation_batch_id: null,
    };
    let upload = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.endsWith('/seal')) {
        return jsonResponse({ ...initial, admitted_files: 300, through: 300, sealed: true });
      }
      upload += 1;
      const admitted = upload === 1 ? 256 : 300;
      return jsonResponse({ ...initial, admitted_files: admitted, through: admitted });
    }));
    const onStatus = vi.fn();

    const result = await uploadFilesToImportSession(
      'project-1', initial, files, { onStatus, headers: { 'X-Test': 'progress' } },
    );

    expect(onStatus.mock.calls.map(([status]) => [status.admitted_files, status.sealed])).toEqual([
      [256, false], [300, false], [300, true],
    ]);
    expect(result).toMatchObject({ admitted_files: 300, sealed: true });
  });

  it('uses generated resumable import routes and aligned multipart fields', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const status = {
      import_ref: 'import-abcd', state: 'admitting', admitted_files: 0, admitted_bytes: 0,
      through: 0, committed_rows: 0, committed_bytes: 0, sheet_id: null,
      sheet_name: 'Files', cancel_requested: false, sealed: false, error: null,
    };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(String(input).endsWith('/sessions') && init?.method === 'GET'
        ? { sessions: [status] }
        : status);
    }));
    const projectId = 'project / imports';
    const first = new File(['one'], 'one.txt');
    const second = new File(['two'], 'two.pdf', { type: 'application/pdf' });

    await createImportSession(projectId, { sheetName: 'Documents' });
    await listImportSessions(projectId);
    await uploadImportSessionFiles(projectId, status.import_ref, [first, second], {
      logicalPaths: ['folder/one.txt', 'folder/two.pdf'], batchId: 'batch-1',
    });
    await sealImportSession(projectId, status.import_ref);
    await cancelImportSession(projectId, status.import_ref);
    await resumeImportSession(projectId, status.import_ref);
    await resolveImportSession(projectId, status.import_ref, 'remove');

    const base = '/api/projects/project%20%2F%20imports/import/files/sessions';
    expect(requests.map(({ input, init }) => [String(input), init?.method])).toEqual([
      [base, 'POST'], [base, 'GET'], [`${base}/import-abcd/files`, 'POST'],
      [`${base}/import-abcd/seal`, 'POST'], [`${base}/import-abcd/cancel`, 'POST'],
      [`${base}/import-abcd/resume`, 'POST'], [`${base}/import-abcd/resolve`, 'POST'],
    ]);
    expect(JSON.parse(String(requests[0].init?.body))).toEqual({ sheet_name: 'Documents' });
    const upload = requests[2].init?.body as FormData;
    expect(upload.getAll('files')).toEqual([first, second]);
    expect(upload.getAll('logical_paths')).toEqual(['folder/one.txt', 'folder/two.pdf']);
    expect(upload.get('batch_id')).toBe('batch-1');
    expect(JSON.parse(String(requests[6].init?.body))).toEqual({ decision: 'remove' });
  });

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

  it('prepares aligned archive selections through the generated discriminated-union route', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(requests.length === 1
        ? { kind: 'pdf_import', draft: { draft_id: 'archive-draft-1' } }
        : {
            kind: 'bulk',
            plan: {
              plan_id: 'archive-plan-1', questions: [], proposed_outputs: [], message: 'Ready.',
            },
          });
    }));
    const first = new File(['zip'], 'records.zip', { type: 'application/zip' });
    const second = new File(['notes'], 'notes.txt', { type: 'text/plain' });
    const signal = new AbortController().signal;

    await expect(prepareImportFiles(
      'project / archive',
      [first, second],
      ['folder/records.zip', 'folder/notes.txt'],
      true,
      { headers: { Authorization: 'Bearer archive' }, signal },
    )).resolves.toMatchObject({ kind: 'pdf_import', draft: { draft_id: 'archive-draft-1' } });
    await expect(prepareImportFiles(
      'project / archive', [first], ['records.zip'], true,
    )).resolves.toEqual({
      kind: 'bulk',
      plan: { plan_id: 'archive-plan-1', questions: [], proposed_outputs: [], message: 'Ready.' },
    });

    expect(requests).toHaveLength(2);
    expect(requests[0].input).toBe('/api/projects/project%20%2F%20archive/import/files/prepare');
    expect(requests[0].init?.method).toBe('POST');
    expect(requests[0].init?.signal).toBe(signal);
    expect(new Headers(requests[0].init?.headers).get('Authorization')).toBe('Bearer archive');
    const body = requests[0].init?.body as FormData;
    expect(body.getAll('files')).toEqual([first, second]);
    expect(body.getAll('logical_paths')).toEqual(['folder/records.zip', 'folder/notes.txt']);
    expect(body.get('expand_archive')).toBe('true');
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
                logical_paths: ['mail/inbox.csv'], search_preparation_batch_id: 'existing-prep',
              },
              {
                id: 'sent', kind: 'sheet', sheet_id: 29, sheet_name: 'Sent',
                logical_paths: ['mail/sent.csv'], search_preparation_batch_id: null,
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
      { id: 'inbox', kind: 'sheet', sheet_id: 23, sheet_name: 'Inbox', logical_paths: ['mail/inbox.csv'], search_preparation_batch_id: 'existing-prep' },
      { id: 'sent', kind: 'sheet', sheet_id: 29, sheet_name: 'Sent', logical_paths: ['mail/sent.csv'], search_preparation_batch_id: null },
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

  it('uses exact routes, native multipart FormData, headers, and abort', async () => {
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
          ? { sheet_id: 4, rows: 1, columns: ['name'], encoding: 'utf-8' }
          : requests.length === 3
            ? { sheet_id: 5, rows: 1, columns: ['name'] }
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
    await expect(api.importCsv(file, options)).resolves.toEqual({
      sheet_id: 4,
      rows: 1,
      columns: ['name'],
      encoding: 'utf-8',
    });
    await expect(api.importXlsx(file, options)).resolves.toMatchObject({ sheet_id: 5, rows: 1 });
    await expect(api.importFiles([file, new File(['two'], 'two.txt')], options)).resolves.toMatchObject({ sheet_id: 7, rows: 2 });

    const base = '/api/projects/hostile%20%2F%2525%20%E2%98%83';
    expect(requests.slice(0, 1).map(({ input, init }) => [String(input), init?.method, init?.body])).toEqual([
      [`${base}/seed-sample`, 'POST', undefined],
    ]);
    expect(requests[1]?.input).toBe(`${base}/import/csv`);
    expect(requests[1]?.init?.body).toBeInstanceOf(FormData);
    expect((requests[1]?.init?.body as FormData).get('file')).toBe(file);
    expect(requests.slice(2).map(({ input }) => String(input))).toEqual([
      `${base}/import/xlsx`, `${base}/import/files`,
    ]);
    expect((requests[2]?.init?.body as FormData).get('file')).toBe(file);
    expect((requests[3]?.init?.body as FormData).getAll('files')).toEqual([
      file, expect.any(File),
    ]);
    for (const request of requests) {
      expect(request.init?.signal).toBe(controller.signal);
      const headers = new Headers(request.init?.headers);
      expect(headers.get('authorization')).toBe('Bearer onboarding');
      expect(headers.get('x-trace-id')).toBe('onboarding-import');
    }
    expect(new Headers(requests[0]?.init?.headers).get('content-type')).toBeNull();
    expect(new Headers(requests[1]?.init?.headers).get('content-type')).toBeNull();
    expect(fetch).toHaveBeenCalledTimes(4);
  });

  it('keeps network identity stable', async () => {
    const network = new Error('network identity');
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(network)));
    await expect(seedSampleProject('network/project')).rejects.toBe(network);
  });

  it('routes CSV import encoding through the generated multipart contract', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      { sheet_id: 4, rows: 1, columns: ['name'], encoding: 'cp1252' },
    )));

    const file = new File(['name\nMuñoz\n'], 'rows.csv');
    await expect(importCsv('project/one', file, 'cp1252')).resolves.toEqual({
      sheet_id: 4, rows: 1, columns: ['name'], encoding: 'cp1252',
    });
    expect(vi.mocked(fetch).mock.calls.map(([input]) => String(input))).toEqual([
      '/api/projects/project%2Fone/import/csv?encoding=cp1252',
    ]);
    for (const [, init] of vi.mocked(fetch).mock.calls) {
      expect(init).toEqual(expect.objectContaining({ body: expect.any(FormData), method: 'POST' }));
      expect((init?.body as FormData).get('file')).toBe(file);
    }
  });

  it('routes the public XLSX and files wrappers through generated multipart contracts', async () => {
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(_input);
      if (path.endsWith('/xlsx')) return jsonResponse({ sheet_id: 5, rows: 1, columns: ['name'] });
      expect((init?.body as FormData).getAll('files')).toHaveLength(2);
      return jsonResponse({ sheet_id: 7, rows: 2 });
    }));
    const first = new File(['one'], 'one.txt');
    const second = new File(['two'], 'two.txt');
    await expect(importXlsx('project/one', first)).resolves.toEqual({ sheet_id: 5, rows: 1, columns: ['name'] });
    await expect(importFiles('project/one', [first, second])).resolves.toEqual({ sheet_id: 7, rows: 2 });
  });
});
