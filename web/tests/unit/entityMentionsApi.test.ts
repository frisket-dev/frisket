import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createEntityMentionsApi } from '../../src/api/entityMentions';

import { ApiError, createProjectApi } from '../../src/api/real';

let api = createProjectApi('test-project');

function stubFetch(body: unknown, init: { ok?: boolean; status?: number } = {}) {
  const fetchMock = vi.fn(() =>
    Promise.resolve({
      ok: init.ok ?? true,
      status: init.status ?? 200,
      statusText: '',
      json: () => Promise.resolve(body),
    } as Response),
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function requestBody(fetchMock: ReturnType<typeof vi.fn>, call = 0): Record<string, unknown> {
  const init = fetchMock.mock.calls[call]![1] as RequestInit;
  return JSON.parse(String(init.body)) as Record<string, unknown>;
}

beforeEach(() => api = createProjectApi('p1'));
afterEach(() => vi.unstubAllGlobals());

const PREVIEW_WIRE = {
  schema_version: 'entity-mentions-preview.v2',
  sheet_id: 7,
  column: { id: 11, name: 'entities', semantic_type: 'entity_mentions' },
  coverage: {
    target_rows: null,
    completed_rows: 9,
    failed_rows: 1,
    sheet_rows: 12,
  },
  search: 'Ada',
  type: 'person',
  total_groups: 1,
  type_totals: [
    { type: 'person', total_groups: 1 },
  ],
  limit: 100,
  offset: 0,
  items: [
    {
      type: 'person',
      selector: { kind: 'fingerprint', fingerprint: 'ada lovelace' },
      label: 'Ada Lovelace',
      row_count: 3,
      mention_count: 4,
      surface_count: 1,
      surfaces: [{ text: 'Ada Lovelace', row_count: 3, mention_count: 4 }],
    },
  ],
};

const DOCUMENTS_WIRE = {
  schema_version: 'entity-mention-detail.v1',
  sheet_id: 7,
  column: { id: 11, name: 'entities', semantic_type: 'entity_mentions' },
  type: 'person',
  selector: { kind: 'fingerprint', fingerprint: 'ada lovelace' },
  totals: { mentions: 4, documents: 2 },
  limit: 100,
  offset: 0,
  documents: [
    { row_id: 20, title: 'Hearing', occurrence_count: 3 },
  ],
  next_offset: 1,
};

const OCCURRENCES_WIRE = {
  schema_version: 'entity-mention-occurrences.v1',
  sheet_id: 7,
  row_id: 20,
  column: { id: 11, name: 'entities', semantic_type: 'entity_mentions' },
  type: 'person',
  selector: { kind: 'text', text: 'Ada' },
  text_column: { id: 4, name: 'body' },
  totals: { occurrences: 2 },
  occurrences: [
    {
      occurrence_id: 'occ-1',
      start: 3,
      end: 6,
      quote: 'Ada',
      snippet: {
        text: '🛰 Ada signed',
        mark_start: 3,
        mark_end: 6,
        truncated_start: true,
        truncated_end: false,
      },
    },
  ],
  next_offset: null,
  unpositioned: { reason: 'content_hash_mismatch', total: 2 },
};

describe('entity mention generated transport', () => {
  it('maps all three requests, responses, and project paths without double encoding', async () => {
    const fetchMock = stubFetch(PREVIEW_WIRE);
    api = createProjectApi('folder/child');

    const preview = await api.entityMentionsPreview({
      sheetId: '7',
      columnId: '11',
      search: 'Ada',
      type: 'person',
      limit: 100,
      offset: 0,
    });
    expect(fetchMock.mock.calls[0]![0]).toBe(
      '/api/projects/folder%2Fchild/entity-mentions/v1/preview',
    );
    expect(requestBody(fetchMock)).toEqual({
      sheet_id: 7,
      column_id: 11,
      search: 'Ada',
      type: 'person',
      limit: 100,
      offset: 0,
    });
    expect(preview).toEqual({
      sheetId: '7',
      column: { id: '11', name: 'entities', semanticType: 'entity_mentions' },
      coverage: {
        targetRows: null,
        completedRows: 9,
        failedRows: 1,
        sheetRows: 12,
      },
      search: 'Ada',
      type: 'person',
      totalGroups: 1,
      typeTotals: [{ type: 'person', totalGroups: 1 }],
      limit: 100,
      offset: 0,
      items: [{
        type: 'person',
        selector: { kind: 'fingerprint', fingerprint: 'ada lovelace' },
        label: 'Ada Lovelace',
        rowCount: 3,
        mentionCount: 4,
        surfaceCount: 1,
        surfaces: [{ text: 'Ada Lovelace', rowCount: 3, mentionCount: 4 }],
      }],
    });

    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: '',
      json: () => Promise.resolve(DOCUMENTS_WIRE),
    } as Response);
    const documents = await api.entityMentionDocuments({
      sheetId: '7',
      columnId: '11',
      type: 'person',
      fingerprint: 'ada lovelace',
      limit: 100,
      offset: 0,
    });
    expect(fetchMock.mock.calls[1]![0]).toBe(
      '/api/projects/folder%2Fchild/entity-mentions/v1/documents',
    );
    expect(requestBody(fetchMock, 1)).toEqual({
      sheet_id: 7,
      column_id: 11,
      type: 'person',
      fingerprint: 'ada lovelace',
      limit: 100,
      offset: 0,
    });
    expect(documents).toEqual({
      sheetId: '7',
      columnId: '11',
      type: 'person',
      selector: { kind: 'fingerprint', fingerprint: 'ada lovelace' },
      totals: { mentions: 4, documents: 2 },
      documents: [{ rowId: '20', title: 'Hearing', occurrenceCount: 3 }],
      nextOffset: 1,
    });

    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: '',
      json: () => Promise.resolve(OCCURRENCES_WIRE),
    } as Response);
    const occurrences = await api.entityMentionOccurrences({
      sheetId: '7',
      rowId: '20',
      columnId: '11',
      type: 'person',
      text: 'Ada',
      snippetRadius: 32,
    });
    expect(fetchMock.mock.calls[2]![0]).toBe(
      '/api/projects/folder%2Fchild/entity-mentions/v1/occurrences',
    );
    expect(requestBody(fetchMock, 2)).toEqual({
      sheet_id: 7,
      row_id: 20,
      column_id: 11,
      type: 'person',
      text: 'Ada',
      snippet_radius: 32,
    });
    expect(occurrences).toEqual({
      sheetId: '7',
      rowId: '20',
      columnId: '11',
      type: 'person',
      selector: { kind: 'text', text: 'Ada' },
      textColumn: { id: '4', name: 'body' },
      totals: { occurrences: 2 },
      occurrences: [{
        occurrenceId: 'occ-1',
        start: 3,
        end: 6,
        quote: 'Ada',
        snippet: {
          text: '🛰 Ada signed',
          markStart: 3,
          markEnd: 6,
          truncatedStart: true,
          truncatedEnd: false,
        },
      }],
      nextOffset: null,
      unpositioned: { reason: 'content_hash_mismatch', total: 2 },
    });
  });

  it('preserves omission and lets the service own both-or-neither identity errors', async () => {
    const fetchMock = stubFetch(PREVIEW_WIRE);

    await api.entityMentionsPreview({ sheetId: '7', columnId: '11', search: '  ', type: '' });
    expect(requestBody(fetchMock)).toEqual({ sheet_id: 7, column_id: 11 });

    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: '',
      json: () => Promise.resolve(DOCUMENTS_WIRE),
    } as Response);
    await api.entityMentionDocuments({
      sheetId: '7', columnId: '11', type: 'person', fingerprint: 'fp', text: 'Ada',
    });
    expect(requestBody(fetchMock, 1)).toEqual({
      sheet_id: 7, column_id: 11, type: 'person', fingerprint: 'fp', text: 'Ada',
    });

    fetchMock.mockResolvedValueOnce({
      ok: true,
      status: 200,
      statusText: '',
      json: () => Promise.resolve(OCCURRENCES_WIRE),
    } as Response);
    await api.entityMentionOccurrences({
      sheetId: '7', rowId: '20', columnId: '11', type: 'person',
    });
    expect(requestBody(fetchMock, 2)).toEqual({
      sheet_id: 7, row_id: 20, column_id: 11, type: 'person',
    });
  });

  it('forwards adapter options and preserves bare, ordinary, and network failures', async () => {
    const fetchMock = stubFetch(PREVIEW_WIRE);
    const controller = new AbortController();
    const transport = createEntityMentionsApi((status, payload) => {
      if (
        payload !== null
        && typeof payload === 'object'
        && 'message' in payload
        && 'code' in payload
      ) {
        const error = payload as { message: string; code: string; details?: Record<string, unknown> };
        return new ApiError(status, error.message, error.code, error.details);
      }
      return new ApiError(status, JSON.stringify(payload));
    });
    await transport.preview(
      'folder/child',
      { sheetId: '7', columnId: '11' },
      { signal: controller.signal, headers: { 'X-Test': 'entity' } },
    );
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/projects/folder%2Fchild/entity-mentions/v1/preview',
      expect.objectContaining({
        method: 'POST',
        signal: controller.signal,
        headers: expect.any(Headers),
      }),
    );
    const headers = new Headers((fetchMock.mock.calls[0]![1] as RequestInit).headers);
    expect(headers.get('X-Test')).toBe('entity');
    expect(headers.get('Content-Type')).toBe('application/json');

    stubFetch(
      {
        schema_version: 'frisket.action_error.v1',
        code: 'invalid_input_ref',
        message: 'no such column',
        details: { requested: 11 },
      },
      { ok: false, status: 400 },
    );
    const bare = api.entityMentionsPreview({ sheetId: '7', columnId: '11' });
    await expect(bare).rejects.toMatchObject({
      status: 400,
      message: 'no such column',
      code: 'invalid_input_ref',
      details: { requested: 11 },
    });

    stubFetch({ detail: 'gateway denied' }, { ok: false, status: 502 });
    await expect(api.entityMentionDocuments({
      sheetId: '7', columnId: '11', type: 'person', fingerprint: 'ada',
    })).rejects.toMatchObject({ status: 502, message: 'gateway denied' });

    const network = new Error('offline');
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(network)));
    await expect(api.entityMentionOccurrences({
      sheetId: '7', rowId: '20', columnId: '11', type: 'person', text: 'Ada',
    })).rejects.toBe(network);
  });
});
