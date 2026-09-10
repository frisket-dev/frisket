// @vitest-environment jsdom
//
// The real preview launch path must distinguish the two server-owned outcomes:
// an effectful action preview is one terminal typed 400 (no modal or retry),
// while an exact-free operator-LAN preview keeps its ClaimsGate 402 and exact
// echo. Here the real path runs — executeRegisteredAction →
// openPreviewView → api.startPreview → global fetch (the only stubbed layer,
// mirroring tests/unit/resolvePreviewApi.test.ts) — and the assertions are on
// the actual POST bodies and terminal preview-view state:
//   preview_effect_requires_run → one unconfirmed POST, terminal error banner;
//   free operator-LAN 402 → shared modal → exact confirmed/hash retry.
// jobStore.test.ts remains the behavioral proof for the startRun (persisted
// run) path; this file is its preview-path sibling.

import { useEffect } from 'react';
import { act, cleanup, waitFor, render } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useWorkspaceModel } from '../../src/workspace/useWorkspaceModel';
import { WorkspaceStoresProvider } from '../../src/bind/WorkspaceStoresProvider';
import { useJobsHandle } from '../../src/bind/useJobsHandle';
import type { ProjectInfo } from '../../src/api/open';
import type { GeneratedActionRequest } from '../../src/api/types';
import type { JobStoreHandle } from '../../src/state/jobStore';


// ---------------------------------------------------------------------------
// Harness

const PROJECT: ProjectInfo = { id: 'p1', name: 'Preview gate test project' };

type Model = ReturnType<typeof useWorkspaceModel>;
let latest: Model | null = null;
let latestJobs: JobStoreHandle | null = null;

function Probe() {
  const current = useWorkspaceModel({ project: PROJECT });
  const jobs = useJobsHandle();
  // Captured in an (every-render) effect, not during render, per
  // react-hooks/globals; every read below happens inside act/waitFor, i.e.
  // after effects have flushed.
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

// ---------------------------------------------------------------------------
// Fetch stub (the HTTP layer — one level below api/real.ts, so real.ts's 402
// decode and v1-spec encode both run for real).

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

// Contract-valid wire bodies (frisket.sheet_list.v1 / frisket.sheet_data.v1) —
// the strict generated validators reject anything less.
const SHEET_LIST_WIRE = [
  {
    id: 1,
    name: 'Stories',
    parent_sheet_id: null,
    parent_op_id: null,
    rows: 3,
    title_column_id: null,
    cited_column_ids: [],
    annotated_text_column_ids: [],
  },
];

const SHEET_DATA_WIRE = {
  total: 3,
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
  rows: [
    { id: 1, cells: {}, meta: {}, parent_row_id: null, child_count: 0 },
    { id: 2, cells: {}, meta: {}, parent_row_id: null, child_count: 0 },
    { id: 3, cells: {}, meta: {}, parent_row_id: null, child_count: 0 },
  ],
};

// Minimal empty history page — a malformed body here would surface as an
// error toast (see workspaceMountHistoryFailure.test.tsx), muddying the
// cost-gate assertions below.
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

const EFFECT_REQUIRES_RUN_MESSAGE =
  'This action can cause an external or metered effect, so it cannot run as a preview. Run the action instead.';

const EFFECT_REQUIRES_RUN_400 = {
  schema_version: 'frisket.action_preview.v1',
  error: {
    schema_version: 'frisket.action_error.v1',
    code: 'preview_effect_requires_run',
    message: EFFECT_REQUIRES_RUN_MESSAGE,
    action_kind: 'map.translate',
    field: 'kind',
    details: { requires_durable_run: true },
    needs_confirmation: false,
  },
};

const SIDECAR_GATE_MESSAGE =
  'This action sends data to your operator-managed LAN service. Confirm to continue.';
const SIDECAR_PROMISE_SET_HASH = 'a'.repeat(64);

const SIDECAR_CONFIRMATION_402 = {
  schema_version: 'frisket.action_preview.v1',
  error: {
    schema_version: 'frisket.action_error.v1',
    code: 'cost_gate',
    message: SIDECAR_GATE_MESSAGE,
    action_kind: 'media.ocr',
    field: null,
    details: {
      estimate: {
        cost: 0,
        rows: 2,
        cost_source: 'free_local',
        billed_cost: 0,
        policy_id: 'frisket.pricing.identity.v1',
      },
      claims: [
        {
          field: 'egress_class',
          display: 'Media leaves this machine for your own LAN service.',
        },
      ],
      promise_set_hash: SIDECAR_PROMISE_SET_HASH,
    },
    needs_confirmation: false,
  },
};

const PREVIEW_STARTED_202 = {
  schema_version: 'frisket.preview_start.v1',
  preview_id: 'pv-1',
  total: 2,
};

/** Every POST body sent to the preview endpoint, in order — the assertion
 *  surface of this file. */
let previewStartBodies: Array<Record<string, unknown>> = [];
/** Scripted responses for successive preview-start POSTs. */
let previewStartResponses: Array<() => Response> = [];

function installFetchStub() {
  previewStartBodies = [];
  previewStartResponses = [];
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      if (method === 'POST' && url === '/api/projects/p1/actions/v1/preview') {
        previewStartBodies.push(
          JSON.parse(String(init?.body)) as Record<string, unknown>,
        );
        const next = previewStartResponses.shift();
        if (!next) {
          throw new Error(
            `unscripted preview-start POST #${previewStartBodies.length}`,
          );
        }
        return next();
      }
      if (method === 'GET' && url.startsWith('/api/projects/p1/actions/v1/preview/')) {
        return jsonResponse({
          schema_version: 'frisket.preview_status.v1',
          preview_id: 'pv-1',
          status: 'running',
          progress: { done: 0, total: 2 },
        });
      }
      if (method === 'GET' && url === '/api/projects/p1/sheets') {
        return jsonResponse(SHEET_LIST_WIRE);
      }
      if (method === 'GET' && url.startsWith('/api/projects/p1/history')) {
        return jsonResponse(HISTORY_WIRE);
      }
      if (method === 'GET' && url.startsWith('/api/projects/p1/sheets/1/data')) {
        return jsonResponse(SHEET_DATA_WIRE);
      }
      // Everything else the workspace mount fires (catalog, history, jobs,
      // plugin index, …) gets a benign empty body; those paths tolerate — or
      // surface-and-swallow — malformed responses, and none of them is under
      // test here.
      return jsonResponse({});
    }),
  );
}

// Built-in previews use the same canonical request as their generated form.
const PREVIEW_REQ: GeneratedActionRequest = {
  action_id: 'map.translate',
  scope: { kind: 'sheet_rows', sheet_id: 1, row_ids: [1, 2] },
  params: { source: ['story'], engine: 'llm', target_language: 'English',
    model: 'anthropic/claude-haiku-4-5' },
  output_names: { translation: 'story_en' },
  idempotency_key: 'translate-preview-test',
};

const SIDECAR_PREVIEW_REQ: GeneratedActionRequest = {
  action_id: 'media.ocr', scope: { kind: 'sheet_rows', sheet_id: 1 },
  params: { source: 'story', engine: 'dots.mocr' },
  output_names: { text: 'ocr_text', blocks: 'ocr_blocks' },
  idempotency_key: 'ocr-preview-test',
};

function params(body: Record<string, unknown>): Record<string, unknown> {
  return body.params as Record<string, unknown>;
}

async function openPreview(req: GeneratedActionRequest): Promise<void> {
  mountWorkspaceModel();
  // The workspace has loaded its sheet — openPreviewView bails on !sheet.
  await waitFor(() => expect(model().sheetsLoaded).toBe(true));
  act(() => {
    model().executeRegisteredAction(req, 'preview');
  });
}

async function openGatedPreview(req: GeneratedActionRequest): Promise<void> {
  await openPreview(req);
  // The typed 402 reached the SHARED confirmation flow (the same
  // costGate state CostGateModal renders from), not a terminal error banner.
  await waitFor(() => expect(jobHandle().store.get().costGate).not.toBeNull());
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

// ---------------------------------------------------------------------------

describe('action preview effect admission (behavioral)', () => {
  it('renders the exact typed 400 as a terminal preview error with one POST and no modal', async () => {
    previewStartResponses = [() => jsonResponse(EFFECT_REQUIRES_RUN_400, 400)];

    await openPreview(PREVIEW_REQ);

    await waitFor(() =>
      expect(model().mainViewModel.activePreviewView?.status).toBe('error'),
    );
    expect(previewStartBodies).toHaveLength(1);
    expect(previewStartBodies[0]).toEqual(PREVIEW_REQ);
    expect(previewStartBodies[0]).not.toHaveProperty('confirmation');
    expect(JSON.stringify(previewStartBodies[0])).not.toContain(
      'consented_promise_set_hash',
    );
    expect(jobHandle().store.get().costGate).toBeNull();
    expect(model().mainViewModel.activePreviewView).toMatchObject({
      previewId: '',
      status: 'error',
      error: EFFECT_REQUIRES_RUN_MESSAGE,
    });
    // No server preview id means no poll/cancel lifecycle and no retry path.
    expect(previewStartResponses).toHaveLength(0);
  });

  it('retains the legitimate free operator-LAN ClaimsGate modal and exact echo retry', async () => {
    previewStartResponses = [
      () => jsonResponse(SIDECAR_CONFIRMATION_402, 402),
      () => jsonResponse(PREVIEW_STARTED_202, 202),
    ];

    await openGatedPreview(SIDECAR_PREVIEW_REQ);

    expect(previewStartBodies).toHaveLength(1);
    expect(previewStartBodies[0].action_id).toBe('media.ocr');
    expect(params(previewStartBodies[0])).toEqual({ source: 'story', engine: 'dots.mocr' });
    expect(previewStartBodies[0]).not.toHaveProperty('confirmation');
    expect(JSON.stringify(previewStartBodies[0])).not.toContain(
      'consented_promise_set_hash',
    );

    const gate = jobHandle().store.get().costGate!;
    expect(gate.estimate).toMatchObject({
      cost: 0,
      rows: 2,
      billed_cost: 0,
      cost_source: 'free_local',
      claims: [
        {
          field: 'egress_class',
          display: 'Media leaves this machine for your own LAN service.',
        },
      ],
      promise_set_hash: SIDECAR_PROMISE_SET_HASH,
    });
    expect(gate.message).toBe(SIDECAR_GATE_MESSAGE);

    act(() => {
      jobHandle().confirmCostGate();
    });

    await waitFor(() => expect(previewStartBodies).toHaveLength(2));
    const retry = previewStartBodies[1];
    expect(retry).toEqual({ ...previewStartBodies[0], confirmation: SIDECAR_PROMISE_SET_HASH });

    await waitFor(() =>
      expect(model().mainViewModel.activePreviewView?.previewId).toBe('pv-1'),
    );
    expect(jobHandle().store.get().costGate).toBeNull();
    expect(model().mainViewModel.activePreviewView?.status).toBe('running');
  });
});
