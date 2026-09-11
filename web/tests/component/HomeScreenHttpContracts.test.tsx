import { describe, expect, it, vi } from 'vitest';

import { fetchProjectSheetCounts } from '../../src/api/homeSheetCounts';

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const SHEETS = [
  {
    id: 1,
    name: 'First',
    rows: 2,
    title_column_id: null,
    cited_column_ids: [],
    annotated_text_column_ids: [],
    parent_sheet_id: null,
    op_label: null,
    op_kind: null,
    syncState: null,
    stale_reason: null,
    materialized_kind: null,
    columns: [],
  },
  {
    id: 2,
    name: 'Second',
    rows: 3,
    title_column_id: null,
    cited_column_ids: [],
    annotated_text_column_ids: [],
    parent_sheet_id: null,
    op_label: null,
    op_kind: null,
    syncState: null,
    stale_reason: null,
    materialized_kind: null,
    columns: [],
  },
];

describe('Home sheet counts generated HTTP contract', () => {
  it('uses tenant.list_sheets.get once with generated pid encoding and maps counts', async () => {
    const fetch = vi.fn(async () => jsonResponse(SHEETS));
    vi.stubGlobal('fetch', fetch);

    await expect(fetchProjectSheetCounts('project id%')).resolves.toEqual({ sheets: 2, rows: 5 });

    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0]?.[0]).toBe('/api/projects/project%20id%25/sheets');
    expect(fetch.mock.calls[0]?.[1]).toEqual({ method: 'GET' });
    vi.unstubAllGlobals();
  });

  it('keeps a non-OK sheet response as zero counts', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ detail: 'denied' }, 403)));

    await expect(fetchProjectSheetCounts('project')).resolves.toEqual({ sheets: 0, rows: 0 });
    vi.unstubAllGlobals();
  });

  it('keeps a network failure rejected for the caller effect to handle', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('network unavailable');
    }));

    await expect(fetchProjectSheetCounts('project')).rejects.toThrow('network unavailable');
    vi.unstubAllGlobals();
  });
});
