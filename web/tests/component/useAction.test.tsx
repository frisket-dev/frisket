// @vitest-environment jsdom

import { act, renderHook } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { useAction } from '../../src/hooks/useAction';
import { errorMessage } from '../../src/hooks/errorMessage';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

describe('errorMessage', () => {
  it('prefers the Error message, then the fallback, then the stringified value', () => {
    expect(errorMessage(new Error('boom'), 'fallback')).toBe('boom');
    expect(errorMessage(new Error(''), 'fallback')).toBe('');
    expect(errorMessage('raw', 'fallback')).toBe('fallback');
    expect(errorMessage('raw')).toBe('raw');
  });
});

describe('useAction', () => {
  it('owns busy and returns the value', async () => {
    const task = deferred<number>();
    const { result } = renderHook(() => useAction());
    let outcome!: Promise<number | undefined>;
    act(() => { outcome = result.current.run(() => task.promise); });
    expect(result.current.busy).toBe(true);
    await act(async () => { task.resolve(3); });
    await expect(outcome).resolves.toBe(3);
    expect(result.current).toMatchObject({ busy: false, error: null });
  });

  it('captures a failure and clears it on the next run', async () => {
    const { result } = renderHook(() => useAction({ errorFallback: 'Could not save.' }));
    await act(async () => { await result.current.run(() => Promise.reject(new Error('nope'))); });
    expect(result.current).toMatchObject({ busy: false, error: 'nope' });
    await act(async () => { await result.current.run(() => Promise.reject(42)); });
    expect(result.current.error).toBe('Could not save.');
    const next = deferred<void>();
    act(() => { void result.current.run(() => next.promise); });
    expect(result.current.error).toBeNull();
    await act(async () => { next.resolve(); });
  });

  it('lets only the latest run settle busy and error', async () => {
    const slow = deferred<void>();
    const fast = deferred<void>();
    const { result } = renderHook(() => useAction());
    act(() => { void result.current.run(() => slow.promise); });
    act(() => { void result.current.run(() => fast.promise); });
    await act(async () => { slow.reject(new Error('stale')); });
    expect(result.current).toMatchObject({ busy: true, error: null });
    await act(async () => { fast.resolve(); });
    expect(result.current).toMatchObject({ busy: false, error: null });
  });
});
