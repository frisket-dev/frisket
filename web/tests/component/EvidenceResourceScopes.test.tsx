// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ColumnEvidenceApiPort, EvidenceLinkViewerApiPort } from '../../src/api/ports';
import type { CellEvidenceLinkSummary, ColumnEvidenceBatchPayload, EvidenceViewerPayload } from '../../src/api/types';
import { useColumnEvidence } from '../../src/workbench/columnEvidenceResource';
import { useEvidenceLinkViewer } from '../../src/workbench/evidenceLinkViewerResource';

type EvidenceApi = ColumnEvidenceApiPort & EvidenceLinkViewerApiPort;
const ROW_IDS = ['3'];
let sequence = 0;
const ids = () => ({ sheet: String(++sequence), column: '2', link: sequence });
type Ids = ReturnType<typeof ids>;

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(finish => { resolve = finish; });
  return { promise, resolve };
}

function columnPayload(label: string): ColumnEvidenceBatchPayload {
  const link: CellEvidenceLinkSummary = {
    id: 1, stable_id: label, export_ref: label, status: 'active', role: 'support',
    evidence_kind: 'source', span_count: 1, artifact_count: 1, snippet: label, viewer_href: '/viewer',
  };
  return { schema_version: 'frisket.column_evidence_batch.v1', sheet_id: 1, column_id: 2,
    rows: [{ row_id: 3, links: [link] }] };
}

function viewerPayload(label: string): EvidenceViewerPayload {
  return {
    schema_version: 'frisket.evidence_viewer.v1', artifacts: [], warnings: [],
    link: {
      id: 1, stable_id: label, export_ref: label, subject_kind: 'cell', subject_ref: {},
      sheet_id: 1, row_id: 3, column_id: 2, run_id: null, op_id: null, receipt_id: null,
      role: 'support', status: 'active', confidence: null, pinned: false, producer: {},
      stale_reason: null, stale_at: null, created_at: '2026-10-20T00:00:00Z', text_layer_hash_mismatch: false,
    },
  };
}

function scopedApi(label: string) {
  return {
    getColumnEvidence: vi.fn<EvidenceApi['getColumnEvidence']>().mockResolvedValue(columnPayload(label)),
    getEvidenceViewer: vi.fn<EvidenceApi['getEvidenceViewer']>().mockResolvedValue(viewerPayload(label)),
  };
}

function useResources(api: EvidenceApi, key: Ids) {
  return {
    column: useColumnEvidence(key.sheet, key.column, ROW_IDS, api),
    viewer: useEvidenceLinkViewer(key.link, api),
  };
}

afterEach(cleanup);

describe('project-scoped evidence resources', () => {
  it('isolates two scoped APIs with identical numeric sheet, column, row and link IDs', async () => {
    const key = ids();
    const firstApi = scopedApi('first-project');
    const secondApi = scopedApi('second-project');
    const first = renderHook(() => useResources(firstApi, key));
    const second = renderHook(() => useResources(secondApi, key));
    await waitFor(() => {
      expect(first.result.current.column.byRow['3']?.[0].snippet).toBe('first-project');
      expect(second.result.current.column.byRow['3']?.[0].snippet).toBe('second-project');
      expect(first.result.current.viewer.payload?.link.stable_id).toBe('first-project');
      expect(second.result.current.viewer.payload?.link.stable_id).toBe('second-project');
    });
    expect(firstApi.getColumnEvidence).toHaveBeenCalledTimes(1);
    expect(secondApi.getColumnEvidence).toHaveBeenCalledTimes(1);
    expect(firstApi.getEvidenceViewer).toHaveBeenCalledTimes(1);
    expect(secondApi.getEvidenceViewer).toHaveBeenCalledTimes(1);
  });

  it('shares one request and stable snapshots within a scope, including remounts', async () => {
    const api = scopedApi('shared');
    const key = ids();
    const first = renderHook(() => useResources(api, key));
    const second = renderHook(() => useResources(api, key));
    await waitFor(() => expect(first.result.current.viewer.status).toBe('ready'));
    expect(first.result.current.column).toBe(second.result.current.column);
    expect(first.result.current.viewer).toBe(second.result.current.viewer);
    const settled = first.result.current;
    first.unmount();
    second.unmount();
    const remounted = renderHook(() => useResources(api, key));
    expect(remounted.result.current.column).toBe(settled.column);
    expect(remounted.result.current.viewer).toBe(settled.viewer);
    expect(api.getColumnEvidence).toHaveBeenCalledTimes(1);
    expect(api.getEvidenceViewer).toHaveBeenCalledTimes(1);
  });

  it('ignores the old project completion after switching APIs without changing numeric IDs', async () => {
    const oldColumn = deferred<ColumnEvidenceBatchPayload>();
    const oldViewer = deferred<EvidenceViewerPayload>();
    const oldApi = scopedApi('old');
    oldApi.getColumnEvidence.mockReturnValue(oldColumn.promise);
    oldApi.getEvidenceViewer.mockReturnValue(oldViewer.promise);
    const nextApi = scopedApi('next');
    const key = ids();
    const reader = renderHook(({ api }) => useResources(api, key), { initialProps: { api: oldApi } });
    reader.rerender({ api: nextApi });
    await waitFor(() => expect(reader.result.current.viewer.payload?.link.stable_id).toBe('next'));
    const selected = reader.result.current;
    await act(async () => {
      oldColumn.resolve(columnPayload('retired'));
      oldViewer.resolve(viewerPayload('retired'));
    });
    expect(reader.result.current.column).toBe(selected.column);
    expect(reader.result.current.viewer).toBe(selected.viewer);
    expect(reader.result.current.column.byRow['3']?.[0].snippet).toBe('next');
    expect(reader.result.current.viewer.payload?.link.stable_id).toBe('next');
  });

  it('cannot publish an evicted request into a replacement entry with the same key', async () => {
    const oldColumn = deferred<ColumnEvidenceBatchPayload>();
    const oldViewer = deferred<EvidenceViewerPayload>();
    const nextColumn = deferred<ColumnEvidenceBatchPayload>();
    const nextViewer = deferred<EvidenceViewerPayload>();
    const api = scopedApi('other');
    const key = ids();
    const repeatedColumn = vi.fn().mockReturnValueOnce(oldColumn.promise).mockReturnValueOnce(nextColumn.promise);
    const repeatedViewer = vi.fn().mockReturnValueOnce(oldViewer.promise).mockReturnValueOnce(nextViewer.promise);
    api.getColumnEvidence.mockImplementation(sheet => sheet === key.sheet ? repeatedColumn() : Promise.resolve(columnPayload('other')));
    api.getEvidenceViewer.mockImplementation(link => link === key.link ? repeatedViewer() : Promise.resolve(viewerPayload('other')));
    const reader = renderHook(({ key: current }) => useResources(api, current), { initialProps: { key } });
    // Both resources retain only a bounded number of inactive entries.
    for (let index = 0; index < 70; index++) reader.rerender({ key: ids() });
    reader.rerender({ key });
    expect(repeatedColumn).toHaveBeenCalledTimes(2);
    expect(repeatedViewer).toHaveBeenCalledTimes(2);
    await act(async () => {
      nextColumn.resolve(columnPayload('replacement'));
      nextViewer.resolve(viewerPayload('replacement'));
    });
    await act(async () => {
      oldColumn.resolve(columnPayload('evicted'));
      oldViewer.resolve(viewerPayload('evicted'));
    });
    expect(reader.result.current.column.byRow['3']?.[0].snippet).toBe('replacement');
    expect(reader.result.current.viewer.payload?.link.stable_id).toBe('replacement');
  });

  it('keeps settled empty snapshots stable without requesting empty inputs', () => {
    const api = scopedApi('unused');
    const reader = renderHook(() => ({
      column: useColumnEvidence('1', null, ROW_IDS, api), viewer: useEvidenceLinkViewer(null, api),
    }));
    const empty = reader.result.current;
    reader.rerender();
    expect(reader.result.current.column).toBe(empty.column);
    expect(reader.result.current.viewer).toBe(empty.viewer);
    expect(empty.column).toEqual({ status: 'ready', byRow: {} });
    expect(empty.viewer).toEqual({ status: 'ready', payload: null });
    expect(api.getColumnEvidence).not.toHaveBeenCalled();
    expect(api.getEvidenceViewer).not.toHaveBeenCalled();
  });

  it('retains every subscribed request when active readers exceed the inactive retention caps', async () => {
    const api = scopedApi('active');
    const readers = Array.from({ length: 66 }, () => {
      const key = ids();
      return renderHook(() => useResources(api, key));
    });
    await waitFor(() => {
      for (const reader of readers) {
        expect(reader.result.current.column.status).toBe('ready');
        expect(reader.result.current.viewer.status).toBe('ready');
      }
    });
    expect(api.getColumnEvidence).toHaveBeenCalledTimes(readers.length);
    expect(api.getEvidenceViewer).toHaveBeenCalledTimes(readers.length);
  });

  it('does not share a failed project read with another scoped API', async () => {
    const key = ids();
    const failedApi = scopedApi('failed');
    failedApi.getColumnEvidence.mockRejectedValue(new Error('denied'));
    failedApi.getEvidenceViewer.mockRejectedValue(new Error('denied'));
    const failed = renderHook(() => useResources(failedApi, key));
    await waitFor(() => expect(failed.result.current.viewer.status).toBe('error'));
    const successfulApi = scopedApi('successful');
    const successful = renderHook(() => useResources(successfulApi, key));
    await waitFor(() => expect(successful.result.current.viewer.status).toBe('ready'));
    expect(failed.result.current.column.status).toBe('error');
    expect(successful.result.current.column.byRow['3']?.[0].snippet).toBe('successful');
  });
});
