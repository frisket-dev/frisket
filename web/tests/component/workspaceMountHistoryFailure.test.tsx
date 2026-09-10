// @vitest-environment jsdom
//
// Regression: the mount-time refreshHistory() used to have no .catch, so a
// malformed (or failed) GET /history body during workspace mount surfaced as
// an unhandled promise rejection (observed twice while building
// previewCostGate.test.tsx). The fix routes the failure through showError —
// the exact idiom of its siblings refreshSheets and loadHistoryPage — so a
// bad history payload becomes the standard error toast and nothing else.
// Here the REAL mount path runs (useWorkspaceModel → api/real.ts getHistory →
// global fetch, the only stubbed layer) against a malformed history body, and
// the assertions are: mount completes with zero unhandled rejections, the
// toast carries the failure, and the model stays usable (a later
// refreshHistory() against a healthy body populates history normally).

import { useEffect } from 'react';
import { act, cleanup, waitFor, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useWorkspaceModel } from '../../src/workspace/useWorkspaceModel';
import { WorkspaceStoresProvider } from '../../src/bind/WorkspaceStoresProvider';
import type { ProjectInfo } from '../../src/api/open';


// ---------------------------------------------------------------------------
// Harness (same shape as previewCostGate.test.tsx)

const PROJECT: ProjectInfo = { id: 'p1', name: 'Mount history failure test' };

type Model = ReturnType<typeof useWorkspaceModel>;
let latest: Model | null = null;

function Probe() {
  const current = useWorkspaceModel({ project: PROJECT });
  // Captured in an (every-render) effect, not during render, per
  // react-hooks/globals; every read below happens inside act/waitFor, i.e.
  // after effects have flushed.
  useEffect(() => {
    latest = current;
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
  if (!latest) throw new Error('useWorkspaceModel has not rendered yet');
  return latest;
}

// ---------------------------------------------------------------------------
// Fetch stub (the HTTP layer — one level below api/real.ts, so real.ts's
// history decode runs for real and actually chokes on the malformed body).

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

// Contract-valid sheet-list wire body (frisket.sheet_list.v1) — the mount
// must succeed on everything EXCEPT history for this test to isolate the
// history failure.
const SHEET_LIST_WIRE = [
  {
    id: 1,
    name: 'Stories',
    parent_sheet_id: null,
    parent_op_id: null,
    rows: 0,
    title_column_id: null,
    cited_column_ids: [],
    annotated_text_column_ids: [],
  },
];

// Contract-valid empty sheet-data wire body (frisket.sheet_data.v1) —
// listSheets() joins each sheet with its columns via getSheetData, so the
// mount's sheet load only completes if this route answers validly too.
const SHEET_DATA_WIRE = {
  total: 0,
  columns: [
    {
      id: 11,
      name: 'story',
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

// A healthy empty history page, served AFTER the scripted malformed
// responses run out — proves the model recovers on the next refresh.
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

/** Scripted responses for successive GET /history calls; falls through to
 *  the healthy HISTORY_WIRE once exhausted. */
let historyResponses: Array<() => Response> = [];

function installFetchStub() {
  historyResponses = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      if (method === 'GET' && url.startsWith('/api/projects/p1/history')) {
        const next = historyResponses.shift();
        return next ? next() : jsonResponse(HISTORY_WIRE);
      }
      if (method === 'GET' && url === '/api/projects/p1/sheets') {
        return jsonResponse(SHEET_LIST_WIRE);
      }
      if (method === 'GET' && url.startsWith('/api/projects/p1/sheets/1/data')) {
        return jsonResponse(SHEET_DATA_WIRE);
      }
      // Everything else the workspace mount fires (catalog, jobs, review
      // count, plugin index, …) gets a benign empty body; those paths
      // tolerate — or surface-and-swallow — malformed responses, and none of
      // them is under test here.
      return jsonResponse({});
    }),
  );
}

// ---------------------------------------------------------------------------
// Unhandled-rejection capture: the regression being pinned is precisely a
// promise nobody catches, so record every one the mount produces.

let unhandledRejections: unknown[] = [];
const onUnhandledRejection = (reason: unknown) => {
  unhandledRejections.push(reason);
};

beforeEach(() => {
  installFetchStub();
  unhandledRejections = [];
  process.on('unhandledRejection', onUnhandledRejection);
});

afterEach(() => {
  process.off('unhandledRejection', onUnhandledRejection);
  cleanup();
  latest = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------

describe('workspace mount with a malformed GET /history response', () => {
  it('completes mount with no unhandled rejection, shows the error toast, and stays usable', async () => {
    // The mount-time refreshHistory() gets a body that is valid JSON but not
    // a history page — real.ts's decode throws while mapping it.
    historyResponses = [() => jsonResponse({ not: 'a history page' })];

    mountWorkspaceModel();

    // Mount completed: sheets loaded despite the history failure.
    await waitFor(() => expect(model().sheetsLoaded).toBe(true));

    // The failure surfaced through the sibling idiom (showError → toast),
    // not as a rejection: same catch shape as refreshSheets/loadHistoryPage.
    await waitFor(() => expect(model().overlayModel.error).not.toBeNull());
    expect(model().overlayModel.error!.message).not.toBe('');

    // No bogus patch landed — history is still the initial null, not some
    // half-decoded object.
    expect(model().overlayModel.history).toBeNull();

    // The model stays usable: the next refresh (stub now serves a healthy
    // page) populates history normally.
    act(() => {
      void model().refreshHistory();
    });
    await waitFor(() => expect(model().overlayModel.history).not.toBeNull());
    expect(model().overlayModel.history!.ops).toEqual([]);

    // Drain the task queue so any would-be unhandledRejection event has had
    // its macrotask turn, then assert none fired.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(unhandledRejections).toEqual([]);
  });
});
