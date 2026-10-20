// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { useExistingProviderValidation } from '../../src/settings/useExistingProviderValidation';
import { ProjectAiProvidersSettings, WorkspaceAiProvidersSettings } from '../../src/settings/AiProviderSettings';
import { OrganizationAiProvidersSettings } from '../../src/settings/OrganizationSettings';
import * as api from '../../src/api/open';
import type { ProviderValidateResult } from '../../src/api/types';

const projectApi = vi.hoisted(() => ({
  getProjectProviderKeys: vi.fn(), validateProjectProviderKey: vi.fn(),
}));
vi.mock('../../src/bind/useWorkspaceStores', () => ({ useWorkspaceStores: () => ({ projectApi }) }));
vi.mock('../../src/engine-selector/ModelsGatewaySettings', () => ({ ModelsGatewaySettings: () => null }));
vi.mock('../../src/api/open', async (original) => ({
  ...await original<typeof import('../../src/api/open')>(),
  listProviders: vi.fn(), providerCatalog: vi.fn(), listOrgKeys: vi.fn(),
  listOrgLocalEndpoints: vi.fn(), listModelPulls: vi.fn(), validateProviderKey: vi.fn(), validateOrgKey: vi.fn(),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const accepted: ProviderValidateResult = { provider: 'openai', ok: true, reachable: true, status: 200, detail: null };

beforeEach(() => {
  projectApi.getProjectProviderKeys.mockResolvedValue({ providers: [{
    id: 'openai', label: 'OpenAI', secret_name: 'OPENAI_API_KEY', kind: 'api', configured: true,
    hint: '…1234', spend_cap_usd: null, spent_usd: null, unmetered_calls: 0,
  }] });
  vi.mocked(api.listProviders).mockResolvedValue({ providers: [{
    id: 'openai', label: 'OpenAI', kind: 'platform_api', configured: true, source: 'env', hint: '…1234', models: [],
  }], schemaVersion: 'frisket.local_providers.v1', tier: 'local' });
  vi.mocked(api.listModelPulls).mockResolvedValue({ pulls: [] });
  vi.mocked(api.providerCatalog).mockResolvedValue({ providers: [] } as Awaited<ReturnType<typeof api.providerCatalog>>);
  vi.mocked(api.listOrgKeys).mockResolvedValue([{ provider: 'openai', hint: '…1234' }] as Awaited<ReturnType<typeof api.listOrgKeys>>);
  vi.mocked(api.listOrgLocalEndpoints).mockResolvedValue({ endpoints: [] } as Awaited<ReturnType<typeof api.listOrgLocalEndpoints>>);
});
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it.each([
  { scope: 'project', render: () => render(<ProjectAiProvidersSettings project={{ id: 'one', name: 'One', role: 'owner' }} />),
    label: 'Test OpenAI provider override', validate: projectApi.validateProjectProviderKey },
  { scope: 'workspace', render: () => render(<WorkspaceAiProvidersSettings />),
    label: 'Test OpenAI provider key', validate: vi.mocked(api.validateProviderKey) },
  { scope: 'organization', render: () => render(<OrganizationAiProvidersSettings />),
    label: 'Test openai provider key', validate: vi.mocked(api.validateOrgKey) },
])('$scope checks a stored key through its own endpoint with cancellation', async (test) => {
  test.validate.mockResolvedValue(accepted);
  test.render();
  fireEvent.click(await screen.findByRole('button', { name: test.label }));
  expect(await screen.findByText('Valid — key accepted')).toBeVisible();
  expect(test.validate).toHaveBeenCalledWith('openai', undefined, { signal: expect.any(AbortSignal) });
});

it('keeps provider checks independent and lets only the latest same-provider response publish', async () => {
  const first = deferred<ProviderValidateResult>();
  const second = deferred<ProviderValidateResult>();
  const other = deferred<ProviderValidateResult>();
  const validate = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(other.promise).mockReturnValueOnce(second.promise);
  const { result } = renderHook(() => useExistingProviderValidation('workspace', validate));
  act(() => { void result.current.testExisting('openai'); void result.current.testExisting('anthropic'); });
  act(() => { void result.current.testExisting('openai'); });
  expect(validate.mock.calls[0][1].aborted).toBe(true);
  expect(validate.mock.calls[1][1].aborted).toBe(false);
  await act(async () => { second.resolve(accepted); other.resolve({ ...accepted, ok: false, reachable: false, detail: 'Offline' }); });
  await act(async () => { first.reject(new Error('Old failure')); });
  expect(result.current.existingTests.openai).toEqual({ text: 'Valid — key accepted', tone: 'success' });
  expect(result.current.existingTests.anthropic).toEqual({ text: 'Unreachable: Offline', tone: 'warning' });
});

it('retires old scopes and permission revisions, including transports that ignore abort', async () => {
  const first = deferred<ProviderValidateResult>();
  const next = deferred<ProviderValidateResult>();
  const validate = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(next.promise);
  const { result, rerender, unmount } = renderHook(
    ({ scope, allowed }) => useExistingProviderValidation(scope, validate, allowed),
    { initialProps: { scope: 'project-one', allowed: true } },
  );
  act(() => { void result.current.testExisting('openai'); });
  rerender({ scope: 'project-two', allowed: true });
  expect(validate.mock.calls[0][1].aborted).toBe(true);
  expect(result.current.existingTests).toEqual({});
  rerender({ scope: 'project-one', allowed: true });
  expect(result.current.existingTests).toEqual({});
  rerender({ scope: 'project-two', allowed: true });
  act(() => { void result.current.testExisting('openai'); });
  await act(async () => { first.resolve(accepted); });
  expect(result.current.existingTests.openai.tone).toBe('pending');
  rerender({ scope: 'project-two', allowed: false });
  expect(validate.mock.calls[1][1].aborted).toBe(true);
  act(() => { void result.current.testExisting('openai'); });
  expect(validate).toHaveBeenCalledTimes(2);
  await act(async () => { next.resolve(accepted); });
  expect(result.current.existingTests).toEqual({});
  unmount();
});

it('preserves rejected-key status and error fallback without treating either as a save receipt', async () => {
  const validate = vi.fn().mockResolvedValueOnce({ ...accepted, ok: false, status: 401 })
    .mockRejectedValueOnce(new Error(''));
  const { result } = renderHook(() => useExistingProviderValidation('organization', validate));
  await act(async () => { await result.current.testExisting('openai'); });
  expect(result.current.existingTests.openai).toEqual({ text: 'Reachable but rejected (HTTP 401)', tone: 'error' });
  await act(async () => { await result.current.testExisting('openai', 'OpenAI'); });
  expect(result.current.existingTests.openai).toEqual({ text: 'Could not test OpenAI', tone: 'error' });
});
