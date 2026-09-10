import { afterEach, describe, expect, it, vi } from 'vitest';

import { createActionPreviewRunsApi } from '../../src/api/actionPreviewRuns';
import { createProjectApi } from '../../src/api/real';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const startWire = (previewId = 'preview-1') => jsonResponse({
  schema_version: 'frisket.action_preview.v1',
  preview_id: previewId,
  total: 2,
}, 202);

const runningWire = (previewId = 'preview-1') => jsonResponse({
  schema_version: 'frisket.action_preview.v1',
  preview_id: previewId,
  status: 'running',
  progress: { done: 0, total: 2 },
});

const terminalWire = (status: 'done' | 'error' | 'cancelled', previewId = 'preview-1') => jsonResponse({
  schema_version: 'frisket.action_preview.v1',
  preview_id: previewId,
  status,
  progress: { done: 2, total: 2 },
});

const previewNotFound = {
  schema_version: 'frisket.action_preview.v1',
  error: {
    schema_version: 'frisket.action_error.v1',
    code: 'preview_not_found',
    message: 'Preview run was not found for this project.',
    action_kind: null,
    field: null,
    details: {},
    needs_confirmation: false,
  },
};

function mappedError(status: number, payload: unknown): Error & { status: number; payload: unknown } {
  return Object.assign(new Error(`preview error ${status}`), { status, payload });
}

function action(): { kind: string; params: Record<string, never> } {
  return { kind: 'map.classify', params: {} };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('action preview project binding', () => {
  it.each([
    ['ocr', (api: ReturnType<typeof createProjectApi>, file: File) =>
      api.compareOcrScratch(file, { pages: [1], engine: 'rapidocr' })],
    ['transcribe', (api: ReturnType<typeof createProjectApi>, file: File) =>
      api.compareTranscribeScratch(file, { engine: 'whisper' })],
  ] as const)('registers a scratch-started %s preview for normal get and cancel', async (kind, startScratch) => {
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push(String(input));
      if (init?.method === 'POST') return startWire('scratch-preview');
      if (init?.method === 'GET') return runningWire('scratch-preview');
      return new Response(null, { status: 204 });
    }));
    const api = createProjectApi('project-a');
    const file = new File(['sample'], 'sample.wav', { type: 'audio/wav' });

    await expect(startScratch(api, file)).resolves.toEqual({
      previewId: 'scratch-preview', total: 2,
    });
    await api.getPreview('scratch-preview');
    await api.cancelPreview('scratch-preview');

    expect(requests.slice(1)).toEqual([
      '/api/projects/project-a/actions/v1/preview/scratch-preview',
      '/api/projects/project-a/actions/v1/preview/scratch-preview',
    ]);
    expect(requests[0]).toContain(`/api/projects/project-a/${kind}/compare-scratch`);
  });

  it('keeps scratch preview registrations isolated by project', async () => {
    const fetchMock = vi.fn(async () => startWire('scratch-a'));
    vi.stubGlobal('fetch', fetchMock);
    const projectA = createProjectApi('project-a');
    const projectB = createProjectApi('project-b');
    await projectA.compareTranscribeScratch(
      new File(['sample'], 'sample.wav', { type: 'audio/wav' }),
      { engine: 'whisper' },
    );
    const callsAfterStart = fetchMock.mock.calls.length;

    await expect(projectB.getPreview('scratch-a')).rejects.toMatchObject({ status: 400 });
    expect(fetchMock).toHaveBeenCalledTimes(callsAfterStart);
  });

  it('keeps one immutable project through the native request lifecycle', async () => {
    const requests: string[] = [];
    const api = createProjectApi('project-a');
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push(String(input));
      if (init?.method === 'POST') return startWire();
      if (init?.method === 'GET') return runningWire();
      return new Response(null, { status: 204 });
    }));

    const start = api.startPreview({
      action_id: 'map.classify',
      scope: { kind: 'sheet_rows', sheet_id: 1 },
      params: {},
      output_names: { result: 'result' },
      idempotency_key: 'preview-project-binding',
    });

    await expect(start).resolves.toEqual({ previewId: 'preview-1', total: 2 });
    await api.getPreview('preview-1');
    await api.cancelPreview('preview-1');

    expect(requests).toEqual([
      '/api/projects/project-a/actions/v1/preview',
      '/api/projects/project-a/actions/v1/preview/preview-1',
      '/api/projects/project-a/actions/v1/preview/preview-1',
    ]);
  });

  it('keeps a running preview bound for subsequent polls', async () => {
    const requests: string[] = [];
    const responses = [startWire(), runningWire(), runningWire()];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requests.push(String(input));
      return responses.shift() as Response;
    }));
    const api = createActionPreviewRunsApi(mappedError, 'project-a');

    await api.start(action());
    await api.get('preview-1');
    await api.get('preview-1');

    expect(requests).toEqual([
      '/api/projects/project-a/actions/v1/preview',
      '/api/projects/project-a/actions/v1/preview/preview-1',
      '/api/projects/project-a/actions/v1/preview/preview-1',
    ]);
  });

  it('does not bind a preview ID when start fails', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({
      schema_version: 'frisket.action_preview.v1',
      error: { code: 'preview_refused', message: 'Preview refused.' },
    }, 400));
    vi.stubGlobal('fetch', fetchMock);
    const api = createActionPreviewRunsApi(mappedError, 'project-a');

    await expect(api.start(action())).rejects.toMatchObject({ status: 400 });
    await expect(api.get('preview-1')).rejects.toMatchObject({
      status: 400,
      payload: previewNotFound,
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each(['done', 'error', 'cancelled'] as const)(
    'retires a %s preview only after its successful terminal response',
    async (status) => {
      const fetchMock = vi.fn(async () => startWire());
      vi.stubGlobal('fetch', fetchMock);
      const api = createActionPreviewRunsApi(mappedError, 'project-a');

      await api.start(action());
      fetchMock.mockResolvedValueOnce(terminalWire(status));
      await api.get('preview-1');
      const callsAfterTerminal = fetchMock.mock.calls.length;

      await expect(api.get('preview-1')).rejects.toMatchObject({
        status: 400,
        payload: previewNotFound,
      });
      expect(fetchMock).toHaveBeenCalledTimes(callsAfterTerminal);
    },
  );

  it('retires after a successful cancel, but retains binding after failed get or cancel for retries', async () => {
    const requests: string[] = [];
    const responses = [
      startWire('retry-get'),
      jsonResponse({ error: 'transient' }, 500),
      runningWire('retry-get'),
      startWire('retry-cancel'),
      jsonResponse({ error: 'transient' }, 500),
      new Response(null, { status: 204 }),
    ];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requests.push(String(input));
      return responses.shift() as Response;
    }));
    const api = createActionPreviewRunsApi(mappedError, 'project-a');

    await api.start(action());
    await expect(api.get('retry-get')).rejects.toMatchObject({ status: 500 });
    await api.get('retry-get');
    await api.start(action());
    await expect(api.cancel('retry-cancel')).rejects.toMatchObject({ status: 500 });
    await api.cancel('retry-cancel');
    const callsAfterCancel = requests.length;

    await expect(api.cancel('retry-cancel')).rejects.toMatchObject({
      status: 400,
      payload: previewNotFound,
    });
    expect(requests).toHaveLength(callsAfterCancel);
    expect(requests).toEqual([
      '/api/projects/project-a/actions/v1/preview',
      '/api/projects/project-a/actions/v1/preview/retry-get',
      '/api/projects/project-a/actions/v1/preview/retry-get',
      '/api/projects/project-a/actions/v1/preview',
      '/api/projects/project-a/actions/v1/preview/retry-cancel',
      '/api/projects/project-a/actions/v1/preview/retry-cancel',
    ]);
  });

  it('retains a preview binding when cancellation is aborted', async () => {
    const requests: string[] = [];
    let cancelAttempts = 0;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      requests.push(String(input));
      if (init?.method === 'POST') return Promise.resolve(startWire());
      cancelAttempts += 1;
      if (cancelAttempts === 2) return Promise.resolve(new Response(null, { status: 204 }));
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
      });
    }));
    const api = createActionPreviewRunsApi(mappedError, 'project-a');
    const controller = new AbortController();

    await api.start(action());
    const pending = api.cancel('preview-1', { signal: controller.signal });
    const abortError = new DOMException('preview cancel aborted', 'AbortError');
    controller.abort(abortError);

    await expect(pending).rejects.toBe(abortError);
    await expect(api.cancel('preview-1')).resolves.toBeUndefined();
    expect(requests).toEqual([
      '/api/projects/project-a/actions/v1/preview',
      '/api/projects/project-a/actions/v1/preview/preview-1',
      '/api/projects/project-a/actions/v1/preview/preview-1',
    ]);
  });

  it.each(['', 'unknown'])('rejects %j get and cancel locally without a fetch', async (previewId) => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const api = createActionPreviewRunsApi(mappedError, 'project-a');

    await expect(api.get(previewId)).rejects.toMatchObject({
      status: 400,
      payload: previewNotFound,
    });
    await expect(api.cancel(previewId)).rejects.toMatchObject({
      status: 400,
      payload: previewNotFound,
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
