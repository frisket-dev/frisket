import { describe, expect, it, vi } from 'vitest';

import type {
  ActionComparisonApi,
  ActionComparisonCell,
  ActionComparisonContext,
} from '../../src/api/actionComparison';
import { ConfirmationRequiredError } from '../../src/api/contractErrors';
import {
  actionCompareCellEquality,
  createActionCompareRunner,
  createActionCompareSession,
  type ActionCompareRunOutcome,
  type ActionCompareVariantRun,
} from '../../src/workbench/actionCompareSession';

function valueCell(value: string | number | boolean | null): ActionComparisonCell {
  return {
    state: value === null ? 'null' : 'value',
    ...(value === null ? {} : { value }),
    origin: {
      kind: 'run_result',
      run_id: 41,
      op_id: 9,
      review_decision: null,
    },
  };
}

function context(baselineMode: 'preview' | 'current_values' = 'current_values'): ActionComparisonContext {
  const draft = {
    action_id: 'map.extract',
    scope: { kind: 'sheet_rows' as const, sheet_id: 7 },
    params: { source: 'body', model: 'local/qwen' },
    output_names: { answer: 'answer' },
  };
  return {
    schema_version: 'frisket.action_compare_context.v1',
    available: true,
    reason: null,
    limits: { max_sample_rows: 20, max_challengers: 3 },
    baseline_mode: baselineMode,
    comparison_basis: baselineMode === 'preview'
      ? { kind: 'draft', draft }
      : { kind: 'run', run_id: 41, column_id: 13 },
    draft,
    sheet: { id: 7, name: 'Court filings' },
    sample_row_ids: [101, 102],
    sample_fingerprint: 'sample-v1',
    locked_params: ['source'],
    source_columns: [{
      param: 'source', column_id: 11, name: 'body', column_type: 'text', format: null,
    }],
    source_rows: {
      101: { body: valueCell('Alpha source') },
      102: { body: valueCell('Beta source') },
    },
    output_columns: [{
      logical_key: 'answer', column_id: 13, current_name: 'answer',
      column_type: 'text', format: null, hidden: false, schema: null,
    }],
    expected_output_signature: 'output-v1',
    replacement_targets: [{ logical_key: 'answer', current_name: 'answer', column_id: 13 }],
    baseline_rows: baselineMode === 'current_values'
      ? { 101: { answer: valueCell('Alpha') }, 102: { answer: valueCell(null) } }
      : null,
    origin_summary: {
      mixed_runs: false,
      includes_manual_edits: false,
      includes_reject_clear: false,
    },
  };
}

function ready(previewId: string, value = 'Result'): ActionCompareRunOutcome {
  return {
    status: 'ready',
    previewId,
    result: {
      rows: {
        101: { answer: valueCell(value) },
        102: { answer: valueCell(null) },
      },
    },
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe('action comparison session', () => {
  it('seeds A without executing, clones only settings, and enforces A plus three challengers', () => {
    const runVariant = vi.fn<ActionCompareVariantRun>();
    const session = createActionCompareSession({ context: context() }, { runVariant });

    expect(session.getSnapshot().variants).toMatchObject([
      { displayKey: 'A', baseline: true, status: 'current', previewId: null },
    ]);
    expect(runVariant).not.toHaveBeenCalled();

    const b = session.addVariant();
    const c = session.addVariant(b!);
    session.addVariant(c!);
    expect(session.addVariant()).toBeNull();
    expect(session.getSnapshot().variants.map((variant) => [
      variant.displayKey, variant.status, variant.previewId, variant.resultRevision,
    ])).toEqual([
      ['A', 'current', null, 0],
      ['B', 'pending', null, null],
      ['C', 'pending', null, null],
      ['D', 'pending', null, null],
    ]);
  });

  it('runs pending variants sequentially and keeps failures independent', async () => {
    const first = deferred<ActionCompareRunOutcome>();
    const calls: string[] = [];
    const runVariant: ActionCompareVariantRun = vi.fn(async ({ variant, onStarted }) => {
      calls.push(variant.displayKey);
      onStarted(`preview-${variant.displayKey}`);
      if (variant.displayKey === 'B') return first.promise;
      if (variant.displayKey === 'C') return { status: 'failed', previewId: 'preview-C', error: 'No model' };
      return ready('preview-D', 'D result');
    });
    const session = createActionCompareSession({ context: context() }, { runVariant });
    session.addVariant();
    session.addVariant();
    session.addVariant();

    const running = session.runPending();
    await Promise.resolve();
    expect(calls).toEqual(['B']);
    expect(session.getSnapshot().variants[1]).toMatchObject({ status: 'running', previewId: 'preview-B' });

    first.resolve(ready('preview-B', 'B result'));
    await running;

    expect(calls).toEqual(['B', 'C', 'D']);
    expect(session.getSnapshot().variants.slice(1)).toMatchObject([
      { status: 'ready', error: null },
      { status: 'failed', error: 'No model' },
      { status: 'ready', error: null },
    ]);
  });

  it('fences a late completion after editing and cancels only the affected preview', async () => {
    const pending = deferred<ActionCompareRunOutcome>();
    const cancelPreview = vi.fn(async () => undefined);
    const session = createActionCompareSession(
      { context: context() },
      {
        runVariant: async ({ onStarted }) => {
          onStarted('preview-B');
          return pending.promise;
        },
        cancelPreview,
      },
    );
    const b = session.addVariant()!;
    const running = session.runPending();
    await Promise.resolve();

    const changed = {
      ...session.getSnapshot().variants[1].draft,
      params: { ...session.getSnapshot().variants[1].draft.params, model: 'cloud/sonnet' },
    };
    session.updateVariant(b, changed);
    expect(cancelPreview).toHaveBeenCalledWith('preview-B');
    expect(session.getSnapshot().variants[1]).toMatchObject({
      status: 'pending', draftRevision: 1, resultRevision: null,
    });

    pending.resolve(ready('preview-B', 'late result'));
    await running;
    expect(session.getSnapshot().variants[1]).toMatchObject({
      status: 'pending', result: null, resultRevision: null,
    });
  });

  it('stops the whole sequence when the active preview is cancelled externally', async () => {
    const calls: string[] = [];
    const session = createActionCompareSession(
      { context: context() },
      {
        runVariant: async ({ variant, onStarted }) => {
          calls.push(variant.displayKey);
          onStarted(`preview-${variant.displayKey}`);
          return { status: 'cancelled', previewId: `preview-${variant.displayKey}` };
        },
      },
    );
    session.addVariant();
    session.addVariant();

    await session.runPending();

    expect(calls).toEqual(['B']);
    expect(session.getSnapshot()).toMatchObject({ sequenceStatus: 'cancelled' });
    expect(session.getSnapshot().variants.slice(1)).toMatchObject([
      { status: 'cancelled' },
      { status: 'pending' },
    ]);
  });

  it('cancels, joins, and discards before an approved retarget', async () => {
    const pending = deferred<ActionCompareRunOutcome>();
    const cancelPreview = vi.fn(async () => undefined);
    const discardPreview = vi.fn(async () => undefined);
    const session = createActionCompareSession(
      { context: context() },
      {
        runVariant: async ({ onStarted }) => {
          onStarted('preview-B');
          return pending.promise;
        },
        cancelPreview,
        discardPreview,
      },
    );
    session.addVariant();
    const running = session.runPending();
    await Promise.resolve();

    expect(await session.retarget({ context: context('preview') }, async () => false)).toBe(false);
    expect(session.getSnapshot().context.baseline_mode).toBe('current_values');

    const retargeted = session.retarget({ context: context('preview') }, async () => true);
    pending.resolve({ status: 'cancelled', previewId: 'preview-B' });
    expect(await retargeted).toBe(true);
    await running;

    expect(cancelPreview).toHaveBeenCalledWith('preview-B');
    expect(discardPreview).toHaveBeenCalledWith('preview-B');
    expect(session.getSnapshot()).toMatchObject({
      context: { baseline_mode: 'preview' },
      variants: [{ displayKey: 'A', status: 'pending' }],
    });
  });

  it('returns settings only for a usable evaluated variant', async () => {
    const existing = createActionCompareSession(
      { context: context() },
      { runVariant: async () => ready('preview-B') },
    );
    expect(existing.useVariant(existing.getSnapshot().variants[0].id)).toBeNull();
    const b = existing.addVariant()!;
    expect(existing.useVariant(b)).toBeNull();
    await existing.runPending();
    expect(existing.useVariant(b)).toMatchObject({
      variantId: b,
      variantLabel: 'B',
      context: { baseline_mode: 'current_values' },
    });

    const fresh = createActionCompareSession(
      { context: context('preview') },
      { runVariant: async () => ready('preview-A') },
    );
    await fresh.runPending();
    expect(fresh.useVariant(fresh.getSnapshot().variants[0].id)).toMatchObject({ variantLabel: 'A' });
  });
});

describe('actionCompareCellEquality', () => {
  it('preserves null/error/missing states and compares object keys independently of order', () => {
    const origin = valueCell('x').origin;
    expect(actionCompareCellEquality(valueCell(null), valueCell(null))).toBe('same');
    expect(actionCompareCellEquality(
      { state: 'value', value: { a: 1, b: [2, 3] }, origin },
      { state: 'value', value: { b: [2, 3], a: 1 }, origin },
    )).toBe('same');
    expect(actionCompareCellEquality(
      { state: 'value', value: [2, 3], origin },
      { state: 'value', value: [3, 2], origin },
    )).toBe('different');
    expect(actionCompareCellEquality(
      { state: 'error', error: 'failed', origin },
      valueCell(null),
    )).toBe('not_comparable');
    expect(actionCompareCellEquality(
      {
        state: 'value', value: null, origin,
        bounded_value: {
          kind: 'preview_artifact', artifact_id: 'a1', href: '/artifact/a1', preview: 'same prefix',
          value_kind: 'text', total_characters: 100_000, total_bytes: 100_000, truncated: true,
        },
      },
      valueCell('same prefix'),
    )).toBe('not_comparable');
  });
});

describe('createActionCompareRunner', () => {
  it('executes free starts immediately and polls the exact sample request', async () => {
    const api = {
      getContext: vi.fn().mockResolvedValue(context('preview')),
      startPreview: vi.fn().mockResolvedValue({
        schema_version: 'frisket.action_preview.v1', preview_id: 'p1', total: 2,
      }),
      getPreview: vi.fn().mockResolvedValue({
        previewId: 'p1', status: 'done', progress: { done: 2, total: 2, preparation: null },
        rows: { 101: { answer: { state: 'value', value: 'ok' } } },
        rowIds: [101, 102], sampled: 2, total: 2, error: null, accounting: null,
      }),
      cancelPreview: vi.fn(),
    } as unknown as ActionComparisonApi;
    const confirm = vi.fn();
    const started = vi.fn();
    const runner = createActionCompareRunner(api, confirm);

    const outcome = await runner({
      context: context('preview'),
      variant: createActionCompareSession({ context: context('preview') }, { runVariant: vi.fn() })
        .getSnapshot().variants[0],
      signal: new AbortController().signal,
      onStarted: started,
    });

    expect(outcome).toMatchObject({ status: 'ready', previewId: 'p1' });
    expect(confirm).not.toHaveBeenCalled();
    expect(started).toHaveBeenCalledWith('p1');
    expect(api.startPreview).toHaveBeenCalledWith(expect.objectContaining({
      action: expect.objectContaining({
        action_id: 'map.extract', scope: expect.objectContaining({ row_ids: [101, 102] }),
      }),
      comparison: {
        basis: context('preview').comparison_basis,
        expected_output_signature: 'output-v1',
        expected_sample_fingerprint: 'sample-v1',
      },
    }), expect.anything());
    expect(api.getContext).toHaveBeenCalledWith({
      source: context('preview').comparison_basis,
      sample_row_ids: [101, 102],
    }, expect.anything());
  });

  it('retries only the gated variant with the exact 402 hash and stops when confirmation is declined', async () => {
    const estimate = { cost: 0.12, rows: 2, promise_set_hash: 'exact-hash' };
    const api = {
      getContext: vi.fn().mockResolvedValue(context('preview')),
      startPreview: vi.fn()
        .mockRejectedValueOnce(new ConfirmationRequiredError(estimate, 'Approve this preview'))
        .mockResolvedValueOnce({ schema_version: 'frisket.action_preview.v1', preview_id: 'p2', total: 2 }),
      getPreview: vi.fn().mockResolvedValue({
        previewId: 'p2', status: 'done', progress: { done: 2, total: 2, preparation: null },
        rows: {}, rowIds: [101, 102], sampled: 2, total: 2, error: null, accounting: null,
      }),
      cancelPreview: vi.fn(),
    } as unknown as ActionComparisonApi;
    const confirm = vi.fn().mockResolvedValue(true);
    const runner = createActionCompareRunner(api, confirm);
    const variant = createActionCompareSession({ context: context('preview') }, { runVariant: vi.fn() })
      .getSnapshot().variants[0];

    await runner({ context: context('preview'), variant, signal: new AbortController().signal, onStarted: vi.fn() });

    expect(confirm).toHaveBeenCalledWith(estimate, 'Approve this preview');
    const first = api.startPreview.mock.calls[0][0].action;
    const retry = api.startPreview.mock.calls[1][0].action;
    expect(retry).toEqual({ ...first, confirmation: 'exact-hash' });
    expect(retry.idempotency_key).toBe(first.idempotency_key);

    const declinedApi = {
      ...api,
      startPreview: vi.fn().mockRejectedValue(new ConfirmationRequiredError(estimate, 'Approve')),
      getPreview: vi.fn(),
      cancelPreview: vi.fn(),
    } as unknown as ActionComparisonApi;
    const declined = await createActionCompareRunner(declinedApi, vi.fn().mockResolvedValue(false))({
      context: context('preview'), variant, signal: new AbortController().signal, onStarted: vi.fn(),
    });
    expect(declined).toEqual({ status: 'cancelled' });
    expect(declinedApi.getPreview).not.toHaveBeenCalled();
  });
});
