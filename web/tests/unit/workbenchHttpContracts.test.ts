import { afterEach, describe, expect, it, vi } from 'vitest';

import { createWorkbenchPluginsApi } from '../../src/api/workbenchPlugins';
import { getWorkbenchPluginRuntimeIndexContract } from '../../src/api/httpContractRoutes';

const settings = {
  schemaVersion: 'frisket.workbench_plugin_settings.v1', projectId: 'p', pluginId: 'plug',
  canMutate: true, settings: [],
};
const installed = {
  schemaVersion: 'frisket.plugin_install_plan_execution.v1', projectId: 'p', pluginId: 'plug',
  source: { submittedPath: '/plugins/plug' }, installState: 'failed',
  activation: 'manifestLoaded', runtimeSource: 'plugin.load_receipt', receiptId: 'receipt',
  manifestSha256: 'manifest', packageSha256: 'package', arbitraryPackageLoadAllowed: false,
  installFailure: { diagnostic: 'source unreadable' }, layoutMutated: false,
  workbenchDescriptorPackage: null, workbenchDescriptorManifests: null,
};
const activation = {
  schemaVersion: 'frisket.workbench_plugin_activation.v1', projectId: 'p', pluginId: 'plug',
  receiptId: 'receipt', manifestSha256: 'manifest', packageSha256: 'package',
  runtimeSource: 'plugin.load_receipt', activation: 'registryManifestRegistered',
  installState: 'enabled', registryActivated: true, arbitraryPackageLoadAllowed: false,
  permissionsAccepted: ['plugin:trusted_local_backend'], registeredPluginManifests: ['plug'],
};
const backend = {
  schemaVersion: 'frisket.workbench_plugin_backend_activation.v1', projectId: 'p', pluginId: 'plug',
  receiptId: 'receipt', manifestSha256: 'manifest', packageSha256: 'package',
  runtimeSource: 'plugin.load_receipt', arbitraryPackageLoadAllowed: false,
  executableHandlersRegistered: true, trustedRuntimeBindingsRegistered: true,
  registeredBackendContributions: { actions: ['plug.action'], detail: { nested: true } },
  registeredExecutableHandlers: { jobHandlers: 2, summary: null },
  registeredRuntimeBindings: { actions: [], featureFlag: false, priority: 3 },
};
const state = {
  schemaVersion: 'frisket.workbench_plugin_install_state.v1', projectId: 'p', pluginId: 'plug',
  receiptId: 'receipt', manifestSha256: 'manifest', packageSha256: 'package', installState: 'disabled',
  activation: 'blocked', runtimeSource: 'plugin.load_receipt', permissionsAccepted: [],
  registryActivated: false, arbitraryPackageLoadAllowed: false, disabledReason: 'plugin_disabled',
  source: { submittedPath: '/plugins/plug' }, installFailure: { diagnostic: 'still loaded' },
};

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function contractError(status: number, payload: unknown): Error {
  return new Error(`unexpected contract error ${status}: ${JSON.stringify(payload)}`);
}

class MappedContractError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super(`mapped contract error ${status}`);
    this.name = 'MappedContractError';
    this.status = status;
    this.payload = payload;
  }
}

async function expectMapped(request: Promise<unknown>, status: number): Promise<void> {
  await expect(request).rejects.toMatchObject({
    name: 'MappedContractError',
    status,
  });
}

afterEach(() => vi.unstubAllGlobals());

describe('existing workbench runtime-index HTTP contract', () => {
  it('accepts the middleware 403 and route-body schema-mismatch 409', async () => {
    const responses = [
      jsonResponse({ detail: 'viewer role required' }, 403),
      jsonResponse({ detail: 'project bundle schema mismatch' }, 409),
    ];
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return responses.shift()!;
    }));
    const errorFactory = vi.fn(
      (status: number, payload: unknown) => new MappedContractError(status, payload),
    );
    const controller = new AbortController();
    const options = {
      signal: controller.signal,
      headers: new Headers({
        Authorization: 'Bearer token',
        'Content-Type': 'application/json',
        'X-Trace-Id': 'trace-workbench',
      }),
    };

    await expectMapped(
      getWorkbenchPluginRuntimeIndexContract('project/one', errorFactory, options),
      403,
    );
    await expectMapped(
      getWorkbenchPluginRuntimeIndexContract('project/one', errorFactory, options),
      409,
    );

    expect(requests.map((request) => request.input)).toEqual([
      '/api/projects/project%2Fone/workbench/plugins',
      '/api/projects/project%2Fone/workbench/plugins',
    ]);
    expect(errorFactory).toHaveBeenCalledTimes(2);
    for (const request of requests) {
      expect(request.init?.method).toBe('GET');
      expect(request.init?.signal).toBe(controller.signal);
      const headers = new Headers(request.init?.headers);
      expect(headers.get('authorization')).toBe('Bearer token');
      expect(headers.get('content-type')).toBe('application/json');
      expect(headers.get('x-trace-id')).toBe('trace-workbench');
    }
  });

  it('maps an additional non-2xx status', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'teapot' }, 418)));
    const errorFactory = vi.fn(
      (status: number, payload: unknown) => new MappedContractError(status, payload),
    );

    await expect(
      getWorkbenchPluginRuntimeIndexContract('project-1', errorFactory),
    ).rejects.toMatchObject({
      name: 'MappedContractError', status: 418, payload: { detail: 'teapot' },
    });
    expect(errorFactory).toHaveBeenCalledOnce();
    expect(errorFactory).toHaveBeenCalledWith(418, { detail: 'teapot' });
  });

  it('forwards the exact signal and preserves abort rejection', async () => {
    const controller = new AbortController();
    let forwardedSignal: AbortSignal | null | undefined;
    vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      forwardedSignal = init?.signal;
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
      });
    }));

    const request = getWorkbenchPluginRuntimeIndexContract(
      'project-1', contractError, { signal: controller.signal },
    );
    const abortError = new DOMException('cancelled', 'AbortError');
    controller.abort(abortError);

    await expect(request).rejects.toBe(abortError);
    expect(forwardedSignal).toBe(controller.signal);
  });
});

describe('workbench plugin generated HTTP contracts', () => {
  it('preserves lifecycle URLs, bodies, consent facts, headers, abort, and bodyless changes', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses = [settings, settings, installed, activation, backend, state, state];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(responses.shift());
    }));
    const controller = new AbortController();
    const options = {
      signal: controller.signal,
      headers: { Authorization: 'Bearer session', 'X-Trace-Id': 'workbench' },
    };
    const api = createWorkbenchPluginsApi((status, payload) =>
      Object.assign(new Error('workbench failed'), { status, payload }),
    );
    const pluginId = 'plug /☃';
    const activateInput = {
      pluginId, receiptId: 'receipt', trustAcknowledged: true,
      permissionsAccepted: ['plugin:trusted_local_backend'], arbitraryPackageLoadAllowed: false,
    };

    await api.getSettings('project one', pluginId, options);
    await api.patchSettings('project one', pluginId, { 'plug.mode': 'detail' }, options);
    const localInstall = await api.installLocal('project one', {
      pluginId, source: { kind: 'localPath', value: '/plugins/plug' }, arbitraryPackageLoadAllowed: false,
    }, options);
    await api.activate('project one', activateInput, options);
    const backendActivation = await api.activateBackend('project one', {
      pluginId, trustAcknowledged: true, arbitraryPackageLoadAllowed: false, executableHandlersAllowed: true,
    }, options);
    const disabled = await api.disable('project one', pluginId, options);
    await api.uninstall('project one', pluginId, options);

    expect(localInstall).toMatchObject({
      source: { submittedPath: '/plugins/plug' },
      installFailure: { diagnostic: 'source unreadable' },
      workbenchDescriptorPackage: null,
      workbenchDescriptorManifests: null,
    });
    expect(backendActivation).toMatchObject({
      registeredBackendContributions: { detail: { nested: true } },
      registeredExecutableHandlers: { jobHandlers: 2, summary: null },
      registeredRuntimeBindings: { featureFlag: false, priority: 3 },
    });
    expect(disabled).toMatchObject({
      source: { submittedPath: '/plugins/plug' },
      installFailure: { diagnostic: 'still loaded' },
    });

    const base = '/api/projects/project%20one/workbench/plugins/plug%20%2F%E2%98%83';
    expect(requests.map(({ input, init }) => [String(input), init?.method, init?.body])).toEqual([
      [`${base}/settings`, 'GET', undefined],
      [`${base}/settings`, 'PATCH', JSON.stringify({ values: { 'plug.mode': 'detail' } })],
      [`${base}/install-local`, 'POST', JSON.stringify({ source: { kind: 'localPath', value: '/plugins/plug' }, arbitraryPackageLoadAllowed: false })],
      [`${base}/activate`, 'POST', JSON.stringify({
        receiptId: 'receipt', trustAcknowledged: true,
        permissionsAccepted: ['plugin:trusted_local_backend'], arbitraryPackageLoadAllowed: false,
      })],
      [`${base}/backend/activate`, 'POST', JSON.stringify({ trustAcknowledged: true, arbitraryPackageLoadAllowed: false, executableHandlersAllowed: true })],
      [`${base}/disable`, 'POST', undefined],
      [`${base}/uninstall`, 'POST', undefined],
    ]);
    for (const { init } of requests) {
      expect(init?.signal).toBe(controller.signal);
      const headers = new Headers(init?.headers);
      expect(headers.get('authorization')).toBe('Bearer session');
      expect(headers.get('x-trace-id')).toBe('workbench');
    }
    for (const request of requests.slice(1, 5)) {
      expect(new Headers(request.init?.headers).get('content-type')).toBe('application/json');
    }
    for (const request of requests.slice(5)) {
      expect(new Headers(request.init?.headers).get('content-type')).toBeNull();
    }
  });

  it('uses the scoped legacy ApiError mapping without producer details', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      detail: { code: 'plugin_lifecycle_not_installed', message: 'not installed', details: { private: true } },
    }, 409)));
    const { createProjectApi } = await import('../../src/api/real');
    const projectApi = createProjectApi('workbench error');

    await expect(projectApi.disableWorkbenchPlugin('plug')).rejects.toMatchObject({
      name: 'ApiError', status: 409, message: 'not installed', code: 'plugin_lifecycle_not_installed',
      details: undefined,
    });
  });
});
