import { afterEach, expect, it, vi } from 'vitest';
import { createProjectApi } from '../../src/api/real';
import type { ActionProposal } from '../../src/api/types';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});
const proposal: ActionProposal = { kind: 'derive', title: 'Expand playlist', spec: {
  action_id: 'derive.collection_expand', scope: { kind: 'project' },
  params: { source_sheet_id: 1, source_column_id: 4, source_row_id: 7 },
  sheet_name: 'Playlist videos', output_names: { url: 'Video URL' },
} };

interface PostedAction {
  url: string;
  body: Record<string, unknown>;
  signal: AbortSignal | null | undefined;
}

function actionTransport(onPost: () => void = () => {}) {
  const posted: PostedAction[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
    posted.push({ url: String(input), body, signal: init?.signal });
    onPost();
    const materializes = body.sheet_name !== undefined;
    return new Response(JSON.stringify({ schema_version: 'frisket.action_result.v1',
      action: { kind: body.action_id, action_id: `proposal-${posted.length}` },
      status: 'completed',
      run_id: materializes ? null : 17,
      receipt_id: `receipt-${posted.length}`,
      outputs: materializes ? [{ kind: 'sheet', sheet_id: 91, name: body.sheet_name,
        ref: { kind: 'materialized_sheet', sheet_id: 91, row_count: 2 } }] : [],
      errors: [],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
  vi.stubGlobal('fetch', fetchMock);
  return { posted, fetchMock };
}

it('keeps source and sheet naming stable across an opaque confirmation retry', async () => {
  const api = createProjectApi('run-proposal-test');
  const { posted } = actionTransport();
  const before = structuredClone(proposal);
  await expect(api.runProposal(proposal)).resolves.toEqual({ run_id: null, output_sheet_id: '91' });
  await api.runProposal(proposal, true, 'exact-collection-challenge');
  expect(posted.map(({ url }) => url)).toEqual([
    '/api/projects/run-proposal-test/actions/v1/run',
    '/api/projects/run-proposal-test/actions/v1/run',
  ]);
  expect(posted[0].body).toMatchObject(proposal.spec);
  expect(posted[1].body).toEqual({
    ...posted[0].body, confirmation: 'exact-collection-challenge',
  });
  expect(proposal).toEqual(before);
  expect(posted[1].body).not.toHaveProperty('params.confirmed');
});

it('does not launch an aborted invocation', async () => {
  const api = createProjectApi('proposal-abort-project');
  const { fetchMock } = actionTransport();
  const controller = new AbortController();
  controller.abort();
  await expect(api.runProposal(proposal, false, undefined, {
    projectId: 'proposal-abort-project', signal: controller.signal,
  })).rejects.toMatchObject({ name: 'AbortError' });
  expect(fetchMock).not.toHaveBeenCalled();
});

it('passes the captured project and abort signal through and ignores completion after cancellation', async () => {
  const api = createProjectApi('original-project');
  const controller = new AbortController();
  const { posted } = actionTransport(() => controller.abort());
  await expect(api.runProposal(proposal, false, undefined, {
    projectId: 'captured-project', signal: controller.signal,
  })).rejects.toMatchObject({ name: 'AbortError' });
  expect(posted).toHaveLength(1);
  expect(posted[0].url).toBe('/api/projects/captured-project/actions/v1/run');
  expect(posted[0].signal).toBe(controller.signal);
});

it('passes structured Params without alias projection or catalog reads', async () => {
  const api = createProjectApi('run-proposal-python');
  const { posted, fetchMock } = actionTransport();
  const params = { input_columns: ['transcript'], code: 'result = {"excerpt": row["transcript"]}',
    return_schema: { type: 'object', properties: { excerpt: { type: 'string' } }, required: ['excerpt'] },
    output_routes: [{ name: 'excerpt', path: '$.excerpt', target: { kind: 'column', type: 'text' } }],
  };
  const before = structuredClone(params);
  await expect(api.runProposal({ kind: 'map', title: 'Python', spec: {
    action_id: 'map.python', scope: { kind: 'sheet_rows', sheet_id: 1 },
    output_names: { excerpt: 'Excerpt' }, params,
  } })).resolves.toEqual({ run_id: 17 });
  expect(fetchMock).toHaveBeenCalledOnce();
  expect(posted[0].url).toBe('/api/projects/run-proposal-python/actions/v1/run');
  expect(posted[0].body).toEqual({ action_id: 'map.python',
    scope: { kind: 'sheet_rows', sheet_id: 1 }, params: before, output_names: { excerpt: 'Excerpt' },
    idempotency_key: expect.stringContaining('ask-map.python:') });
  expect(params).toEqual(before);
});
