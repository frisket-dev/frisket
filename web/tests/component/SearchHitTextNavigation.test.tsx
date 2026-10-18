// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useEffect } from 'react';
import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WorkspaceStoresProvider } from '../../src/bind/WorkspaceStoresProvider';
import { useWorkspaceStores } from '../../src/bind/useWorkspaceStores';
import { useSelector } from '../../src/bind/useSelector';
import { useWorkspaceModel } from '../../src/workspace/useWorkspaceModel';
import { useRoute } from '../../src/routes';
import { RowDrawerBody } from '../../src/components/RowDrawer';
import { consumeGridCellReveal, getPendingGridCellReveal } from '../../src/grid/gridCellReveal';
import type { WorkspaceStores } from '../../src/state/createWorkspaceStores';
import type { ProjectSearchHitWire } from '../../src/api/projectSearch';

const project = { id: 'search-navigation', name: 'Search navigation' };
const total = 20_971_520;
const tail = 20_971_506;
let latest: ReturnType<typeof useWorkspaceModel>;
let stores: WorkspaceStores;
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { 'Content-Type': 'application/json' },
});
function Probe() {
  const route = useRoute();
  const active = route.kind === 'project' ? route : undefined;
  const model = useWorkspaceModel({ project, routeSheetId: active?.sheetId, routePanel: active?.panel });
  const workspace = useWorkspaceStores();
  const row = useSelector(workspace.detail.store, (state) => state.rowDrawer);
  const columnId = useSelector(workspace.selection.store, (state) => state.selectedColumnId);
  useEffect(() => { latest = model; stores = workspace; });
  const sheet = model.mainViewModel.sheet;
  return row && sheet ? <RowDrawerBody projectId={project.id} sheet={sheet} row={row}
    selectedColumnId={columnId} onEdit={async () => {}} expandedRunResultFields={new Set()}
    onToggleRunResultField={() => {}} /> : null;
}
function hit(start = tail): ProjectSearchHitWire {
  const text = { text_surface_id: 'cell:20:7', content_hash: 'hash', start, end: start + 6 };
  return {
    source: { source_id: 'cell:20:7', source_kind: 'text', version_kind: 'retrieval_revision', version_value: 9, content_hash: 'hash' },
    part: { part_id: 'tail', part_kind: 'text_chunk', producer_recipe_id: 'text-v1' },
    binding: { binding_id: 'binding:20:7', sheet_id: 2, row_id: 20, column_id: 7,
      dependencies: [{ row_id: 20, column_id: 7, retrieval_revision: 9 }] },
    location: { kind: 'text', precision: 'exact', text, pages: null, time: null, source_span_ids: [] },
    column_name: 'body', score: 1, rerank_score: null, contributions: [], snippet: 'needle',
    ai_generated: false, additional_binding_count: 0,
    navigation: { kind: 'cell', sheet_id: 2, row_id: 20, column_id: 7, text_range: text },
  };
}
const windowRequests: Array<{ start: number; revision: string | null; signal?: AbortSignal | null }> = [];
let interceptWindow: ((start: number, signal?: AbortSignal | null) => Promise<Response> | undefined) | undefined;
let columnType = 'text';
beforeEach(() => {
  window.history.replaceState(null, '', '/p/search-navigation/s/1');
  columnType = 'text';
  interceptWindow = undefined;
  windowRequests.length = 0;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), 'http://localhost');
    const column = { id: 7, name: 'body', type: columnType, ai_generated: false, format: null,
      current_run_id: null, transcript_status: null, media_download_candidate: null };
    if (url.pathname.endsWith('/sheets')) return json([1, 2].map((id) => ({ id, name: `Sheet ${id}`, rows: 1,
      parent_sheet_id: null, parent_op_id: null, title_column_id: null, cited_column_ids: [],
      annotated_text_column_ids: [], document_text_column_ids: [], dependent_sheet_ids: [], columns: [column] })));
    if (url.pathname.endsWith('/locate')) return json({ found: true, row_id: 20, index: 0, page_offset: 0, page_size: 100 });
    if (url.pathname.endsWith('/data')) return json({ total: 1, columns: [column], rows: [{ id: 20, cells: {}, meta: {
      '7': { current_value_ref: { kind: 'source_cell', row_id: 20, column_id: 7, run_id: null, op_id: null }, bounded_value: { preview: 'preview only', value_kind: 'text', total_characters: total, total_bytes: total + 3,
        retrieval_revision: 9, truncated: true, manual_editable: false, manual_edit_byte_limit: 65536 } },
    }, parent_row_id: null, child_count: 0 }] });
    if (url.pathname.endsWith('/text-window')) {
      const start = Number(url.searchParams.get('start'));
      windowRequests.push({ start, revision: url.searchParams.get('revision'), signal: init?.signal });
      const intercepted = interceptWindow?.(start, init?.signal);
      if (intercepted) return intercepted;
      const text = `${'x'.repeat(255)}😀needle tail`;
      return json({ retrieval_revision: 9, total_characters: total, total_bytes: total + 3,
        range: { start, end: start + Array.from(text).length }, text,
        previous_start: Math.max(0, start - 16384), next_start: null });
    }
    return json({});
  }));
});
afterEach(() => {
  cleanup();
  const pending = getPendingGridCellReveal();
  if (pending) consumeGridCellReveal(pending.requestId);
  vi.unstubAllGlobals();
});
async function ready() {
  render(<WorkspaceStoresProvider projectId={project.id}><Probe /></WorkspaceStoresProvider>);
  await waitFor(() => expect(latest?.sheetsLoaded).toBe(true));
  await waitFor(() => expect(stores.route.store.get().sheetId).toBe('1'));
}

describe('located text search navigation', () => {
  it('opens a cross-sheet large text hit in the existing Detail reader at its revision-pinned passage', async () => {
    await ready();
    act(() => latest.overlayModel.navigateToSearchHit(hit()));
    expect(stores.route.store.get()).toMatchObject({ sheetId: '2', panel: { kind: 'row', rowId: '20', columnId: '7' } });
    expect(await screen.findByTestId('bounded-text-match')).toHaveTextContent(/^needle$/);
    expect(stores.route.store.get()).toMatchObject({ sheetId: '2', panel: { kind: 'row', rowId: '20', columnId: '7' } });
    expect(stores.detail.store.get().rowTextTarget).toMatchObject({ sheetId: '2', rowId: '20', columnId: '7', retrievalRevision: 9 });
    expect(windowRequests).toHaveLength(1);
    expect(windowRequests[0]).toMatchObject({ start: tail - 256, revision: '9' });
    expect(screen.getByTestId('bounded-text-window').textContent!.length).toBeLessThan(16384);
  });

  it('aborts a replaced hit and cannot publish its older passage', async () => {
    await ready();
    let resolveOld!: (response: Response) => void;
    interceptWindow = (start) => start === tail - 256
      ? new Promise((resolve) => { resolveOld = resolve; }) : undefined;
    act(() => latest.overlayModel.navigateToSearchHit(hit()));
    await waitFor(() => expect(windowRequests).toHaveLength(1));
    act(() => latest.overlayModel.navigateToSearchHit(hit(50_000)));
    expect(await screen.findByTestId('bounded-text-match')).toHaveTextContent(/^needle$/);
    expect(windowRequests[0].signal?.aborted).toBe(true);
    await act(async () => { resolveOld(json({ retrieval_revision: 9, total_characters: total, total_bytes: total,
      range: { start: tail - 256, end: tail }, text: 'obsolete passage', previous_start: 0, next_start: null })); });
    expect(screen.queryByText('obsolete passage')).not.toBeInTheDocument();
    expect(windowRequests[1]).toMatchObject({ start: 49_744, revision: '9' });
  });

  it.each(['json', 'missing revision'])('keeps ordinary cell navigation for %s without guessing text offsets', async (mode) => {
    if (mode === 'json') columnType = 'json';
    await ready();
    const target = hit();
    if (mode === 'missing revision') target.binding.dependencies = [];
    act(() => latest.overlayModel.navigateToSearchHit(target));
    expect(getPendingGridCellReveal()).toMatchObject({ sheetId: '2', rowId: '20', columnId: '7' });
    expect(stores.detail.store.get().rowTextTarget).toBeNull();
    expect(windowRequests).toHaveLength(0);
  });

  it('retains document page and audio time navigation', async () => {
    await ready();
    const target = hit();
    target.navigation = { kind: 'document', sheet_id: 2, row_id: 20, column_id: 7, page: 4, time_seconds: 12.5 };
    act(() => latest.overlayModel.navigateToSearchHit(target));
    expect(stores.chrome.store.get().documentView).toMatchObject({ sheetId: '2', sourceColumnId: '7', activeRowId: '20',
      readerPage: { rowId: '20', page: 4 }, readerTime: { rowId: '20', seconds: 12.5 } });
    expect(windowRequests).toHaveLength(0);
  });
});
