// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { createElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('apache-arrow', () => ({ tableFromIPC: vi.fn() }));
vi.mock('pluralize', () => ({
  default: Object.assign((word: string) => word, {
    addIrregularRule: vi.fn(),
    addUncountableRule: vi.fn(),
  }),
}));

const mocks = vi.hoisted(() => ({
  getRuntimeConfig: vi.fn(),
  listOrgKeys: vi.fn(),
  listProviders: vi.fn(),
}));

vi.mock('../../src/api/open', () => ({
  getRuntimeConfig: mocks.getRuntimeConfig,
  listOrgKeys: mocks.listOrgKeys,
  listProviders: mocks.listProviders,
  onRuntimeConfigChanged: vi.fn(() => () => undefined),
}));

import { ApiError, createProjectApi } from '../../src/api/real';
import { createProjectProviderKeysApi } from '../../src/api/projectProviderKeys';
import { ReplayModeBanner } from '../../src/components/ReplayModeBanner';
import { EditionModuleProvider } from '../../src/editions/module';
import { LOCAL_EDITION_MODULE } from '../../src/editions/openModules';

const renderBanner = () => render(
  createElement(
    EditionModuleProvider,
    { edition: LOCAL_EDITION_MODULE },
    createElement(ReplayModeBanner),
  ),
);

const catalog = {
  schemaVersion: 'frisket.project_provider_keys.v1',
  projectId: 'tenant /%25 ☃',
  providers: [
    {
      id: 'openai',
      label: 'OpenAI',
      kind: 'platform_api',
      models: [],
      configured: true,
      hint: '…cret',
      spend_cap_usd: 0,
      updated_at: null,
    },
  ],
};

const validation = {
  provider: 'open/ai +%',
  ok: true,
  reachable: true,
  status: 200,
  detail: null,
  validation_token: '',
};

const deletion = { ok: true, deleted: true, provider: 'open/ai +%' };

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  window.history.replaceState(null, '', '/');
});

describe('project provider-key generated HTTP contracts', () => {
  it('renders arbitrary project/provider values once and preserves bodies, casing, headers, signals, and returns', async () => {
    const requests: Array<{ input: RequestInfo | URL; init?: RequestInit }> = [];
    const responses = [catalog, catalog, catalog, validation, deletion];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push({ input, init });
      return jsonResponse(responses.shift());
    }));
    const controller = new AbortController();
    const options = {
      signal: controller.signal,
      headers: { Authorization: 'Bearer project', 'X-Trace-Id': 'provider-keys' },
    };
    const api = createProjectProviderKeysApi((status, payload) =>
      Object.assign(new Error(`mapped ${status}`), { status, payload }),
    );
    const projectId = 'tenant /%25 ☃';
    const provider = 'open/ai +%';

    await expect(api.getProjectProviderKeys(projectId, options)).resolves.toEqual(catalog);
    await expect(api.setProjectProviderKey(projectId, provider, '', 0, '', options)).resolves.toEqual(catalog);
    await expect(api.setProjectProviderKey(projectId, provider, 'second', undefined, null, options)).resolves.toEqual(catalog);
    await expect(api.validateProjectProviderKey(projectId, provider, '', options)).resolves.toEqual(validation);
    await expect(api.deleteProjectProviderKey(projectId, provider, options)).resolves.toEqual(deletion);

    const base = '/api/projects/tenant%20%2F%2525%20%E2%98%83/provider-keys';
    expect(requests.map(({ input, init }) => [String(input), init?.method, init?.body])).toEqual([
      [base, 'GET', undefined],
      [base, 'POST', JSON.stringify({ provider, key: '', spend_cap_usd: 0, validation_token: '' })],
      [base, 'POST', JSON.stringify({ provider, key: 'second' })],
      [`${base}/validate`, 'POST', JSON.stringify({ provider })],
      [`${base}/open%2Fai%20%2B%25`, 'DELETE', undefined],
    ]);
    for (const request of requests) {
      expect(request.init?.signal).toBe(controller.signal);
      const headers = new Headers(request.init?.headers);
      expect(headers.get('authorization')).toBe('Bearer project');
      expect(headers.get('x-trace-id')).toBe('provider-keys');
    }
    for (const request of [requests[0], requests[4]]) {
      expect(new Headers(request.init?.headers).get('content-type')).toBeNull();
    }
    for (const request of [requests[1], requests[2], requests[3]]) {
      expect(new Headers(request.init?.headers).get('content-type')).toBe('application/json');
    }
  });

  it('maps contract failures, preserves network/abort identity, and resolves active RealApi scope per invocation', async () => {
    const projectError = { detail: { code: 'project_key_denied', message: 'denied', provider: 'openai' } };
    const api = createProjectProviderKeysApi((status, payload) =>
      Object.assign(new Error(`mapped ${status}`), { status, payload }),
    );
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(projectError, 403)));
    await expect(api.getProjectProviderKeys('denied')).rejects.toMatchObject({
      message: 'mapped 403', status: 403, payload: projectError,
    });

    const network = new Error('network identity');
    vi.stubGlobal('fetch', vi.fn(async () => Promise.reject(network)));
    await expect(api.setProjectProviderKey('network', 'openai', 'secret')).rejects.toBe(network);

    const controller = new AbortController();
    let forwardedSignal: AbortSignal | null | undefined;
    vi.stubGlobal('fetch', vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      forwardedSignal = init?.signal;
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true });
      });
    }));
    const abort = new DOMException('aborted', 'AbortError');
    const pending = api.deleteProjectProviderKey('abort', 'openai', { signal: controller.signal });
    controller.abort(abort);
    await expect(pending).rejects.toBe(abort);
    expect(forwardedSignal).toBe(controller.signal);

    const realRequests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      realRequests.push(String(input));
      return jsonResponse(catalog);
    }));
    const firstApi = createProjectApi('first /%25');
    const secondApi = createProjectApi('second /%25');
    await expect(firstApi.getProjectProviderKeys()).resolves.toEqual(catalog);
    await expect(secondApi.setProjectProviderKey('openai', 'secret', 0, '')).resolves.toEqual(catalog);
    expect(realRequests).toEqual([
      '/api/projects/first%20%2F%2525/provider-keys',
      '/api/projects/second%20%2F%2525/provider-keys',
    ]);

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'missing key' }, 404)));
    await expect(secondApi.validateProjectProviderKey('openai')).rejects.toMatchObject({
      name: 'ApiError', status: 404, message: 'missing key', code: undefined, details: undefined,
    } satisfies Partial<ApiError>);
  });

  it('uses the typed arbitrary-project adapter for the banner and retains labels/fallback on success or failure', async () => {
    window.history.replaceState(null, '', '/p/banner%20project%2F%25');
    mocks.getRuntimeConfig.mockResolvedValue({
      live_calls_possible: false,
      cache_mode: 'replay_strict',
      cache_mode_editable: false,
    });
    mocks.listProviders.mockResolvedValue({ providers: [] });
    mocks.listOrgKeys.mockResolvedValue([]);
    const requests: string[] = [];
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      requests.push(String(input));
      return jsonResponse({
        providers: [
          { configured: true, label: 'Route label', id: 'ignored' },
          { configured: true, id: 'fallback' },
          { configured: false, label: 'not configured', id: 'skip' },
        ],
      });
    }));

    renderBanner();
    await screen.findByText(/Route label, fallback configured, but strict replay mode/);
    expect(requests).toEqual(['/api/projects/banner%20project%2F%25/provider-keys']);

    cleanup();
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'project keys unavailable' }, 500)));
    renderBanner();
    await waitFor(() => {
      expect(screen.getByText(/No AI providers are configured/)).toBeTruthy();
    });
  });
});
