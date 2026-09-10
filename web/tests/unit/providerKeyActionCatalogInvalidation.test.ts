import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  createProjectApi,
  deleteOrgKey,
  deleteProviderKey,
  discoverLocalEndpoints,
  setOrgKey,
  setProviderKey,
} from '../../src/api/real';
import { onActionCatalogInvalidated } from '../../src/api/catalogEvents';

const projectId = 'provider keys/project +%';
const projectApi = createProjectApi(projectId);
const encodedProjectId = encodeURIComponent(projectId);
const catalogPath = `/api/projects/${encodedProjectId}/actions/v1/catalog`;

const catalog = {
  schema_version: 'frisket.action_catalog.v2',
  action_schema: {},
  error_schema: {},
  result_schema: {},
  receipt_schema: {},
  validation_result_schema: {},
  actions: [],
};

function jsonResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    headers: { 'Content-Type': 'application/json' },
  });
}

function requestPath(input: RequestInfo | URL): string {
  return typeof input === 'string' ? input : input.toString();
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve(value: T): void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

const mutations = [
  {
    name: 'local endpoint discovery',
    invoke: () => discoverLocalEndpoints(),
    returnsVoid: false,
    response: {
      candidates: [
        {
          label: 'Ollama',
          origin: 'http://localhost:11434',
          outcome: 'added',
        },
      ],
    },
  },
  {
    name: 'local set',
    invoke: () => setProviderKey('openai', 'secret', 'validation-token'),
    returnsVoid: false,
    response: { schemaVersion: 'frisket.providers.v1', providers: [] },
  },
  {
    name: 'local delete',
    invoke: () => deleteProviderKey('openai'),
    returnsVoid: false,
    response: { schemaVersion: 'frisket.providers.v1', providers: [] },
  },
  {
    name: 'organization set',
    invoke: () => setOrgKey('openai', 'secret', 'validation-token'),
    returnsVoid: true,
    response: { provider: 'openai', hint: '…cret' },
  },
  {
    name: 'organization delete',
    invoke: () => deleteOrgKey('openai'),
    returnsVoid: true,
    response: { deleted: true },
  },
  {
    name: 'project set',
    invoke: () =>
      projectApi.setProjectProviderKey(
        'openai',
        'secret',
        3,
        'validation-token',
      ),
    returnsVoid: false,
    response: {
      schemaVersion: 'frisket.project_provider_keys.v1',
      projectId,
      providers: [],
    },
  },
  {
    name: 'project delete',
    invoke: () => projectApi.deleteProjectProviderKey('openai'),
    returnsVoid: false,
    response: { ok: true, deleted: true, provider: 'openai' },
  },
] as const;

afterEach(() => {
  projectApi.invalidateActionCatalog();
  vi.unstubAllGlobals();
});

describe('provider-config action-catalog invalidation', () => {
  it.each(mutations)(
    '$name clears the primed project catalog and emits exactly once after its transport fulfills',
    async ({ invoke, response, returnsVoid }) => {
      const transport = deferred<Response>();
      let catalogRequests = 0;
      vi.stubGlobal(
        'fetch',
        vi.fn((input: RequestInfo | URL) => {
          if (requestPath(input) === catalogPath) {
            catalogRequests += 1;
            return Promise.resolve(jsonResponse(catalog));
          }
          return transport.promise;
        }),
      );

      await projectApi.listActionCatalog();
      let invalidations = 0;
      const unsubscribe = onActionCatalogInvalidated(() => {
        invalidations += 1;
      });

      const pending = invoke();
      expect(invalidations).toBe(0);
      expect(catalogRequests).toBe(1);

      transport.resolve(jsonResponse(response));
      const result = await pending;

      expect(invalidations).toBe(1);
      expect(result).toEqual(returnsVoid ? undefined : response);
      await projectApi.listActionCatalog();
      expect(catalogRequests).toBe(2);
      unsubscribe();
    },
  );

  it.each(mutations)(
    '$name preserves a rejected transport error and does not clear, emit, or refetch',
    async ({ invoke }) => {
      const transportError = new Error('provider transport failed');
      let catalogRequests = 0;
      vi.stubGlobal(
        'fetch',
        vi.fn((input: RequestInfo | URL) => {
          if (requestPath(input) === catalogPath) {
            catalogRequests += 1;
            return Promise.resolve(jsonResponse(catalog));
          }
          return Promise.reject(transportError);
        }),
      );

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
    },
  );
});
