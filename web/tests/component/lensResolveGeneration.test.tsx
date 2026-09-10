// @vitest-environment jsdom

import { useEffect } from 'react';
import { act, cleanup, waitFor, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useWorkspaceModel } from '../../src/workspace/useWorkspaceModel';
import { useWorkspaceStores } from '../../src/bind/useWorkspaceStores';
import { WorkspaceStoresProvider } from '../../src/bind/WorkspaceStoresProvider';
import type { ProjectInfo } from '../../src/api/open';
import type { WorkspaceStores } from '../../src/state/createWorkspaceStores';


const PROJECT: ProjectInfo = { id: 'p1', name: 'Lens generation test project' };

type Model = ReturnType<typeof useWorkspaceModel>;
let latestModel: Model | null = null;
let latestStores: WorkspaceStores | null = null;

function Probe() {
  const currentModel = useWorkspaceModel({ project: PROJECT });
  const currentStores = useWorkspaceStores();
  useEffect(() => {
    latestModel = currentModel;
    latestStores = currentStores;
  });
  return null;
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
