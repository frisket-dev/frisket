import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createGraphLineageApi } from '../../src/api/graphLineage';
import { ApiError, createProjectApi } from '../../src/api/real';

function stubFetch(body: unknown, init: { ok?: boolean; status?: number } = {}) {
  const fetchMock = vi.fn(() => Promise.resolve({
    ok: init.ok ?? true,
    status: init.status ?? 200,
    statusText: '',
    json: () => Promise.resolve(body),
  } as Response));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

let api = createProjectApi('p1');
beforeEach(() => { api = createProjectApi('p1'); });
afterEach(() => vi.unstubAllGlobals());

describe('graph and lineage generated transport', () => {
  it('returns each open response by identity, encodes pid once, and preserves graph omission/defaults', async () => {
    const lineage = { project_id: 'folder/child', future: { nested: [null, 3] } };
    const graph = { sheet_id: 7, diagnostics: [{ code: 'future', extra: null }] };
    const fetchMock = stubFetch(lineage);
    api = createProjectApi('folder/child');

    const result = await api.getLineage();
    expect(result).toBe(lineage);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/projects/folder%2Fchild/lineage',
      expect.objectContaining({ method: 'GET' }),
    );
    const lineageInit = fetchMock.mock.calls[0]![1] as RequestInit;
    expect(lineageInit.body).toBeUndefined();
    expect(new Headers(lineageInit.headers).has('Content-Type')).toBe(false);

    fetchMock.mockResolvedValueOnce({
      ok: true, status: 200, statusText: '', json: () => Promise.resolve(graph),
    } as Response);
    const graphResult = await api.getSheetGraph({
      sheetId: 7,
      direction: 'future-direction' as 'directed',
      nodeLabelColumnId: '11',
      nodeColorColumnId: '',
      nodeSizeColumnId: null,
      edgeLabelColumnId: 14,
    });
    expect(graphResult).toBe(graph);
    expect(fetchMock.mock.calls[1]![0]).toBe(
      '/api/projects/folder%2Fchild/sheets/7/graph?direction=future-direction&node_label_column_id=11&edge_label_column_id=14&limit_nodes=200&limit_edges=500',
    );
    const graphInit = fetchMock.mock.calls[1]![1] as RequestInit;
    expect(graphInit.body).toBeUndefined();
    expect(new Headers(graphInit.headers).has('Content-Type')).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('passes signal and headers through a bodyless GET and preserves nested/string/422/non-json errors', async () => {
    const fetchMock = stubFetch({ project_id: 'p1' });
    const controller = new AbortController();
    const transport = createGraphLineageApi((status, payload) => {
      const detail = payload && typeof payload === 'object' && 'detail' in payload
        ? (payload as { detail: unknown }).detail
        : payload;
      if (detail && typeof detail === 'object' && 'message' in detail) {
        const coded = detail as { message: string; code?: string; [key: string]: unknown };
        const { code, message, ...details } = coded;
        return new ApiError(status, message, typeof code === 'string' ? code : undefined, details);
      }
      return new ApiError(status, typeof detail === 'string' ? detail : JSON.stringify(detail));
    });
    await transport.getLineage('folder/child', {
      signal: controller.signal,
      headers: { 'X-Test': 'graph-lineage' },
    });
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/projects/folder%2Fchild/lineage',
      expect.objectContaining({ method: 'GET', signal: controller.signal }),
    );
    const headers = new Headers((fetchMock.mock.calls[0]![1] as RequestInit).headers);
    expect(headers.get('X-Test')).toBe('graph-lineage');
    expect(headers.has('Content-Type')).toBe(false);

    stubFetch(
      { detail: { code: 'sheet_not_found', message: 'no sheet', nested: { x: 1 } } },
      { ok: false, status: 404 },
    );
    await expect(api.getSheetGraph({ sheetId: 7 })).rejects.toMatchObject({
      status: 404, message: 'no sheet', code: 'sheet_not_found', details: { nested: { x: 1 } },
    });

    stubFetch({ detail: 'missing project' }, { ok: false, status: 404 });
    await expect(api.getLineage()).rejects.toMatchObject({ status: 404, message: 'missing project' });

    stubFetch({ detail: [{ type: 'int_parsing' }] }, { ok: false, status: 422 });
    await expect(api.getSheetGraph({ sheetId: 'not-a-number' })).rejects.toMatchObject({
      status: 422, message: '[{"type":"int_parsing"}]',
    });

    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve({
      ok: false, status: 502, statusText: 'Bad Gateway', json: () => Promise.reject(new SyntaxError('bad json')),
    } as Response)));
    await expect(api.getLineage()).rejects.toMatchObject({ status: 502, message: 'Bad Gateway' });

    const aborted = new DOMException('aborted', 'AbortError');
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(aborted)));
    await expect(api.getLineage()).rejects.toBe(aborted);
  });
});
