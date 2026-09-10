// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { WorkbenchPluginRuntimeIndex } from '../api/open';
import { createProjectApi } from '../api/real';
import { createWorkspaceStores } from './createWorkspaceStores';
import { createPluginLayoutState, createPluginLayoutStore } from './pluginLayoutStore';

const api = createProjectApi('plugin-layout-lifecycle');

const index = (marker: string): WorkbenchPluginRuntimeIndex =>
  ({ marker, panels: [], views: [], commands: [] }) as unknown as WorkbenchPluginRuntimeIndex;

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, reject, resolve };
}

function createRuntimeApi() {
  return {
    getWorkbenchPluginRuntimeIndex: vi.fn(async () => index('default')),
  };
}

const PLUGINS_AVAILABLE = async () => true;

function setHidden(hidden: boolean): void {
  Object.defineProperty(document, 'hidden', { configurable: true, value: hidden });
}

beforeEach(() => {
  vi.useFakeTimers();
  setHidden(false);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  setHidden(false);
});

describe('createPluginLayoutStore — runtime-index owner', () => {
  it('is side-effect-free at construction and starts with the pre-migration null state', () => {
    const runtimeApi = createRuntimeApi();
    const resource = createPluginLayoutStore(runtimeApi, PLUGINS_AVAILABLE);

    expect(resource.store.get()).toEqual(createPluginLayoutState());
    expect(resource.store.get().workbenchPluginRuntimeIndex).toBeNull();
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('does not probe or poll the plugin index while availability is absent or unknown', async () => {
    const runtimeApi = createRuntimeApi();
    const pending = deferred<boolean>();
    const resource = createPluginLayoutStore(runtimeApi, () => pending.promise);

    const started = resource.start();
    expect(resource.refresh()).toBe(started);
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);

    pending.resolve(false);
    await started;
    await resource.invalidate();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    resource.dispose();
  });

  it('starts immediately, accepts success, and retains the fixed 30-second cadence', async () => {
    const runtimeApi = createRuntimeApi();
    const first = index('first');
    const second = index('second');
    runtimeApi.getWorkbenchPluginRuntimeIndex
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second);
    const resource = createPluginLayoutStore(runtimeApi, PLUGINS_AVAILABLE);

    const started = resource.start();
    await Promise.resolve();
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(1);
    await started;
    expect(resource.store.get().workbenchPluginRuntimeIndex).toBe(first);
    await resource.start();
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(1);

    await vi.advanceTimersByTimeAsync(29_999);
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).toHaveBeenCalledTimes(2);
    expect(resource.store.get().workbenchPluginRuntimeIndex).toBe(second);
    resource.dispose();
  });

  it('shares one in-flight promise across refresh, invalidate, interval, and refocus callers', async () => {
    const pending = deferred<WorkbenchPluginRuntimeIndex>();
    const runtimeApi = createRuntimeApi();
    runtimeApi.getWorkbenchPluginRuntimeIndex.mockReturnValueOnce(pending.promise);
    const resource = createPluginLayoutStore(runtimeApi, PLUGINS_AVAILABLE);

    const started = resource.start();
    expect(resource.refresh()).toBe(started);
    expect(resource.invalidate()).toBe(started);
    await vi.advanceTimersByTimeAsync(30_000);
    document.dispatchEvent(new Event('visibilitychange'));
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).toHaveBeenCalledTimes(1);

    pending.resolve(index('accepted'));
    await started;
    expect(resource.store.get().workbenchPluginRuntimeIndex).toEqual(index('accepted'));
    resource.dispose();
  });

  it('skips hidden ticks, catches up once on visibility, and does not reset interval phase', async () => {
    const runtimeApi = createRuntimeApi();
    const resource = createPluginLayoutStore(runtimeApi, PLUGINS_AVAILABLE);
    await resource.start();

    setHidden(true);
    await vi.advanceTimersByTimeAsync(90_000);
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).toHaveBeenCalledTimes(1);

    setHidden(false);
    document.dispatchEvent(new Event('visibilitychange'));
    await Promise.resolve();
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).toHaveBeenCalledTimes(2);

    await vi.advanceTimersByTimeAsync(29_999);
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).toHaveBeenCalledTimes(3);
    resource.dispose();
  });

  it('keeps initial failure silent, retains last-known-good on a transient failure, then reconciles', async () => {
    const runtimeApi = createRuntimeApi();
    const initialError = new Error('initial');
    const transientError = new Error('transient');
    const good = index('good');
    const reconciled = index('reconciled');
    runtimeApi.getWorkbenchPluginRuntimeIndex
      .mockRejectedValueOnce(initialError)
      .mockResolvedValueOnce(good)
      .mockRejectedValueOnce(transientError)
      .mockResolvedValueOnce(reconciled);
    const log = vi.spyOn(console, 'error').mockImplementation(() => {});
    const resource = createPluginLayoutStore(runtimeApi, PLUGINS_AVAILABLE);

    await resource.start();
    expect(resource.store.get().workbenchPluginRuntimeIndex).toBeNull();
    expect(log).not.toHaveBeenCalled();

    await resource.refresh();
    expect(resource.store.get().workbenchPluginRuntimeIndex).toBe(good);
    await resource.refresh();
    expect(resource.store.get().workbenchPluginRuntimeIndex).toBe(good);
    expect(log).toHaveBeenCalledWith(
      'refreshWorkbenchPluginRuntimeIndex: keeping the last-known-good runtime index after a transient fetch failure',
      transientError,
    );

    await resource.refresh();
    expect(resource.store.get().workbenchPluginRuntimeIndex).toBe(reconciled);
    resource.dispose();
  });

  it('dispose removes browser activity and generation-fences a delayed result', async () => {
    const pending = deferred<WorkbenchPluginRuntimeIndex>();
    const runtimeApi = createRuntimeApi();
    runtimeApi.getWorkbenchPluginRuntimeIndex.mockReturnValueOnce(pending.promise);
    const resource = createPluginLayoutStore(runtimeApi, PLUGINS_AVAILABLE);
    const writes = vi.fn();
    const removeVisibility = vi.spyOn(document, 'removeEventListener');
    const unsubscribe = resource.store.subscribe(writes);

    const started = resource.start();
    await Promise.resolve();
    resource.dispose();
    const disposedSnapshot = resource.store.get();
    expect(vi.getTimerCount()).toBe(0);
    expect(removeVisibility).toHaveBeenCalledWith('visibilitychange', expect.any(Function));

    pending.resolve(index('late'));
    await started;
    await resource.refresh();
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(60_000);

    expect(resource.store.get()).toBe(disposedSnapshot);
    expect(writes).not.toHaveBeenCalled();
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).toHaveBeenCalledTimes(1);
    unsubscribe();
  });

  it('restart owns a fresh request; the retired result/finally cannot overwrite or unlock it', async () => {
    const retired = deferred<WorkbenchPluginRuntimeIndex>();
    const current = deferred<WorkbenchPluginRuntimeIndex>();
    const runtimeApi = createRuntimeApi();
    runtimeApi.getWorkbenchPluginRuntimeIndex
      .mockReturnValueOnce(retired.promise)
      .mockReturnValueOnce(current.promise);
    const resource = createPluginLayoutStore(runtimeApi, PLUGINS_AVAILABLE);

    const retiredStart = resource.start();
    await Promise.resolve();
    resource.dispose();
    const currentStart = resource.start();
    await Promise.resolve();
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).toHaveBeenCalledTimes(2);

    retired.resolve(index('retired'));
    await retiredStart;
    expect(resource.store.get().workbenchPluginRuntimeIndex).toBeNull();
    const currentRefresh = resource.refresh();
    expect(resource.refresh()).toBe(currentRefresh);
    expect(runtimeApi.getWorkbenchPluginRuntimeIndex).toHaveBeenCalledTimes(2);

    const accepted = index('current');
    current.resolve(accepted);
    await currentStart;
    expect(resource.store.get().workbenchPluginRuntimeIndex).toBe(accepted);
    resource.dispose();
  });
});

describe('createWorkspaceStores — plugin-layout lifecycle integration', () => {
  it('lease deactivation disposes the runtime-index timer and rejects a late commit', async () => {
    const pending = deferred<WorkbenchPluginRuntimeIndex>();
    const fetchRuntimeIndex = vi
      .spyOn(api, 'getWorkbenchPluginRuntimeIndex')
      .mockReturnValueOnce(pending.promise);
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const stores = createWorkspaceStores('plugin-layout-lifecycle', api, PLUGINS_AVAILABLE);
    stores.lease.activate();

    const started = stores.pluginLayout.start();
    await Promise.resolve();
    expect(fetchRuntimeIndex).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(1);

    stores.lease.deactivate();
    const disposedSnapshot = stores.pluginLayout.store.get();
    expect(vi.getTimerCount()).toBe(0);

    pending.resolve(index('late-project-result'));
    await started;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(stores.pluginLayout.store.get()).toBe(disposedSnapshot);
    expect(fetchRuntimeIndex).toHaveBeenCalledTimes(1);
  });
});
