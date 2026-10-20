// @vitest-environment jsdom

import { memo, useEffect } from 'react';
import { act, cleanup, waitFor, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useWorkspaceModel } from '../../src/workspace/useWorkspaceModel';
import { useWorkspaceStores } from '../../src/bind/useWorkspaceStores';
import { WorkspaceStoresProvider } from '../../src/bind/WorkspaceStoresProvider';
import type { ProjectInfo, RunActionLaunchResult } from '../../src/api/open';
import type { WorkspaceStores } from '../../src/state/createWorkspaceStores';


const PROJECT: ProjectInfo = { id: 'p1', name: 'Lens generation test project' };

type Model = ReturnType<typeof useWorkspaceModel>;
let latestModel: Model | null = null;
let latestStores: WorkspaceStores | null = null;
const mainRegionCommit = vi.fn();
const overlayRegionCommit = vi.fn();
const MainRegion = memo(function MainRegion({ value }: { value: Model['mainViewModel'] }) {
  useEffect(() => { mainRegionCommit(value); });
  return null;
});
const OverlayRegion = memo(function OverlayRegion({ value }: { value: Model['overlayModel'] }) {
  useEffect(() => { overlayRegionCommit(value); });
  return null;
});

function Probe() {
  const currentModel = useWorkspaceModel({ project: PROJECT });
  const currentStores = useWorkspaceStores();
  useEffect(() => {
    latestModel = currentModel;
    latestStores = currentStores;
  });
  return <><MainRegion value={currentModel.mainViewModel} /><OverlayRegion value={currentModel.overlayModel} /></>;
}

function mountWorkspaceModel() {
  render(
    <WorkspaceStoresProvider projectId={PROJECT.id}>
      <Probe />
    </WorkspaceStoresProvider>,
  );
}

function model(): Model {
  if (!latestModel) throw new Error('useWorkspaceModel has not rendered yet');
  return latestModel;
}

function stores(): WorkspaceStores {
  if (!latestStores) throw new Error('workspace stores have not rendered yet');
  return latestStores;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

const SHEET_LIST_WIRE = [1, 2].map((id) => ({
  id,
  name: `Sheet ${id}`,
  parent_sheet_id: null,
  parent_op_id: null,
  rows: 2,
  title_column_id: null,
  cited_column_ids: [],
  annotated_text_column_ids: [],
  document_text_column_ids: [],
  dependent_sheet_ids: [],
  columns: [
    {
      id: 7, name: 'text', type: 'text', ai_generated: false,
      format: null, semantic_type: null, current_run_id: null, latest_run_id: null,
      generation_managed: false, mixed_origins: false, transcript_status: null,
      media_download_candidate: null, replay_pending_count: 0, default_hidden: false,
    },
  ],
}));

const SHEET_DATA_WIRE = {
  total: 2,
  columns: [
    {
      id: 11,
      name: 'text',
      type: 'text',
      ai_generated: false,
      format: null,
      current_run_id: null,
      transcript_status: null,
      media_download_candidate: null,
    },
  ],
  rows: [],
};

const HISTORY_WIRE = {
  ops: [],
  offset: 0,
  limit: 50,
  total: 0,
  has_more_before: false,
  has_more_after: false,
  prev_offset: null,
  next_offset: null,
  cursor_index: -1,
  cursor_op: null,
  cursor_op_loaded: true,
  undo_target: null,
  redo_target: null,
  revision: { total: 0, max_op_id: 0, op_cursor: 0 },
};

// The complete frisket.query_preview.v1 resolve payload. `query` and
// `evaluator` are fields the server always emits, so the fixture carries them
// rather than the transport pretending they are optional; `evaluator` is the
// producer's own SHEET_FILTER_ROWSET_EVALUATOR constant.
function resolvedLens(lensId: number, rowId: number): Response {
  return jsonResponse({
    lens_id: lensId,
    schema_version: 'frisket.query_preview.v1',
    query: { schema_version: 'frisket.query.v1', kind: 'sheet.filter' },
    sheet_id: 1,
    query_hash: `query-${lensId}`,
    row_ids: [rowId],
    row_count: 1,
    total: 1,
    offset: 0,
    limit: 5000,
    evaluator: { kind: 'frisket.querysets.sheet_filter', version: 'v1' },
    scores: {},
  });
}

let lensRequests: Map<number, ReturnType<typeof deferred<Response>>>;

function installFetchStub() {
  lensRequests = new Map();
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      const lensMatch = url.match(/^\/api\/projects\/p1\/lenses\/(\d+)\/resolve/);
      if (method === 'GET' && lensMatch) {
        const pending = deferred<Response>();
        lensRequests.set(Number(lensMatch[1]), pending);
        return pending.promise;
      }
      if (method === 'GET' && url === '/api/projects/p1/sheets') {
        return Promise.resolve(jsonResponse(SHEET_LIST_WIRE));
      }
      if (method === 'GET' && /^\/api\/projects\/p1\/sheets\/\d+\/data/.test(url)) {
        return Promise.resolve(jsonResponse(SHEET_DATA_WIRE));
      }
      if (method === 'GET' && url.startsWith('/api/projects/p1/history')) {
        return Promise.resolve(jsonResponse(HISTORY_WIRE));
      }
      return Promise.resolve(jsonResponse({}));
    }),
  );
}

async function readyWorkspace() {
  mountWorkspaceModel();
  await waitFor(() => expect(model().sheetsLoaded).toBe(true));
  await waitFor(() => expect(stores().route.store.get().sheetId).toBe('1'));
}

beforeEach(() => {
  mainRegionCommit.mockClear();
  overlayRegionCommit.mockClear();
  installFetchStub();
});

afterEach(() => {
  cleanup();
  latestModel = null;
  latestStores = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('STRUCT-A1 applyLens generation guard', () => {
  it('drops a resolve released after a sheet switch without resetting the grid for lens entry', async () => {
    await readyWorkspace();
    const resetForLensEntry = vi.spyOn(stores().gridView, 'resetForLensEntry');

    let applying!: Promise<void>;
    act(() => {
      applying = model().applyLens(1, 'old sheet lens');
    });
    expect(lensRequests.has(1)).toBe(true);

    act(() => model().selectSheet('2'));
    await waitFor(() => expect(stores().route.store.get().sheetId).toBe('2'));
    expect(stores().lensView.store.get().lensView).toBeNull();

    await act(async () => {
      lensRequests.get(1)!.resolve(resolvedLens(1, 101));
      await applying;
    });

    expect({
      lensView: stores().lensView.store.get().lensView,
      resetForLensEntryCalls: resetForLensEntry.mock.calls.length,
    }).toEqual({ lensView: null, resetForLensEntryCalls: 0 });
  });

  it('drops a rejection released after invalidation without setting lensOpenError', async () => {
    await readyWorkspace();

    let applying!: Promise<void>;
    act(() => {
      applying = model().applyLens(1, 'rejected lens');
    });
    stores().lensView.resetForSheetChange();

    await act(async () => {
      lensRequests.get(1)!.reject(new Error('late rejection'));
      await applying;
    });

    expect(stores().lensView.store.get().lensOpenError).toBeNull();
  });

  it('lets the newer same-sheet applyLens win when the older resolve settles last', async () => {
    await readyWorkspace();
    const resetForLensEntry = vi.spyOn(stores().gridView, 'resetForLensEntry');

    let applyingA!: Promise<void>;
    let applyingB!: Promise<void>;
    act(() => {
      applyingA = model().applyLens(1, 'lens A');
      applyingB = model().applyLens(2, 'lens B');
    });

    await act(async () => {
      lensRequests.get(2)!.resolve(resolvedLens(2, 202));
      await applyingB;
    });
    const stateAfterB = stores().lensView.store.get();
    expect(stateAfterB.lensView).toMatchObject({ lensId: 2, name: 'lens B', rowIds: [202] });

    await act(async () => {
      lensRequests.get(1)!.resolve(resolvedLens(1, 101));
      await applyingA;
    });

    expect(stores().lensView.store.get()).toBe(stateAfterB);
    expect(resetForLensEntry).toHaveBeenCalledTimes(1);
  });

  it('exitLensView invalidates an outstanding store generation token', async () => {
    await readyWorkspace();
    const token = stores().lensView.beginLensResolve();

    act(() => model().mainViewModel.exitLensView());

    expect(stores().lensView.isCurrentLensResolve(token)).toBe(false);
  });
});

describe('workspace controller composition boundaries', () => {

  it('a launch accepted after sheet navigation closes the current action route', async () => {
    await readyWorkspace();
    const launch = deferred<RunActionLaunchResult>();
    const runAction = vi.spyOn(stores().projectApi, 'runAction').mockReturnValueOnce(launch.promise);
    act(() => model().mainViewModel.startRun({
      action_id: 'map.classify',
      scope: { kind: 'sheet_rows', sheet_id: 1 },
      params: { instruction: 'Classify this row', model: 'test' },
      output_names: { result: 'classification' },
      idempotency_key: 'late-launch',
    }));
    expect(runAction).toHaveBeenCalledOnce();

    act(() => model().selectSheet('2'));
    act(() => model().runActionFromSurface('media.ocr'));
    expect(stores().route.store.get()).toMatchObject({ sheetId: '2', actionKind: 'media.ocr' });

    await act(async () => { launch.resolve({ runId: 'late-run' }); });

    expect(stores().route.store.get()).toMatchObject({ sheetId: '2', actionKind: null });
    expect(stores().chrome.store.get().actionPanelOpen).toBe(false);
  });

  it('updates palette state without rerendering the main view projection', async () => {
    await readyWorkspace();
    const before = model().mainViewModel;
    const askNavigation = model().askNavigation;
    const mainCommits = mainRegionCommit.mock.calls.length;
    const overlayCommits = overlayRegionCommit.mock.calls.length;

    act(() => model().overlayModel.setCommandPaletteQuery('document search'));

    expect(model().overlayModel.commandPaletteQuery).toBe('document search');
    expect(model().mainViewModel).toBe(before);
    expect(model().askNavigation).toBe(askNavigation);
    expect(mainRegionCommit).toHaveBeenCalledTimes(mainCommits);
    expect(overlayRegionCommit.mock.calls.length).toBeGreaterThan(overlayCommits);
  });

  it('updates comparison sessions without rerendering overlays', async () => {
    await readyWorkspace();
    const before = model().overlayModel;
    const overlayCommits = overlayRegionCommit.mock.calls.length;

    act(() => model().mainViewModel.setOcrCompareCloseWarn(true));

    expect(model().mainViewModel.ocrCompareCloseWarn).toBe(true);
    expect(model().overlayModel).toBe(before);
    expect(overlayRegionCommit).toHaveBeenCalledTimes(overlayCommits);
  });

  it('keeps document focus separate from action selection while Answers selects rows', async () => {
    await readyWorkspace();
    act(() => model().mainViewModel.openRowById('101'));
    const selected = stores().selection.store.get().selectedRows;

    act(() => model().mainViewModel.selectDocumentRow('202'));

    expect(stores().route.store.get().panel).toMatchObject({ kind: 'row', rowId: '202' });
    expect(stores().selection.store.get().selectedRows).toBe(selected);

    act(() => model().mainViewModel.selectAnswersRow('303'));

    expect(stores().selection.store.get().selectedRows.rowIds).toEqual(['303']);
    expect(stores().route.store.get().panel).toMatchObject({ kind: 'row', rowId: '202' });
  });

  it('confirms the captured delete request after selection and sheet change', async () => {
    await readyWorkspace();
    const deleteRows = vi.spyOn(stores().projectApi, 'deleteRows')
      .mockResolvedValue({ rowIds: ['101'], deleted: 1, total: 1 });
    act(() => {
      stores().selection.setSelectedRows({ sheetId: '1', rowIds: ['101'], rowIndexes: [] });
    });
    act(() => model().mainViewModel.requestDeleteRows());
    const captured = model().overlayModel.deleteRowsConfirm;
    expect(captured).toEqual({ sheetId: '1', rowIds: ['101'] });

    act(() => model().selectSheet('2'));
    act(() => {
      stores().selection.setSelectedRows({ sheetId: '2', rowIds: ['202'], rowIndexes: [] });
    });
    act(() => model().overlayModel.confirmDeleteRows());

    await waitFor(() => expect(deleteRows).toHaveBeenCalledWith('1', ['101']));
    expect(model().overlayModel.deleteRowsConfirm).toBeNull();
  });
});
