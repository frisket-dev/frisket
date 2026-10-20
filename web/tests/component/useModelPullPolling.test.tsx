// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { ModelPullDto } from '../../src/api/types';
import { useModelPullPolling } from '../../src/hooks/useModelPullPolling';

const pull: ModelPullDto = {
  id: 1, model: 'test', display_name: 'Test', operation_kind: 'local_model',
  capabilities: { cancel: true, retry: false, remove: false },
  status: 'running', phase: 'downloading', total_bytes: null, completed_bytes: null,
  error: null, resolved_digest: null, resolved_size: null, created_at: '',
  started_at: null, finished_at: null, cancel_requested: false,
};
afterEach(() => { cleanup(); vi.useRealTimers(); });

it('retires old scoped requests even when their transport ignores cancellation', async () => {
  vi.useFakeTimers();
  let resolve!: (value: ModelPullDto) => void;
  const fetchPull = vi.fn<(id: number, signal: AbortSignal) => Promise<ModelPullDto>>(() =>
    new Promise<ModelPullDto>((done) => { resolve = done; }));
  const onProgress = vi.fn();
  const view = renderHook(({ scopeKey }) => useModelPullPolling({
    id: 1, scopeKey, active: true, fetchPull, onProgress,
  }), { initialProps: { scopeKey: 'workspace' } });
  await act(async () => vi.advanceTimersByTime(1000));
  const oldSignal = fetchPull.mock.calls[0][1];
  view.rerender({ scopeKey: 'organization' });
  expect(oldSignal.aborted).toBe(true);
  await act(async () => resolve({ ...pull, status: 'done' }));
  expect(onProgress).not.toHaveBeenCalled();
  await act(async () => vi.advanceTimersByTime(1000));
  await act(async () => resolve(pull));
  expect(onProgress).toHaveBeenCalledWith(pull, false);
});

it('reports terminal completion once and observes a replacement identity', async () => {
  vi.useFakeTimers();
  const fetchPull = vi.fn(async (id: number) => ({ ...pull, id, status: 'done' as const }));
  const onProgress = vi.fn();
  const view = renderHook(({ id }) => useModelPullPolling({
    id, scopeKey: 'project', active: true, fetchPull, onProgress,
  }), { initialProps: { id: 1 } });
  await act(async () => vi.advanceTimersByTime(1000));
  await act(async () => vi.advanceTimersByTime(5000));
  expect(onProgress).toHaveBeenCalledTimes(1);
  view.rerender({ id: 2 });
  await act(async () => vi.advanceTimersByTime(1000));
  expect(onProgress).toHaveBeenCalledTimes(2);
  expect(onProgress).toHaveBeenLastCalledWith(expect.objectContaining({ id: 2 }), true);
});

it('backs off transient failures and resets after a successful progress read', async () => {
  vi.useFakeTimers();
  const fetchPull = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(pull);
  const onError = vi.fn();
  renderHook(() => useModelPullPolling({
    id: 1, scopeKey: 'selector', active: true, fetchPull,
    onProgress: vi.fn(), onError, intervalMs: 2000, maxIntervalMs: 16000,
    mode: 'settle-relative',
  }));
  await act(async () => vi.advanceTimersByTime(2000));
  expect(onError).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTime(3999));
  expect(fetchPull).toHaveBeenCalledTimes(1);
  await act(async () => vi.advanceTimersByTime(1));
  expect(fetchPull).toHaveBeenCalledTimes(2);
  await act(async () => vi.advanceTimersByTime(2000));
  expect(fetchPull).toHaveBeenCalledTimes(3);
});
