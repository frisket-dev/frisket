// @vitest-environment jsdom

import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { useLoader, type LoaderOptions } from '../../src/hooks/useLoader';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

describe('useLoader', () => {
  it('loads, then settles data', async () => {
    const { result } = renderHook(() => useLoader(() => Promise.resolve('a'), []));
    expect(result.current).toMatchObject({ data: null, error: null, loading: true });
    await waitFor(() => expect(result.current).toMatchObject({ data: 'a', error: null, loading: false }));
  });

  it('reports the error message, or the fallback for a non-Error', async () => {
    const failing = renderHook(() => useLoader(() => Promise.reject(new Error('boom')), []));
    await waitFor(() => expect(failing.result.current).toMatchObject({ data: null, error: 'boom', loading: false }));
    const odd = renderHook(() => useLoader(() => Promise.reject(7), [], { errorFallback: 'Could not load.' }));
    await waitFor(() => expect(odd.result.current.error).toBe('Could not load.'));
  });

  it('reads a changed input as loading with no data in the same render', async () => {
    const loads = new Map([['a', deferred<string>()], ['b', deferred<string>()]]);
    const { result, rerender } = renderHook(({ id }) => useLoader(() => loads.get(id)!.promise, [id]), {
      initialProps: { id: 'a' },
    });
    await act(async () => { loads.get('a')!.resolve('A'); });
    expect(result.current.data).toBe('A');
    rerender({ id: 'b' });
    expect(result.current).toMatchObject({ data: null, loading: true });
    await act(async () => { loads.get('b')!.resolve('B'); });
    expect(result.current).toMatchObject({ data: 'B', loading: false });
  });

  it('keeps the previous data while a changed input loads when asked', async () => {
    const loads = new Map([['a', deferred<string>()], ['b', deferred<string>()]]);
    const options: LoaderOptions = { keepPreviousData: true };
    const { result, rerender } = renderHook(({ id }) => useLoader(() => loads.get(id)!.promise, [id], options), {
      initialProps: { id: 'a' },
    });
    await act(async () => { loads.get('a')!.resolve('A'); });
    rerender({ id: 'b' });
    expect(result.current).toMatchObject({ data: 'A', loading: true });
    await act(async () => { loads.get('b')!.reject(new Error('nope')); });
    expect(result.current).toMatchObject({ data: 'A', error: 'nope', loading: false });
  });

  it('drops a superseded response and aborts its signal', async () => {
    const loads = new Map([['a', deferred<string>()], ['b', deferred<string>()]]);
    const signals: AbortSignal[] = [];
    const { result, rerender } = renderHook(({ id }) => useLoader((signal) => {
      signals.push(signal);
      return loads.get(id)!.promise;
    }, [id]), { initialProps: { id: 'a' } });
    rerender({ id: 'b' });
    expect(signals[0].aborted).toBe(true);
    await act(async () => { loads.get('b')!.resolve('B'); });
    await act(async () => { loads.get('a')!.resolve('A'); });
    expect(result.current.data).toBe('B');
  });

  it('settles nothing after unmount', async () => {
    const load = deferred<string>();
    const signals: AbortSignal[] = [];
    const { result, unmount } = renderHook(() => useLoader((signal) => { signals.push(signal); return load.promise; }, []));
    unmount();
    expect(signals[0].aborted).toBe(true);
    await act(async () => { load.resolve('late'); });
    expect(result.current.data).toBeNull();
  });

  it('reload keeps data while loading and the last reload wins', async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const third = deferred<string>();
    const queue = [first, second, third];
    const load = vi.fn(() => queue.shift()!.promise);
    const { result } = renderHook(() => useLoader(load, []));
    await act(async () => { first.resolve('one'); });
    let slow!: Promise<void>;
    let fast!: Promise<void>;
    act(() => { slow = result.current.reload(); });
    expect(result.current).toMatchObject({ data: 'one', loading: true });
    act(() => { fast = result.current.reload(); });
    await act(async () => { third.resolve('three'); await fast; });
    await act(async () => { second.resolve('two'); await slow; });
    expect(result.current).toMatchObject({ data: 'three', loading: false });
  });

  it('reload clears a previous error while it loads', async () => {
    const outcomes = [deferred<string>(), deferred<string>()];
    const queue = [...outcomes];
    const { result } = renderHook(() => useLoader(() => queue.shift()!.promise, []));
    await act(async () => { outcomes[0].reject(new Error('down')); });
    expect(result.current).toMatchObject({ data: null, error: 'down', loading: false });
    act(() => { void result.current.reload(); });
    expect(result.current).toMatchObject({ error: null, loading: true });
    await act(async () => { outcomes[1].resolve('up'); });
    expect(result.current).toMatchObject({ data: 'up', error: null, loading: false });
  });

  it('does not fetch while disabled', () => {
    const load = vi.fn(() => Promise.resolve('x'));
    const { result } = renderHook(() => useLoader(load, [], { enabled: false }));
    expect(load).not.toHaveBeenCalled();
    expect(result.current).toMatchObject({ data: null, error: null, loading: false });
  });

  it('setData replaces the data locally', async () => {
    const { result } = renderHook(() => useLoader(() => Promise.resolve('server'), []));
    await waitFor(() => expect(result.current.data).toBe('server'));
    act(() => { result.current.setData('local'); });
    expect(result.current.data).toBe('local');
  });
});
