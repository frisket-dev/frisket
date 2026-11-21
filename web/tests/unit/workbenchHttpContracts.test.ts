import { afterEach, describe, expect, it, vi } from 'vitest';

import { createWorkbenchPluginsApi } from '../../src/api/workbenchPlugins';
import { getWorkbenchPluginRuntimeIndexContract } from '../../src/api/httpContractRoutes';
import { ApiError } from '../../src/api/contractErrors';
import { jsonResponse } from '../support/httpContractTestHelpers';

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

async function expectApiError(
  request: Promise<unknown>,
  status: number,
  message: string,
): Promise<void> {
  await expect(request).rejects.toBeInstanceOf(ApiError);
  await expect(request).rejects.toMatchObject({
    name: 'ApiError',
    status,
    message,
    code: undefined,
    details: undefined,
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
    const controller = new AbortController();
    const options = { signal: controller.signal };

    await expectApiError(
      getWorkbenchPluginRuntimeIndexContract('project/one', options),
      403,
      'viewer role required',
    );
    await expectApiError(
      getWorkbenchPluginRuntimeIndexContract('project/one', options),
      409,
      'project bundle schema mismatch',
    );

    expect(requests.map((request) => request.input)).toEqual([
      '/api/projects/project%2Fone/workbench/plugins',
      '/api/projects/project%2Fone/workbench/plugins',
    ]);
    for (const request of requests) {
      expect(request.init?.method).toBe('GET');
      expect(request.init?.signal).toBe(controller.signal);
      expect(request.init?.headers).toBeUndefined();
    }
  });
});

describe('workbench plugin generated HTTP contracts', () => {
  it('preserves lifecycle URLs, bodies, consent facts, content types, abort, and bodyless changes', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses = [settings, settings, installed, activation, backend, state, state];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(responses.shift());
    }));
    const controller = new AbortController();
    const options = { signal: controller.signal };
    const api = createWorkbenchPluginsApi('project one');
    const pluginId = 'plug /☃';
    const activateInput = {
      pluginId, receiptId: 'receipt', trustAcknowledged: true,
      permissionsAccepted: ['plugin:trusted_local_backend'], arbitraryPackageLoadAllowed: false,
    };

    await api.getWorkbenchPluginSettings(pluginId, options);
    await api.patchWorkbenchPluginSettings(pluginId, { 'plug.mode': 'detail' }, options);
    const localInstall = await api.installLocalWorkbenchPlugin({
      pluginId, source: { kind: 'localPath', value: '/plugins/plug' }, arbitraryPackageLoadAllowed: false,
    }, options);
    await api.activateWorkbenchPlugin(activateInput, options);
    const backendActivation = await api.activateWorkbenchPluginBackend({
      pluginId, trustAcknowledged: true, arbitraryPackageLoadAllowed: false, executableHandlersAllowed: true,
    }, options);
    const disabled = await api.disableWorkbenchPlugin(pluginId, options);
    await api.uninstallWorkbenchPlugin(pluginId, options);

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
    }
    expect(requests[0]?.init?.headers).toBeUndefined();
    for (const request of requests.slice(1, 5)) {
      expect([...new Headers(request.init?.headers).entries()]).toEqual([
        ['content-type', 'application/json'],
      ]);
    }
    for (const request of requests.slice(5)) {
      expect(request.init?.headers).toBeUndefined();
    }
  });

  it('maps detail-wrapped lifecycle errors to ApiError with the remaining detail keys as details', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({
      detail: { code: 'plugin_lifecycle_not_installed', message: 'not installed', details: { private: true } },
    }, 409)));
    const { createProjectApi } = await import('../../src/api/real');
    const projectApi = createProjectApi('workbench error');

    await expect(projectApi.disableWorkbenchPlugin('plug')).rejects.toMatchObject({
      name: 'ApiError', status: 409, message: 'not installed', code: 'plugin_lifecycle_not_installed',
      details: { details: { private: true } },
    });
  });
});
