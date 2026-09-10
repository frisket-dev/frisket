import { afterEach, expect, it, vi } from 'vitest';
import { createProjectApi } from '../../src/api/real';
import type { CopilotProposal } from '../../src/api/types';

afterEach(() => vi.restoreAllMocks());
const proposal: CopilotProposal = { kind: 'derive', title: 'Expand playlist', spec: {
  action_id: 'derive.collection_expand', scope: { kind: 'project' },
  params: { source_sheet_id: 1, source_column_id: 4, source_row_id: 7 },
  sheet_name: 'Playlist videos', output_names: { url: 'Video URL' },
} };

it('keeps source and sheet naming stable across an opaque confirmation retry', async () => {
  const api = createProjectApi('run-proposal-test');
  const runAction = vi.spyOn(api, 'runAction').mockResolvedValue({ runId: null, outputSheetId: '91' });
  const before = structuredClone(proposal);
  await expect(api.runProposal(proposal)).resolves.toEqual({ run_id: null, output_sheet_id: '91' });
  await api.runProposal(proposal, true, 'exact-collection-challenge');
  expect(runAction.mock.calls[0][0]).toMatchObject(proposal.spec);
  expect(runAction.mock.calls[1][0]).toEqual({
    ...runAction.mock.calls[0][0], confirmation: 'exact-collection-challenge',
  });
  expect(proposal).toEqual(before);
  expect(runAction.mock.calls[1][0]).not.toHaveProperty('params.confirmed');
});

it('does not launch an aborted invocation', async () => {
  const api = createProjectApi('proposal-abort-project');
  const runAction = vi.spyOn(api, 'runAction').mockResolvedValue({ runId: '17' });
  const controller = new AbortController();
  controller.abort();
  await expect(api.runProposal(proposal, false, undefined, {
    projectId: 'proposal-abort-project', signal: controller.signal,
  })).rejects.toMatchObject({ name: 'AbortError' });
  expect(runAction).not.toHaveBeenCalled();
});

it('passes the captured project and abort signal through and ignores completion after cancellation', async () => {
  const api = createProjectApi('original-project');
  const controller = new AbortController();
  const runAction = vi.spyOn(api, 'runAction').mockImplementation(async () => {
    controller.abort();
    return { runId: '17' };
  });
  await expect(api.runProposal(proposal, false, undefined, {
    projectId: 'captured-project', signal: controller.signal,
  })).rejects.toMatchObject({ name: 'AbortError' });
  expect(runAction.mock.calls[0][1]).toEqual({ projectId: 'captured-project', signal: controller.signal });
});

it('passes structured Params without alias projection or catalog reads', async () => {
  const api = createProjectApi('run-proposal-python');
  const catalog = vi.spyOn(api, 'listActionCatalog');
  const runAction = vi.spyOn(api, 'runAction').mockResolvedValue({ runId: '17' });
  const params = { input_columns: ['transcript'], code: 'result = {"excerpt": row["transcript"]}',
    return_schema: { type: 'object', properties: { excerpt: { type: 'string' } }, required: ['excerpt'] },
    output_routes: [{ name: 'excerpt', path: '$.excerpt', target: { kind: 'column', type: 'text' } }],
  };
  const before = structuredClone(params);
  await api.runProposal({ kind: 'map', title: 'Python', spec: {
    action_id: 'map.python', scope: { kind: 'sheet_rows', sheet_id: 1 },
    output_names: { excerpt: 'Excerpt' }, params,
  } });
  expect(runAction.mock.calls[0][0]).toEqual({ action_id: 'map.python',
    scope: { kind: 'sheet_rows', sheet_id: 1 }, params: before, output_names: { excerpt: 'Excerpt' },
    idempotency_key: expect.stringContaining('copilot-map.python:') });
  expect(params).toEqual(before);
  expect(catalog).not.toHaveBeenCalled();
});
