// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest';
import { createProjectApi } from '../../src/api/real';
import { backfillWithConfirmation } from '../../src/api/backfillWithConfirmation';

const completed = {
  schema_version: 'frisket.action_result.v1',
  action: { kind: 'run.backfill', action_id: 'backfill-attempt' },
  status: 'completed', project_id: 'backfill-wire', run_id: 12,
  receipt_id: 'backfill-receipt', errors: [],
  outputs: [{ kind: 'run_backfill', ref: { filled: 2, run_id: 12 } }],
};

function response(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe('typed backfill transport', () => {
  it.each([undefined, [], [9, 3]])('preserves exact row-selection intent %j', async (rowIds) => {
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
      expect(url).toContain('/api/projects/backfill-wire/actions/v1/run');
      const body = JSON.parse(String(init.body));
      posts.push(body);
      return rowIds?.length === 0 ? response({
        ...completed, status: 'failed', outputs: [],
        errors: [{ code: 'invalid_params', message: 'Explicit backfill rows must not be empty', field: 'scope.row_ids' }],
      }, 400) : response(completed);
    }));
    const api = createProjectApi('backfill-wire');
    const run = api.backfillColumn('7', 'Transcript', false, rowIds);
    if (rowIds?.length === 0) await expect(run).rejects.toThrow('must not be empty');
    else await expect(run).resolves.toEqual({ filled: 2, runId: '12' });
    expect(posts).toEqual([{
      action_id: 'run.backfill',
      scope: { kind: 'sheet_rows', sheet_id: 7, ...(rowIds === undefined ? {} : { row_ids: rowIds }) },
      params: { column: 'Transcript' }, output_names: {}, idempotency_key: expect.any(String),
    }]);
  });

  it('binds the priced 402 token to the same selected-row request and pending key', async () => {
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init: RequestInit) => {
      posts.push(JSON.parse(String(init.body)));
      return posts.length === 1 ? response({
        ...completed, status: 'needs_confirmation', outputs: [],
        errors: [{ code: 'model_cost_requires_confirmation', message: 'Approve these two rows',
          details: { reason: 'model_cost', estimate: { cost: 2.5, rows: 2 }, promise_set_hash: 'exact-quote' } }],
      }, 402) : response(completed);
    }));
    const approve = vi.fn(async () => true);
    const api = createProjectApi('backfill-wire');
    await expect(backfillWithConfirmation(api, approve, '7', 'Transcript', [9, 3]))
      .resolves.toEqual({ filled: 2, runId: '12' });
    expect(approve).toHaveBeenCalledWith(expect.objectContaining({ cost: 2.5, rows: 2, promise_set_hash: 'exact-quote' }), 'Approve these two rows');
    expect(posts).toHaveLength(2);
    expect(posts[0].confirmation).toBeUndefined();
    expect(posts[1]).toEqual({ ...posts[0], confirmation: 'exact-quote' });
    expect(posts[1].scope).toEqual({ kind: 'sheet_rows', sheet_id: 7, row_ids: [9, 3] });
    expect(posts[1].params).toEqual({ column: 'Transcript' });
  });

  it('propagates a 429 without prompting and retains its key for the same retry only', async () => {
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init: RequestInit) => {
      posts.push(JSON.parse(String(init.body)));
      return response({
        ...completed, status: 'failed', outputs: [],
        errors: [{ code: 'rate_limit_exceeded', message: 'Try again later' }],
      }, 429);
    }));
    const api = createProjectApi('backfill-wire');
    const approve = vi.fn(async () => true);
    for (const rowIds of [[9], [9], [3], undefined]) {
      await expect(backfillWithConfirmation(api, approve, '7', 'Transcript', rowIds))
        .rejects.toThrow('Try again later');
    }
    expect(approve).not.toHaveBeenCalled();
    expect(posts[0].idempotency_key).toBe(posts[1].idempotency_key);
    expect(new Set(posts.map((post) => post.idempotency_key)).size).toBe(3);
  });

  it('does not turn a quote token into approval without the explicit user decision', async () => {
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal('fetch', vi.fn(async (_url: string, init: RequestInit) => {
      posts.push(JSON.parse(String(init.body)));
      return response(completed);
    }));
    await createProjectApi('backfill-wire').backfillColumn('7', 'Transcript', false, [9], 'not-approved');
    expect(posts[0].confirmation).toBeUndefined();
    expect(posts[0].params).toEqual({ column: 'Transcript' });
  });
});
