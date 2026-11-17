import { afterEach, expect, it, vi } from 'vitest';
import { createViewsLensesApi } from '../../src/api/viewsLenses';
import type { SavedViewCreateInput } from '../../src/api/types';
const definition: SavedViewCreateInput = { name: 'Top contracts', selection: { scope: { kind: 'sheet', sheet_id: 3, row_ids: [7, 8] }, filters: [{ column_id: 2, condition: { gte: 10 } }], order: [{ column_id: 2, dir: 'desc' }], limit: 20 }, presentation: { columns: [2] } };
const view = { id: 4, name: definition.name, sheet_id: 3, spec: { selection: definition.selection, presentation: definition.presentation }, op_id: 11, created_at: '2026-11-15', updated_at: '2026-11-15', unavailable_reason: null };
function mockFetch(...payloads: unknown[]) {
  const fetch = vi.fn();
  for (const payload of payloads) fetch.mockResolvedValueOnce(new Response(JSON.stringify(payload), { headers: { 'Content-Type': 'application/json' } }));
  vi.stubGlobal('fetch', fetch); return fetch;
}
afterEach(() => vi.unstubAllGlobals());
it('saves one complete stable-ID definition, and reads the same View for semantic and ordinary selections', async () => {
  const fetch = mockFetch(view, [view]);
  const api = createViewsLensesApi('p');
  expect(await api.saveView(definition)).toMatchObject(view);
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual(definition);
  expect(await api.listViews('3')).toEqual([view]);
  expect(String(fetch.mock.calls[1][0])).toBe('/api/projects/p/views?sheet_id=3');
});
it('renames separately from definition replacement, and deletes through the ordinary View route', async () => {
  const fetch = mockFetch(view, view, { ok: true, deleted: 4 });
  const api = createViewsLensesApi('p');
  await api.renameView(4, { name: 'New name' });
  await api.replaceViewDefinition(4, { selection: definition.selection, presentation: {} });
  await api.deleteView(4);
  expect(fetch.mock.calls.map((call) => call[1].method)).toEqual(['PATCH', 'PUT', 'DELETE']);
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ name: 'New name' });
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ selection: definition.selection, presentation: {} });
});
it('pages project refs without inventing a single sheet or discarding completeness metadata', async () => {
  const result = { view_id: 4, query_hash: 'q', refs: [{ sheet_id: 3, row_id: 7, search_score: 0.3, similarity_score: null, relevance_score: null }], total: 1001, offset: 25, limit: 25 };
  const fetch = mockFetch(result); const api = createViewsLensesApi('p');
  expect(await api.resolveView(4, { offset: 25, limit: 25 })).toEqual(result);
  expect(String(fetch.mock.calls[0][0])).toBe('/api/projects/p/views/4/resolve?limit=25&offset=25');
});
it('keeps an unavailable migrated definition unavailable instead of fabricating an all-rows selection', async () => {
  mockFetch([{ ...view, spec: {}, unavailable_reason: 'Missing source column' }]);
  expect(await createViewsLensesApi('p').listViews()).toEqual([{ ...view, spec: null, unavailable_reason: 'Missing source column' }]);
});
it('does not retry a failed read', async () => {
  const fetch = vi.fn().mockRejectedValue(new Error('Offline'));vi.stubGlobal('fetch', fetch);
  await expect(createViewsLensesApi('p').listViews()).rejects.toThrow('Offline');
  expect(fetch).toHaveBeenCalledTimes(1);
});
