import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  V1ActionIdempotencyKeys,
  v1ActionIdempotencySignature,
} from '../../src/api/real';
import { createV1ActionSession } from '../../src/api/v1ActionSession';

describe('v1 action idempotency identity', () => {
  it('keys registered requests by params and envelope names through clear and release', () => {
    vi.useFakeTimers();
    const session = createV1ActionSession('project-a');
    const params = { index_id: 'index-a', k: 3 };
    const request = (sheet_name?: string, output_names?: Record<string, string>) =>
      session.registeredProjectActionSpec(
        'embedding.index_cluster', params, undefined, undefined,
        { sheet_name, output_names },
      );
    const original = request('Clusters', { cluster_id: 'group' });
    const differentSheet = request('Other clusters', { cluster_id: 'group' });
    const differentColumn = request('Clusters', { cluster_id: 'category' });
    expect(new Set([
      original.idempotency_key, differentSheet.idempotency_key, differentColumn.idempotency_key,
    ]).size).toBe(3);
    expect(request('Clusters', { cluster_id: 'group' }).idempotency_key)
      .toBe(original.idempotency_key);
    expect(request().idempotency_key).toBe(request(undefined, {}).idempotency_key);

    session.clearV1ActionIdempotencyKey(original);
    const afterClear = request('Clusters', { cluster_id: 'group' });
    expect(afterClear.idempotency_key).not.toBe(original.idempotency_key);
    expect(request('Other clusters', { cluster_id: 'group' }).idempotency_key)
      .toBe(differentSheet.idempotency_key);
    session.releaseV1ActionIdempotencyKey(afterClear);
    expect(request('Clusters', { cluster_id: 'group' }).idempotency_key)
      .toBe(afterClear.idempotency_key);
    vi.advanceTimersByTime(5001);
    expect(request('Clusters', { cluster_id: 'group' }).idempotency_key)
      .not.toBe(afterClear.idempotency_key);
    vi.useRealTimers();
  });

  it('treats confirmation as authorization for the same request', () => {
    const semanticParams = {
      sheet_id: 1,
      engine: 'llm',
      model: 'openai/gpt-5.6-terra',
    };

    const absent = v1ActionIdempotencySignature('map.ner', semanticParams);
    const unconfirmed = v1ActionIdempotencySignature('map.ner', {
      ...semanticParams,
      confirmed: false,
    });
    const confirmed = v1ActionIdempotencySignature('map.ner', {
      ...semanticParams,
      confirmed: true,
    });

    expect(unconfirmed).toBe(absent);
    expect(confirmed).toBe(absent);
    expect(v1ActionIdempotencySignature('map.ner', {
      ...semanticParams,
      model: 'anthropic/claude-haiku-4-5',
      confirmed: true,
    })).not.toBe(absent);
  });

  it('treats the R3a claims-gate hash echo as retry-flow plumbing', () => {
    const params = {
      sheet_id: 1,
      input_columns: ['text'],
      engine: 'llm',
    };
    const absent = v1ActionIdempotencySignature('map.ner', params);
    const echoed = v1ActionIdempotencySignature('map.ner', {
      ...params,
      confirmed: true,
      consented_promise_set_hash: 'abc123',
    });
    expect(echoed).toBe(absent);
  });

  it('canonicalizes nested object order without collapsing array or scalar identity', () => {
    const baselineParams = {
      sheet_id: 1,
      input_columns: ['title', 'body'],
      output_schema: {
        type: 'object',
        properties: {
          score: { type: 'number' },
          rationale: { type: 'string' },
        },
      },
    };
    const baseline = v1ActionIdempotencySignature('map.extract', baselineParams);
    const relationship = (params: Record<string, unknown>): 'same' | 'different' =>
      v1ActionIdempotencySignature('map.extract', params) === baseline ? 'same' : 'different';

    expect([
      relationship({
        ...baselineParams,
        output_schema: {
          properties: {
            rationale: { type: 'string' },
            score: { type: 'number' },
          },
          type: 'object',
        },
      }),
      relationship({ ...baselineParams, input_columns: ['body', 'title'] }),
      relationship({ ...baselineParams, sheet_id: 2 }),
      relationship({ ...baselineParams, sheet_id: '1' }),
    ]).toEqual(['same', 'different', 'different', 'different']);
  });
});

// Ruling 4 ("a retry is a resume; everything else is a deliberate NEW
// purchase") — the client key policy that makes the accidental double-submit
// a replay and the deliberate second run a purchase:
//   * while a request is pending (and through its confirm loop), an identical
//     request carries the SAME key, so the server dedupes/replays;
//   * after SUCCESS the key is released, not cleared: it lingers for a short
//     replay window so a double-click that lands just after completion
//     replays the terminal receipt instead of buying a second run;
//   * past the window an identical request mints a FRESH key — a second run
//     is a new consented purchase;
//   * failure paths clear immediately (RealApi.withV1ActionResult calls
//     clear, never release, on failure), so a retry is a real second attempt.
describe('V1ActionIdempotencyKeys — the double-buy guard', () => {
  const PARAMS = { sheet_id: 1, column: 'transcript' };

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('keeps one key per semantic request while pending, distinct across requests', () => {
    const keys = new V1ActionIdempotencyKeys();
    const key = keys.keyFor('run.backfill', PARAMS);
    expect(key).toMatch(/^web-run\.backfill:/);
    // Identical re-submit (double-click while in flight) reuses the key…
    expect(keys.keyFor('run.backfill', PARAMS)).toBe(key);
    // …and the confirm-loop identity applies (confirmed/hash stripped).
    expect(
      keys.keyFor('run.backfill', {
        ...PARAMS,
        confirmed: true,
        consented_promise_set_hash: 'h1',
      }),
    ).toBe(key);
    // A different semantic request is a different purchase.
    expect(keys.keyFor('run.backfill', { sheet_id: 2, column: 'transcript' })).not.toBe(key);
  });

  it('namespaces the browser key owner by project without changing semantic wire identity', () => {
    const keys = new V1ActionIdempotencyKeys();
    const projectAKey = keys.keyFor('run.backfill', PARAMS, 'project-a');
    const projectBKey = keys.keyFor('run.backfill', PARAMS, 'project-b');

    expect(projectAKey).not.toBe(projectBKey);
    expect(projectAKey).toMatch(/^web-run\.backfill:/);
    expect(projectBKey).toMatch(/^web-run\.backfill:/);
    expect(projectAKey).not.toContain('project-a');
    expect(projectBKey).not.toContain('project-b');
    expect(keys.keyFor('run.backfill', PARAMS, 'project-a')).toBe(projectAKey);
    expect(keys.keyFor('run.backfill', PARAMS, 'project-b')).toBe(projectBKey);

    keys.clear('run.backfill', PARAMS, 'project-a');
    expect(keys.keyFor('run.backfill', PARAMS, 'project-a')).not.toBe(projectAKey);
    expect(keys.keyFor('run.backfill', PARAMS, 'project-b')).toBe(projectBKey);
    expect(v1ActionIdempotencySignature('run.backfill', PARAMS)).toBe(
      '{"actionKind":"run.backfill","params":{"column":"transcript","sheet_id":1}}',
    );
  });

  it('release keeps the key replayable for the window, then mints fresh', () => {
    const keys = new V1ActionIdempotencyKeys();
    const key = keys.keyFor('run.backfill', PARAMS);
    keys.release('run.backfill', PARAMS);

    // Inside the window: the accidental second click replays server-side.
    vi.advanceTimersByTime(4999);
    expect(keys.keyFor('run.backfill', PARAMS)).toBe(key);

    // keyFor above re-observed the SAME key, so the original timer still
    // owns it; past the window, an identical request is a new purchase.
    vi.advanceTimersByTime(2);
    expect(keys.keyFor('run.backfill', PARAMS)).not.toBe(key);
  });

  it('preserves the legacy third-position replay-window argument', () => {
    const keys = new V1ActionIdempotencyKeys();
    const key = keys.keyFor('run.backfill', PARAMS);
    keys.release('run.backfill', PARAMS, 25);

    vi.advanceTimersByTime(24);
    expect(keys.keyFor('run.backfill', PARAMS)).toBe(key);
    vi.advanceTimersByTime(2);
    expect(keys.keyFor('run.backfill', PARAMS)).not.toBe(key);
  });

  it('clear (the failure path) mints a fresh key immediately', () => {
    const keys = new V1ActionIdempotencyKeys();
    const key = keys.keyFor('run.backfill', PARAMS);
    keys.clear('run.backfill', PARAMS);
    expect(keys.keyFor('run.backfill', PARAMS)).not.toBe(key);
  });

  it('an expired release never reaps a NEWER key for the same request', () => {
    const keys = new V1ActionIdempotencyKeys();
    keys.keyFor('run.backfill', PARAMS);
    keys.release('run.backfill', PARAMS);
    // A failure-clear plus a new attempt inside the window: the stale timer
    // must not delete the successor key when it fires.
    keys.clear('run.backfill', PARAMS);
    const successor = keys.keyFor('run.backfill', PARAMS);
    vi.advanceTimersByTime(10_000);
    expect(keys.keyFor('run.backfill', PARAMS)).toBe(successor);
  });

  it('releases a successful registered project action and clears a failed one', async () => {
    const completed = {
      schema_version: 'frisket.action_result.v1',
      action: { kind: 'source.delete', action_id: 'delete-1' },
      status: 'completed',
      project_id: 'project-a',
      run_id: null,
      receipt_id: null,
      outputs: [],
      errors: [],
    };
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(completed), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })));
    const session = createV1ActionSession('project-a');
    const first = session.registeredProjectActionSpec('source.delete', { source_id: 7 });
    await session.withV1ActionResult(first, () => undefined, { clearIdempotency: 'finally' });
    vi.advanceTimersByTime(5001);
    const afterSuccess = session.registeredProjectActionSpec('source.delete', { source_id: 7 });
    expect(afterSuccess.idempotency_key).not.toBe(first.idempotency_key);

    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('network failed');
    }));
    await expect(session.withV1ActionResult(
      afterSuccess,
      () => undefined,
      { clearIdempotency: 'finally' },
    )).rejects.toThrow('network failed');
    const afterFailure = session.registeredProjectActionSpec('source.delete', { source_id: 7 });
    expect(afterFailure.idempotency_key).not.toBe(afterSuccess.idempotency_key);
  });
});
