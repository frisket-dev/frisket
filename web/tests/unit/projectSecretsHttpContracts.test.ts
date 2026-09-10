import { afterEach, describe, expect, it, vi } from 'vitest';

import { onActionCatalogInvalidated } from '../../src/api/catalogEvents';
import { ApiError, createProjectApi } from '../../src/api/real';
import { createProjectSecretsApi } from '../../src/api/projectSecrets';

const projectId = 'tenant /%25 ☃';
const projectApi = createProjectApi(projectId);
const secretName = 'SERVICE /%25 + TOKEN';
const catalog = {
  schemaVersion: 'frisket.project_secrets.v1',
  projectId,
  secrets: [
    {
      name: secretName,
      hint: null,
      configured: true,
      updatedAt: null,
      consumers: [{ kind: 'mcp_connector', id: 'connector /%25' }],
      producer_extension: { retained: true },
    },
  ],
  conflicts: [
    {
      plugin_id: 'plugin /%25',
      name: secretName,
      hint: null,
      status: 'pending',
      created_at: null,
    },
  ],
  producer_extension: { casing: 'unchanged' },
};
const deletion = {
  ok: true,
  deleted: true,
  name: secretName,
  producer_extension: 'unchanged',
};

const actionCatalog = {
  schema_version: 'frisket.action_catalog.v2',
  action_schema: {},
  error_schema: {},
  result_schema: {},
  receipt_schema: {},
  validation_result_schema: {},
  actions: [],
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function requestPath(input: RequestInfo | URL): string {
  return typeof input === 'string' ? input : input.toString();
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve(value: T): void;
  reject(reason: unknown): void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

afterEach(() => {
  projectApi.invalidateActionCatalog();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('project-secret generated HTTP contracts', () => {
  it('encodes raw paths once and preserves bodyless calls, exact bodies, options, and wire objects', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses = [catalog, catalog, deletion];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(responses.shift());
    }));
    const controller = new AbortController();
    const options = {
      signal: controller.signal,
      headers: { Authorization: 'Bearer project', 'X-Trace-Id': 'project-secrets' },
    };
    const api = createProjectSecretsApi((status, payload) =>
      Object.assign(new Error(`mapped ${status}`), { status, payload }),
    );

    await expect(api.getProjectSecrets(projectId, options)).resolves.toEqual(catalog);
    await expect(api.setProjectSecret(projectId, '', '', options)).resolves.toEqual(catalog);
    await expect(api.deleteProjectSecret(projectId, secretName, options)).resolves.toEqual(deletion);

    const base = '/api/projects/tenant%20%2F%2525%20%E2%98%83/secrets';
    expect(requests.map(({ input, init }) => [String(input), init?.method, init?.body])).toEqual([
      [base, 'GET', undefined],
      [base, 'POST', JSON.stringify({ name: '', value: '' })],
      [`${base}/SERVICE%20%2F%2525%20%2B%20TOKEN`, 'DELETE', undefined],
    ]);
    for (const request of requests) {
      expect(request.init?.signal).toBe(controller.signal);
      const headers = new Headers(request.init?.headers);
      expect(headers.get('authorization')).toBe('Bearer project');
      expect(headers.get('x-trace-id')).toBe('project-secrets');
    }
    expect(new Headers(requests[0].init?.headers).get('content-type')).toBeNull();
    expect(new Headers(requests[1].init?.headers).get('content-type')).toBe('application/json');
    expect(new Headers(requests[2].init?.headers).get('content-type')).toBeNull();
  });

  it('maps contract failures, preserves network and abort identity, and resolves RealApi scope per invocation', async () => {
    const projectError = {
      detail: { code: 'project_secret_denied', message: 'denied', name: secretName },
    };
    const api = createProjectSecretsApi((status, payload) =>
      Object.assign(new Error(`mapped ${status}`), { status, payload }),
    );
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(projectError, 403)));
    await expect(api.getProjectSecrets('denied')).rejects.toMatchObject({
      message: 'mapped 403', status: 403, payload: projectError,
    });

    const network = new Error('network identity');
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(network)));
    await expect(api.setProjectSecret('network', 'name', 'value')).rejects.toBe(network);

    const controller = new AbortController();
    let forwardedSignal: AbortSignal | null | undefined;
    vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      forwardedSignal = init?.signal;
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
      });
    }));
    const abort = new DOMException('aborted', 'AbortError');
    const pending = api.deleteProjectSecret('abort', 'name', { signal: controller.signal });
    controller.abort(abort);
    await expect(pending).rejects.toBe(abort);
    expect(forwardedSignal).toBe(controller.signal);

    const realRequests: string[] = [];
    const realResponses = [catalog, catalog, deletion];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      realRequests.push(String(input));
      return jsonResponse(realResponses.shift());
    }));
    const firstApi = createProjectApi('first /%25');
    const secondApi = createProjectApi('second /%25');
    const thirdApi = createProjectApi('third /%25');
    await expect(firstApi.getProjectSecrets()).resolves.toEqual(catalog);
    await expect(secondApi.setProjectSecret('', '')).resolves.toEqual(catalog);
    await expect(thirdApi.deleteProjectSecret(secretName)).resolves.toEqual(deletion);
    expect(realRequests).toEqual([
      '/api/projects/first%20%2F%2525/secrets',
      '/api/projects/second%20%2F%2525/secrets',
      '/api/projects/third%20%2F%2525/secrets/SERVICE%20%2F%2525%20%2B%20TOKEN',
    ]);

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'missing secret' }, 404)));
    await expect(thirdApi.getProjectSecrets()).rejects.toMatchObject({
      name: 'ApiError', status: 404, message: 'missing secret', code: undefined, details: undefined,
    } satisfies Partial<ApiError>);
  });

  it('keeps the action catalog cached and emits no invalidation after a read', async () => {
    const encodedProject = encodeURIComponent(projectId);
    const catalogPath = `/api/projects/${encodedProject}/actions/v1/catalog`;
    let catalogRequests = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (requestPath(input) === catalogPath) {
        catalogRequests += 1;
        return jsonResponse(actionCatalog);
      }
      return jsonResponse(catalog);
    }));

    await projectApi.listActionCatalog();
    let invalidations = 0;
    const unsubscribe = onActionCatalogInvalidated(() => {
      invalidations += 1;
    });

    await expect(projectApi.getProjectSecrets()).resolves.toEqual(catalog);
    expect(invalidations).toBe(0);
    await projectApi.listActionCatalog();
    expect(catalogRequests).toBe(1);
    unsubscribe();
  });

  it.each([
    {
      name: 'set',
      invoke: () => projectApi.setProjectSecret(secretName, ''),
      response: catalog,
    },
    {
      name: 'delete',
      invoke: () => projectApi.deleteProjectSecret(secretName),
      response: deletion,
    },
  ])('$name invalidates and refetches exactly once, only after transport success', async ({ invoke, response }) => {
    const encodedProject = encodeURIComponent(projectId);
    const catalogPath = `/api/projects/${encodedProject}/actions/v1/catalog`;
    const transport = deferred<Response>();
    let catalogRequests = 0;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      if (requestPath(input) === catalogPath) {
        catalogRequests += 1;
        return Promise.resolve(jsonResponse(actionCatalog));
      }
      return transport.promise;
    }));

    await projectApi.listActionCatalog();
    let invalidations = 0;
    const unsubscribe = onActionCatalogInvalidated(() => {
      invalidations += 1;
    });

    const pending = invoke();
    expect(invalidations).toBe(0);
    expect(catalogRequests).toBe(1);
    transport.resolve(jsonResponse(response));
    await expect(pending).resolves.toEqual(response);
    expect(invalidations).toBe(1);
    await projectApi.listActionCatalog();
    expect(catalogRequests).toBe(2);
    unsubscribe();
  });

  it.each([
    { name: 'set', invoke: () => projectApi.setProjectSecret(secretName, 'value') },
    { name: 'delete', invoke: () => projectApi.deleteProjectSecret(secretName) },
  ])('$name preserves rejection without invalidation or refetch', async ({ invoke }) => {
    const encodedProject = encodeURIComponent(projectId);
    const catalogPath = `/api/projects/${encodedProject}/actions/v1/catalog`;
    const transportError = new Error('secret transport failed');
    let catalogRequests = 0;
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      if (requestPath(input) === catalogPath) {
        catalogRequests += 1;
        return Promise.resolve(jsonResponse(actionCatalog));
      }
      return Promise.reject(transportError);
    }));

    await projectApi.listActionCatalog();
    let invalidations = 0;
    const unsubscribe = onActionCatalogInvalidated(() => {
      invalidations += 1;
    });

    await expect(invoke()).rejects.toBe(transportError);
    expect(invalidations).toBe(0);
    await projectApi.listActionCatalog();
    expect(catalogRequests).toBe(1);
    unsubscribe();
  });
});
