// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { FormEvent } from 'react';
import type { SourceHealth, SourceInfo } from '../../src/api/open';
import type { SourceApiPort } from '../../src/api/ports';
import { useSourceHealth } from '../../src/components/sources/useSourceHealth';
import { useSourcesPanelState } from '../../src/components/sources/useSourcesPanelState';

afterEach(cleanup);
const source = (id = 7): SourceInfo => ({ id, name: `Source ${id}`, kind: 'rss', url: 'https://example.test/feed',
  config: {}, schedule: '@hourly', enabled: true, sheetId: 2, lastCheckedAt: null, lastStatus: 'ok', newRowsTotal: 3 });
const health = (id = 7): SourceHealth => ({ schemaVersion: 'frisket.source_health.v1',
  source: { ...source(id), createdAt: null, redactions: [] },
  summary: { status: 'healthy', lastSuccessAt: null, lastFailureAt: null, consecutiveFailures: 0,
    newRowsTotal: 3, newRowsRecent: 0, changedRowsRecent: 0, skippedRowsRecent: 0, revisionsRecent: 0, recentRunCount: 0, lastCursorSummary: '' },
  runsPage: { schemaVersion: 'frisket.source_runs_page.v1', order: 'desc', offset: 0, limit: 20, total: 0, hasMore: false, nextOffset: null },
  runs: [], downstreamJobs: [], costs: { recentActualMicro: 0, recentEstimatedMicro: 0, basis: 'runs' }, alerts: [], warnings: [] });

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function api() {
  return {
    listSources: vi.fn().mockResolvedValue([source(), source(8)]),
    getSource: vi.fn().mockResolvedValue({ ...source(), runs: [], runsPage: health().runsPage }),
    getSourceHealth: vi.fn().mockImplementation(async (id: number) => health(id)),
    createSource: vi.fn().mockResolvedValue(source()), updateSource: vi.fn().mockResolvedValue(source()),
    deleteSource: vi.fn().mockResolvedValue(undefined), fetchSource: vi.fn().mockResolvedValue({ sheetId: 2 }),
  } satisfies SourceApiPort;
}
const submitEvent = { preventDefault: vi.fn() } as unknown as FormEvent<HTMLFormElement>;

describe('source health request ownership', () => {
  it.each(['success', 'failure'])('does not let late source A %s alter source B loading/error', async (outcome) => {
    const port = api();
    const a = deferred<SourceHealth>(), b = deferred<SourceHealth>();
    port.getSourceHealth.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise);
    const { result } = renderHook(() => useSourcesPanelState(port));
    act(() => { void result.current.actions.openHealth(7); });
    act(() => { void result.current.actions.openHealth(8); });
    expect(result.current.state.healthOpenId).toBe(8);
    await act(async () => { if (outcome === 'success') a.resolve(health()); else a.reject(new Error('A failed')); });
    expect(result.current.state.healthLoading).toBe(true);
    expect(result.current.state.healthError).toBeNull();
    await act(async () => { b.resolve(health(8)); });
    expect(result.current.state.healthLoading).toBe(false);
    expect(result.current.state.health[8]?.source.id).toBe(8);
    expect(result.current.state.healthError).toBeNull();
  });

  it('accepts only the latest read for a source and retires a replaced API scope', async () => {
    const first = api(), second = api();
    const old = deferred<SourceHealth>(), stale = deferred<SourceHealth>();
    first.getSourceHealth.mockReturnValueOnce(old.promise).mockReturnValueOnce(stale.promise);
    const { result, rerender } = renderHook(({ port }) => useSourceHealth(port), { initialProps: { port: first } });
    const retiredLoad = result.current.load;
    act(() => { void result.current.load(7); void result.current.load(7); });
    await act(async () => { old.resolve(health()); });
    expect(result.current.entries[7]?.loading).toBe(true);
    rerender({ port: second });
    expect(result.current.entries[7]).toBeUndefined();
    await act(async () => { await retiredLoad(7); });
    expect(first.getSourceHealth).toHaveBeenCalledTimes(2);
    await act(async () => { await result.current.load(7); });
    await act(async () => { stale.reject(new Error('retired scope')); });
    expect(result.current.entries[7]?.error).toBeNull();
    expect(result.current.entries[7]?.health?.source.id).toBe(7);
  });

  it('does not navigate or refresh retired source health when a poll completes after API replacement', async () => {
    const first = api(), second = api(), pending = deferred<{ sheetId: number | null }>();
    first.fetchSource.mockReturnValueOnce(pending.promise);
    const { result, rerender } = renderHook(({ port }) => useSourceHealth(port), { initialProps: { port: first } });
    const onPolled = vi.fn(), afterPoll = vi.fn(async () => {});
    act(() => { void result.current.poll(7, { onPolled, afterPoll }); });
    rerender({ port: second });
    await act(async () => { pending.resolve({ sheetId: 2 }); });
    expect(onPolled).not.toHaveBeenCalled();
    expect(afterPoll).not.toHaveBeenCalled();
    expect(first.getSourceHealth).not.toHaveBeenCalled();
    expect(second.getSourceHealth).not.toHaveBeenCalled();
    expect(result.current.entries[7]).toBeUndefined();
  });

  it('reloads failed poll health/history, retains its error, and suppresses duplicate polls', async () => {
    const port = api(), pending = deferred<{ sheetId: number | null }>();
    port.fetchSource.mockReturnValueOnce(pending.promise);
    const { result } = renderHook(() => useSourceHealth(port));
    const afterPoll = vi.fn(async () => {}), onPolled = vi.fn();
    act(() => { void result.current.poll(7, { afterPoll, onPolled }); void result.current.poll(7, { afterPoll, onPolled }); });
    expect(port.fetchSource).toHaveBeenCalledOnce();
    await act(async () => { pending.reject(new Error('Offline')); });
    expect(result.current.entries[7]).toMatchObject({ busy: false, loading: false, error: 'Poll failed: Offline' });
    expect(port.getSourceHealth).toHaveBeenCalledWith(7, 0, 20);
    expect(afterPoll).toHaveBeenCalledOnce();
    expect(onPolled).not.toHaveBeenCalled();
  });

  it.each(['schedule', 'enabled'])('refreshes list/open health after a successful %s patch only', async (field) => {
    const port = api();
    const { result } = renderHook(() => useSourcesPanelState(port));
    await waitFor(() => expect(result.current.state.items).not.toBeNull());
    await act(async () => { await result.current.actions.openHealth(7); });
    const patch = () => field === 'schedule' ? result.current.actions.changeSchedule(7, '') : result.current.actions.toggleEnabled(7, false);
    port.listSources.mockClear(); port.getSourceHealth.mockClear();
    await act(async () => { await patch(); });
    expect(port.updateSource).toHaveBeenLastCalledWith(7, field === 'schedule' ? { schedule: null } : { enabled: false });
    expect(port.listSources).toHaveBeenCalledOnce();
    expect(port.getSourceHealth).toHaveBeenCalledExactlyOnceWith(7, 0, 20);
    port.listSources.mockClear(); port.getSourceHealth.mockClear();
    port.updateSource.mockRejectedValueOnce(new Error('Cannot patch'));
    await act(async () => { await patch(); });
    expect(result.current.state.error).toBe('Cannot patch');
    expect(port.listSources).not.toHaveBeenCalled();
    expect(port.getSourceHealth).not.toHaveBeenCalled();
  });

  it('retires an old GET error after a successful patch and silent health refresh', async () => {
    const port = api();
    port.getSourceHealth.mockRejectedValueOnce(new Error('Health read unavailable'));
    const { result } = renderHook(() => useSourcesPanelState(port));
    await act(async () => { await result.current.actions.openHealth(7); });
    expect(result.current.state.healthError).toBe('Health read unavailable');
    await act(async () => { await result.current.actions.toggleEnabled(7, false); });
    expect(result.current.state.health[7]?.source.id).toBe(7);
    expect(result.current.state.healthError).toBeNull();
  });
});

describe('source editor draft-owned completion', () => {
  it.each(['success', 'failure'])('preserves draft B when save A completes with %s', async (outcome) => {
    const port = api(), pending = deferred<SourceInfo>();
    port.updateSource.mockReturnValueOnce(pending.promise);
    const { result } = renderHook(() => useSourcesPanelState(port));
    act(() => { result.current.actions.startEdit(source()); });
    act(() => { void result.current.actions.submit(submitEvent); });
    act(() => { result.current.actions.setAdding(false); result.current.actions.startEdit(source(8)); result.current.actions.updateForm({ name: 'Draft B' }); });
    await act(async () => { if (outcome === 'success') pending.resolve(source()); else pending.reject(new Error('A failed')); });
    expect(result.current.state.form).toMatchObject({ editingId: 8, name: 'Draft B' });
    expect(result.current.state.error).toBeNull();
  });

  it('preserves typing during save and only clears the submitted revision', async () => {
    const port = api(), pending = deferred<SourceInfo>();
    port.updateSource.mockReturnValueOnce(pending.promise);
    const { result } = renderHook(() => useSourcesPanelState(port));
    act(() => { result.current.actions.startEdit(source()); });
    act(() => { void result.current.actions.submit(submitEvent); });
    act(() => { result.current.actions.updateForm({ name: 'New typing' }); });
    await act(async () => { pending.resolve(source()); });
    expect(result.current.state.form.name).toBe('New typing');
    await act(async () => { await result.current.actions.submit(submitEvent); });
    expect(result.current.state.form.editingId).toBeNull();
    expect(port.updateSource).toHaveBeenLastCalledWith(7, expect.objectContaining({ name: 'New typing' }));
  });

  it('does not let save A clear pending save B or B’s error', async () => {
    const port = api(), a = deferred<SourceInfo>(), b = deferred<SourceInfo>();
    port.updateSource.mockReturnValueOnce(a.promise).mockReturnValueOnce(b.promise);
    const { result } = renderHook(() => useSourcesPanelState(port));
    act(() => { result.current.actions.startEdit(source()); });
    act(() => { void result.current.actions.submit(submitEvent); });
    act(() => { result.current.actions.startEdit(source(8)); });
    act(() => { void result.current.actions.submit(submitEvent); });
    await act(async () => { a.resolve(source()); });
    expect(result.current.state.busy).toBe(8);
    expect(result.current.state.form.editingId).toBe(8);
    await act(async () => { b.reject(new Error('B failed')); });
    expect(result.current.state.error).toBe('B failed');
    expect(result.current.state.form.editingId).toBe(8);
    expect(result.current.state.busy).toBeNull();
  });
});
