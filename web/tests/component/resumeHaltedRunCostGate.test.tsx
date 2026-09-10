// @vitest-environment jsdom
//
// Ruling 4 ("a retry is a resume") — the model half of the Monitor dock's
// halted-run resume. The dock hands the halted job to
// model.resumeHaltedRun(sheetId, columnName); this proves, at the wire, that
// the resume:
//   * is run.backfill (durable results replay free, only never-completed rows
//     buy) — never a fresh full-run door;
//   * targets the halted run's OWN sheet, not whichever sheet happens to be
//     selected;
//   * routes an over-gate resume through the SAME 402 → shared-cost-gate →
//     confirmed retry loop as every other purchase, reusing the pending
//     idempotency key so consent authorizes the same request.
//
// Harness: previewCostGate.test.tsx's — the real workspace model over a
// stubbed global fetch, with assertions on the actual POST bodies.

import { useEffect } from 'react';
import { act, cleanup, waitFor, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useWorkspaceModel } from '../../src/workspace/useWorkspaceModel';
import { WorkspaceStoresProvider } from '../../src/bind/WorkspaceStoresProvider';
import { useJobsHandle } from '../../src/bind/useJobsHandle';
import type { ProjectInfo } from '../../src/api/open';
import type { JobStoreHandle } from '../../src/state/jobStore';


const PROJECT: ProjectInfo = { id: 'p1', name: 'Resume gate test project' };

type Model = ReturnType<typeof useWorkspaceModel>;
let latest: Model | null = null;
let latestJobs: JobStoreHandle | null = null;

function Probe() {
  const current = useWorkspaceModel({ project: PROJECT });
  const jobs = useJobsHandle();
  useEffect(() => {
    latest = current;
    latestJobs = jobs;
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

function jobHandle(): JobStoreHandle {
  if (!latestJobs) throw new Error('job handle has not mounted yet');
  return latestJobs;
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

// TWO sheets, and the workspace selects the FIRST — the resume below names
// the second, which is the point: a halted run resumes on ITS sheet.
const SHEET_LIST_WIRE = [1, 2].map((id) => ({
  id,
  name: id === 1 ? 'Stories' : 'Interviews',
  parent_sheet_id: null,
  parent_op_id: null,
  rows: 3,
  title_column_id: null,
  cited_column_ids: [],
  annotated_text_column_ids: [],
}));

// Served for BOTH sheets: listSheets enriches every sheet with a limit=0
// data read, so each listed sheet needs a contract-valid data body.
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

const GATE_MESSAGE =
  'estimated cost $2.80 exceeds the $1.00 gate — confirm to run it anyway.';
const PROMISE_SET_HASH = 'resume-ps-1';

// The v1 run endpoint's 402: an action_result.v1 envelope paused on
// needs_confirmation (parseJsonResponse decodes this into the shared
// ConfirmationRequiredError).
const CONFIRMATION_402 = {
  schema_version: 'frisket.action_result.v1',
  status: 'needs_confirmation',
  run_id: null,
  receipt_id: null,
  outputs: [],
  errors: [
    {
      code: 'needs_confirmation',
      field: 'confirmation',
      message: GATE_MESSAGE,
      details: {
        reason: 'model_cost',
        estimate: { cost: 2.8, rows: 28 },
        promise_set_hash: PROMISE_SET_HASH,
      },
    },
  ],
};

const BACKFILL_COMPLETED = {
  schema_version: 'frisket.action_result.v1',
  status: 'completed',
  run_id: 9,
  receipt_id: 'r-9',
  outputs: [{ kind: 'run_backfill', name: null, ref: { filled: 28, run_id: 9 } }],
  errors: [],
};

let runBodies: Array<Record<string, unknown>> = [];
let runResponses: Array<() => Response> = [];

function installFetchStub() {
  runBodies = [];
  runResponses = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      if (method === 'POST' && url === '/api/projects/p1/actions/v1/run') {
        runBodies.push(JSON.parse(String(init?.body)) as Record<string, unknown>);
        const next = runResponses.shift();
        if (!next) throw new Error(`unscripted v1 run POST #${runBodies.length}`);
        return next();
      }
      if (method === 'GET' && url === '/api/projects/p1/sheets') {
        return jsonResponse(SHEET_LIST_WIRE);
      }
      if (method === 'GET' && url.startsWith('/api/projects/p1/history')) {
        return jsonResponse(HISTORY_WIRE);
      }
      if (
        method === 'GET' &&
        /^\/api\/projects\/p1\/sheets\/\d+\/data/.test(url)
      ) {
        return jsonResponse(SHEET_DATA_WIRE);
      }
      return jsonResponse({});
    }),
  );
}

function params(body: Record<string, unknown>): Record<string, unknown> {
  return body.params as Record<string, unknown>;
}

beforeEach(() => {
  installFetchStub();
});

afterEach(() => {
  cleanup();
  latest = null;
  latestJobs = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('resumeHaltedRun (behavioral)', () => {
  it('resumes via run.backfill on the run’s OWN sheet, through the shared cost gate', async () => {
    runResponses = [
      () => jsonResponse(CONFIRMATION_402, 402),
      () => jsonResponse(BACKFILL_COMPLETED),
    ];

    mountWorkspaceModel();
    await waitFor(() => expect(model().sheetsLoaded).toBe(true));

    act(() => {
      void model().resumeHaltedRun('2', 'transcript');
    });

    // The unconfirmed attempt reached the wire as run.backfill for sheet 2 —
    // the halted run's sheet, NOT the selected sheet 1 — and the 402 landed
    // on the SHARED cost-gate surface.
    await waitFor(() => expect(jobHandle().store.get().costGate).not.toBeNull());
    expect(runBodies).toHaveLength(1);
    expect(runBodies[0].action_id).toBe('run.backfill');
    expect(runBodies[0].scope).toEqual({ kind: 'sheet_rows', sheet_id: 2 });
    expect(params(runBodies[0])).toEqual({ column: 'transcript' });
    expect(runBodies[0].confirmation).toBeUndefined();
    expect(jobHandle().store.get().costGate!.message).toBe(GATE_MESSAGE);

    act(() => {
      jobHandle().confirmCostGate();
    });

    // The confirmed retry is the SAME semantic request: same kind, same
    // sheet/column, the consent bits added, and the SAME pending idempotency
    // key — consent authorizes the request, it does not mint a new purchase.
    await waitFor(() => expect(runBodies).toHaveLength(2));
    const retry = runBodies[1];
    expect(retry.action_id).toBe('run.backfill');
    expect(retry.scope).toEqual({ kind: 'sheet_rows', sheet_id: 2 });
    expect(params(retry)).toEqual({ column: 'transcript' });
    expect(retry.confirmation).toBe(PROMISE_SET_HASH);
    expect(retry.idempotency_key).toBe(runBodies[0].idempotency_key);
  });

  it('cancelling the gate abandons the resume: no second POST', async () => {
    runResponses = [() => jsonResponse(CONFIRMATION_402, 402)];

    mountWorkspaceModel();
    await waitFor(() => expect(model().sheetsLoaded).toBe(true));

    act(() => {
      void model().resumeHaltedRun('2', 'transcript');
    });
    await waitFor(() => expect(jobHandle().store.get().costGate).not.toBeNull());

    act(() => {
      jobHandle().cancelCostGate();
    });

    await waitFor(() => expect(jobHandle().store.get().costGate).toBeNull());
    expect(runBodies).toHaveLength(1);
  });
});
