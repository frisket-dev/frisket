import { describe, expect, it, vi } from 'vitest';

import type {
  ActionComparisonApi,
  ActionComparisonCell,
  ActionComparisonContext,
} from '../../src/api/actionComparison';
import { ApiError, ConfirmationRequiredError } from '../../src/api/contractErrors';
import {
  actionCompareCellEquality,
  createActionCompareRunner,
  createActionCompareSession,
  type ActionCompareCostConfirmation,
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

function withFreeQuotes(api: ActionComparisonApi): ActionComparisonApi {
  return { ...api, quotePreview: api.quotePreview ?? vi.fn().mockResolvedValue({
    schema_version: 'frisket.action_compare_quote.v1', estimate: {
      cost: 0, rows: 2, billed_cost: 0, policy_id: 'test', cost_source: 'free_local', requires_confirmation: false, claims: [],
    },
  }) };
}

describe('action comparison session', () => {
  it('changes row scope while preserving configured drafts and requiring an explicit new run', async () => {
    const discardPreview = vi.fn(async () => undefined);
    const runVariant = vi.fn<ActionCompareVariantRun>(async () => ready('old-scope'));
    const session = createActionCompareSession({ context: context() }, { runVariant, discardPreview });
    const b = session.addVariant()!;
    session.updateVariant(b, { ...context().draft, params: { ...context().draft.params, model: 'chosen/model' } });
    await session.runPending();
    const shelf = session.restoreVariant({ savedId: 9, label: 'Earlier', draft: context().draft, result: { rows: {} } });
    const expanded = context(); expanded.sample_row_ids = [101, 102, 103];
    expect(await session.updateContext(expanded, { rowIds: [101, 102, 103], totalCount: 3 })).toBe(true);
    expect(session.getSnapshot().variants.find((variant) => variant.id === b)).toMatchObject({ status: 'pending', result: null, draft: { params: { model: 'chosen/model' } } });
    expect(session.getSnapshot().variants.find((variant) => variant.id === shelf)).toMatchObject({ savedId: 9, readOnly: true, status: 'ready' });
    expect(discardPreview).toHaveBeenCalledWith('old-scope');
    expect(runVariant).toHaveBeenCalledTimes(1);
    await session.runPending();
    expect(runVariant.mock.calls[1][0].context.sample_row_ids).toEqual([101, 102, 103]);
  });

  it('reuses the first free shelf letter after removal and bounds restored variants', () => {
    const session = createActionCompareSession({ context: context() }, { runVariant: vi.fn() });
    const saved = (id: number) => ({ savedId: id, label: `Saved ${id}`, draft: context().draft, result: { rows: {} } });
    const first = session.restoreVariant(saved(1))!;
    session.restoreVariant(saved(2)); session.removeVariant(first); session.restoreVariant(saved(3));
    expect(session.getSnapshot().variants.map((variant) => variant.displayKey)).toEqual(['A', 'S2', 'S']);
    for (let id = 4; id <= 32; id += 1) expect(session.restoreVariant(saved(id))).not.toBeNull();
    expect(session.getSnapshot().variants).toHaveLength(32);
    expect(session.restoreVariant(saved(33))).toBeNull();
    expect(session.addVariant()).toBeNull();
  });

  it('honors server capacity for five variants and retains the complete expected-row scope', async () => {
    const seed = context();
    seed.limits = { max_sample_rows: 100, max_challengers: 5 } as ActionComparisonContext['limits'];
    seed.sample_row_ids = Array.from({ length: 38 }, (_, index) => 101 + index);
    const runVariant = vi.fn<ActionCompareVariantRun>(async () => ready('expanded'));
    const session = createActionCompareSession({ context: seed }, { runVariant });
    for (let index = 0; index < 5; index += 1) expect(session.addVariant()).not.toBeNull();
    expect(session.getSnapshot().variants.map((variant) => variant.displayKey)).toEqual(['A', 'B', 'C', 'D', 'E', 'F']);
    expect(session.addVariant()).toBeNull();
    await session.runPending();
    expect(runVariant.mock.calls[0][0].context.sample_row_ids).toHaveLength(38);
  });

  it('restores shelf values as read-only variants that cannot run or change settings', async () => {
    const runVariant = vi.fn<ActionCompareVariantRun>();
    const session = createActionCompareSession({ context: context() }, { runVariant });
    const restored = session.restoreVariant({
      savedId: 19, label: 'Last week', draft: context().draft,
      result: { rows: { 101: { answer: valueCell('Old answer') } } },
    });
    expect(restored).not.toBeNull();
    expect(session.getSnapshot().variants[1]).toMatchObject({
      displayKey: 'S', readOnly: true, savedId: 19, status: 'ready',
      result: { rows: { 101: { answer: { value: 'Old answer' } } } },
    });
    expect(session.updateVariant(restored!, context().draft)).toBe(false);
    expect(session.retryVariant(restored!)).toBe(false);
    expect(session.useVariant(restored!)).toBeNull();
    await session.runPending();
    expect(runVariant).not.toHaveBeenCalled();
  });

  it('retains every preview chunk identity and discards each on removal', async () => {
    const discardPreview = vi.fn(async () => undefined);
    const session = createActionCompareSession({ context: context() }, {
      runVariant: async ({ onStarted }) => {
        onStarted('first-chunk'); onStarted('second-chunk');
        return ready('second-chunk');
      }, discardPreview,
    });
    const variantId = session.addVariant()!;
    await session.runPending();
    expect(session.getSnapshot().variants[1].previewIds).toEqual(['first-chunk', 'second-chunk']);
    session.removeVariant(variantId);
    await Promise.resolve();
    expect(discardPreview).toHaveBeenCalledWith('first-chunk');
    expect(discardPreview).toHaveBeenCalledWith('second-chunk');
  });

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
      variant.displayKey, variant.status, variant.previewId,
    ])).toEqual([
      ['A', 'current', null],
      ['B', 'pending', null],
      ['C', 'pending', null],
      ['D', 'pending', null],
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
      status: 'pending', draftRevision: 1,
    });

    pending.resolve(ready('preview-B', 'late result'));
    await running;
    expect(session.getSnapshot().variants[1]).toMatchObject({
      status: 'pending', result: null,
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

  it('lets pending admission resolve, then discards its known preview before disposal finishes', async () => {
    const admission = deferred<Awaited<ReturnType<ActionComparisonApi['startPreview']>>>();
    const discard = deferred<void>();
    const startPreview = vi.fn(() => admission.promise);
    const api = {
      startPreview,
      getPreview: vi.fn(),
      getContext: vi.fn(),
      cancelPreview: vi.fn(),
    } as unknown as ActionComparisonApi;
    const discardPreview = vi.fn(() => discard.promise);
    const session = createActionCompareSession(
      { context: context() },
      {
        runVariant: createActionCompareRunner(withFreeQuotes(api), vi.fn()),
        discardPreview,
      },
    );
    session.addVariant();
    const running = session.runPending();
    await vi.waitFor(() => expect(startPreview).toHaveBeenCalledTimes(1));

    const disposing = session.dispose();
    let disposed = false;
    void disposing.then(() => { disposed = true; });
    await Promise.resolve();
    expect(disposed).toBe(false);
    expect(startPreview).toHaveBeenCalledTimes(1);
    expect(startPreview.mock.calls[0]).toHaveLength(1);

    admission.resolve({
      schema_version: 'frisket.action_preview.v1', preview_id: 'admitted-after-stop', total: 2,
    });
    await vi.waitFor(() => expect(discardPreview).toHaveBeenCalledWith('admitted-after-stop'));
    expect(api.getPreview).not.toHaveBeenCalled();
    expect(disposed).toBe(false);
    discard.resolve();
    await disposing;
    await running;

    expect(session.getSnapshot()).toMatchObject({ disposed: true, variants: [] });
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

  it('marks only the exact ready preview unavailable and makes it retryable', async () => {
    const rerun = deferred<ActionCompareRunOutcome>();
    let attempt = 0;
    const session = createActionCompareSession(
      { context: context() },
      { runVariant: async ({ onStarted }) => {
        attempt += 1;
        if (attempt === 1) return ready('preview-B');
        onStarted('preview-new');
        return rerun.promise;
      } },
    );
    const b = session.addVariant()!;
    await session.runPending();

    expect(session.markPreviewUnavailable('preview-other')).toBe(false);
    expect(session.markPreviewUnavailable('preview-B')).toBe(true);
    expect(session.getSnapshot().variants.find((variant) => variant.id === b)).toMatchObject({
      status: 'unavailable',
      previewId: 'preview-B',
      result: null,
      previousResult: expect.any(Object),
      error: 'Preview no longer available; run this variant again.',
    });
    expect(session.retryVariant(b)).toBe(true);
    expect(session.getSnapshot().variants.find((variant) => variant.id === b)).toMatchObject({
      status: 'pending', previewId: null, error: null,
    });
    const running = session.runPending();
    await Promise.resolve();
    expect(session.markPreviewUnavailable('preview-B')).toBe(false);
    expect(session.getSnapshot().variants.find((variant) => variant.id === b)).toMatchObject({
      status: 'running', previewId: 'preview-new',
    });
    rerun.resolve(ready('preview-new'));
    await running;
  });
});

describe('actionCompareCellEquality', () => {
  it('preserves null/error/missing states and compares object keys independently of order', () => {
    const origin = valueCell('x').origin;
    expect(actionCompareCellEquality(valueCell(null), valueCell(null))).toBe('same');
    expect(actionCompareCellEquality(valueCell(null), valueCell('present'))).toBe('different');
    expect(actionCompareCellEquality(valueCell('present'), valueCell(null))).toBe('different');
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
  it('keeps a completed chunk when Stop arrives while its durable capture is saving', async () => {
    const capture = deferred<{ items: Array<{ id: number }> }>();
    const captureComparison = vi.fn(() => capture.promise);
    const api = withFreeQuotes({
      startPreview: vi.fn().mockResolvedValue({ schema_version: 'frisket.action_preview.v1', preview_id: 'done-before-stop', total: 2 }),
      getPreview: vi.fn().mockResolvedValue({ previewId: 'done-before-stop', status: 'done', progress: { done: 2, total: 2, preparation: null },
        rows: { 101: { answer: { state: 'value', value: 'Retained' } } }, rowIds: [101, 102], sampled: 2, total: 2, error: null, accounting: null }),
      cancelPreview: vi.fn(),
    } as unknown as ActionComparisonApi);
    const discardCaptured = vi.fn(async () => undefined);
    const runner = createActionCompareRunner(api, vi.fn(), { captureComparison, discardCapture: discardCaptured } as never);
    const session = createActionCompareSession({ context: context() }, { runVariant: runner, discardCaptured });
    session.addVariant();
    const running = session.runPending();
    await vi.waitFor(() => expect(captureComparison).toHaveBeenCalledTimes(1));
    session.stop();
    capture.resolve({ items: [{ id: 99 }] });
    await running;
    expect(session.getSnapshot().variants[1]).toMatchObject({ status: 'cancelled', savedId: 99, result: { rows: { 101: { answer: { value: 'Retained' } } } } });
    expect(discardCaptured).not.toHaveBeenCalled();
    await session.dispose();
    expect(discardCaptured).toHaveBeenCalledWith(99);
  });

  it('captures failed terminal previews so their miss scores survive scratch cleanup', async () => {
    const api = withFreeQuotes({
      startPreview: vi.fn().mockResolvedValue({ schema_version: 'frisket.action_preview.v1', preview_id: 'failed-output', total: 2 }),
      getPreview: vi.fn().mockResolvedValue({ previewId: 'failed-output', status: 'error', progress: { done: 0, total: 2, preparation: null },
        rows: {}, rowIds: [101, 102], sampled: 2, total: 2, error: { code: 'provider_failed', message: 'Provider failed' }, accounting: null }),
      cancelPreview: vi.fn(),
    } as unknown as ActionComparisonApi);
    const captureComparison = vi.fn().mockResolvedValue({ items: [{ id: 71 }] });
    const onCaptured = vi.fn();
    const variant = createActionCompareSession({ context: context('preview') }, { runVariant: vi.fn() }).getSnapshot().variants[0];
    const outcome = await createActionCompareRunner(api, vi.fn(), { captureComparison, discardCapture: vi.fn() } as never)({ context: context('preview'), variant, signal: new AbortController().signal, onStarted: vi.fn(), onCaptured });
    expect(outcome).toMatchObject({ status: 'failed', error: 'Provider failed' });
    expect(captureComparison).toHaveBeenCalledWith(expect.objectContaining({ comparison: expect.objectContaining({ variants: [expect.objectContaining({ source: { kind: 'preview', preview_ids: ['failed-output'] } })] }) }));
    expect(onCaptured).toHaveBeenCalledWith(71);
  });

  it('retains each completed chunk durably before starting the next chunk', async () => {
    const seed = context('preview');
    seed.sample_row_ids = Array.from({ length: 38 }, (_, index) => 101 + index);
    const requests: number[][] = [];
    const events: string[] = [];
    const api = withFreeQuotes({
      startPreview: vi.fn(async ({ action }: Parameters<ActionComparisonApi['startPreview']>[0]) => {
        const rows = action.scope.kind === 'sheet_rows' ? action.scope.row_ids ?? [] : [];
        requests.push(rows); events.push(`start-${requests.length}`);
        return { schema_version: 'frisket.action_preview.v1', preview_id: `chunk-${requests.length}`, total: rows.length };
      }),
      getPreview: vi.fn(async (id: string) => ({ previewId: id, status: 'done', progress: { done: 20, total: 20, preparation: null },
        rows: Object.fromEntries(requests[Number(id.split('-')[1]) - 1].map((rowId) => [rowId, { answer: valueCell(`Row ${rowId}`) }])), rowIds: [], sampled: 20, total: 20, error: null, accounting: null })),
      cancelPreview: vi.fn(),
    } as unknown as ActionComparisonApi);
    const captureComparison = vi.fn(async () => { events.push(`capture-${requests.length}`); return { items: [{ id: 77 }] }; });
    const onCaptured = vi.fn();
    const variant = createActionCompareSession({ context: seed }, { runVariant: vi.fn() }).getSnapshot().variants[0];
    const outcome = await createActionCompareRunner(api, vi.fn(), { captureComparison, discardCapture: vi.fn() } as never)({ context: seed, variant, signal: new AbortController().signal, onStarted: vi.fn(), onCaptured });
    expect(outcome.status).toBe('ready');
    expect(events).toEqual(['start-1', 'capture-1', 'start-2', 'capture-2']);
    expect(captureComparison.mock.calls[0][0]).not.toHaveProperty('target_id');
    expect(captureComparison.mock.calls[1][0]).toMatchObject({ target_id: 77, comparison: { row_ids: requests[1] } });
    expect(onCaptured.mock.calls).toEqual([[77], [77]]);
  });

  it('stops the approved sequence on a new 402 without accepting replacement authority', async () => {
    const estimate = { cost: 0.1, rows: 2, billed_cost: 100000, policy_id: 'paid', cost_source: 'external', requires_confirmation: true, promise_set_hash: 'quoted' };
    const api = { quotePreview: vi.fn().mockResolvedValue({ estimate }),
      startPreview: vi.fn().mockRejectedValue(new ConfirmationRequiredError({ ...estimate, promise_set_hash: 'changed' }, 'New cost')),
      getPreview: vi.fn(), cancelPreview: vi.fn() } as unknown as ActionComparisonApi;
    const confirm = vi.fn().mockResolvedValue(true);
    const session = createActionCompareSession({ context: context() }, { runVariant: createActionCompareRunner(api, confirm) });
    session.addVariant(); session.addVariant();
    await session.runPending();
    expect(api.quotePreview).toHaveBeenCalledTimes(2);
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(api.startPreview).toHaveBeenCalledTimes(1);
    expect(session.getSnapshot().sequenceStatus).toBe('cancelled');
    expect(session.getSnapshot().variants.slice(1).map((variant) => variant.status)).toEqual(['failed', 'pending']);
  });

  it('runs 38 expected rows in ordinary preview chunks and merges their results', async () => {
    const seed = context('preview');
    seed.sample_row_ids = Array.from({ length: 38 }, (_, index) => 101 + index);
    const requests: number[][] = [];
    const api = {
      getContext: vi.fn(),
      startPreview: vi.fn(async ({ action }: Parameters<ActionComparisonApi['startPreview']>[0]) => {
        const rows = action.scope.row_ids ?? [];
        requests.push(rows);
        return { schema_version: 'frisket.action_preview.v1', preview_id: `chunk-${requests.length}`, total: rows.length };
      }),
      getPreview: vi.fn(async (id: string) => ({
        previewId: id, status: 'done', progress: { done: 20, total: 20, preparation: null },
        rows: Object.fromEntries(requests[Number(id.split('-')[1]) - 1].map((rowId) => [rowId, { answer: valueCell(`Row ${rowId}`) }])),
        rowIds: [], sampled: 20, total: 20, error: null, accounting: null,
      })), cancelPreview: vi.fn(),
    } as unknown as ActionComparisonApi;
    const onStarted = vi.fn();
    const variant = createActionCompareSession({ context: seed }, { runVariant: vi.fn() }).getSnapshot().variants[0];
    const outcome = await createActionCompareRunner(withFreeQuotes(api), vi.fn())({ context: seed, variant, signal: new AbortController().signal, onStarted });
    expect(requests.map((rows) => rows.length)).toEqual([20, 18]);
    expect(requests.flat()).toEqual(seed.sample_row_ids);
    expect(onStarted.mock.calls.map(([id]) => id)).toEqual(['chunk-1', 'chunk-2']);
    expect(outcome.status).toBe('ready');
    if (outcome.status === 'ready') expect(Object.keys(outcome.result.rows)).toHaveLength(38);
  });

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
    const runner = createActionCompareRunner(withFreeQuotes(api), confirm);

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
      },
    }));
    expect(api.getContext).not.toHaveBeenCalled();
  });

  it('normalizes renamed physical preview columns to logical output keys', async () => {
    const renamed = context('preview');
    renamed.draft.output_names = { answer: 'renamed answer' };
    renamed.comparison_basis = { kind: 'draft', draft: renamed.draft };
    renamed.output_columns[0].current_name = 'renamed answer';
    const api = {
      getContext: vi.fn().mockResolvedValue(renamed),
      startPreview: vi.fn().mockResolvedValue({
        schema_version: 'frisket.action_preview.v1', preview_id: 'renamed-preview', total: 2,
      }),
      getPreview: vi.fn().mockResolvedValue({
        previewId: 'renamed-preview', status: 'done',
        progress: { done: 2, total: 2, preparation: null },
        rows: {
          101: { 'renamed answer': { state: 'value', value: 'Alpha' } },
          102: { 'renamed answer': { state: 'null' } },
        },
        rowIds: [101, 102], sampled: 2, total: 2, error: null, accounting: null,
      }),
      cancelPreview: vi.fn(),
    } as unknown as ActionComparisonApi;
    const variant = createActionCompareSession({ context: renamed }, { runVariant: vi.fn() })
      .getSnapshot().variants[0];

    const outcome = await createActionCompareRunner(withFreeQuotes(api), vi.fn())({
      context: renamed, variant, signal: new AbortController().signal, onStarted: vi.fn(),
    });

    expect(outcome).toMatchObject({
      status: 'ready',
      result: {
        rows: {
          101: { answer: { state: 'value', value: 'Alpha' } },
          102: { answer: { state: 'null' } },
        },
      },
    });
  });

  it('refuses preview output names that are unknown or ambiguous in context', async () => {
    const base = context('preview');
    const api = {
      getContext: vi.fn(),
      startPreview: vi.fn().mockResolvedValue({
        schema_version: 'frisket.action_preview.v1', preview_id: 'bad-preview', total: 1,
      }),
      getPreview: vi.fn().mockResolvedValue({
        previewId: 'bad-preview', status: 'done',
        progress: { done: 1, total: 1, preparation: null },
        rows: { 101: { stranger: { state: 'value', value: 'unexpected' } } },
        rowIds: [101], sampled: 1, total: 1, error: null, accounting: null,
      }),
      cancelPreview: vi.fn(),
    } as unknown as ActionComparisonApi;
    const variant = createActionCompareSession({ context: base }, { runVariant: vi.fn() })
      .getSnapshot().variants[0];

    const unknown = await createActionCompareRunner(withFreeQuotes(api), vi.fn())({
      context: base, variant, signal: new AbortController().signal, onStarted: vi.fn(),
    });
    expect(unknown).toEqual({
      status: 'failed', previewId: 'bad-preview',
      error: 'Comparison preview returned unexpected output "stranger".',
    });
    expect(api.getContext).not.toHaveBeenCalled();

    const ambiguous = context('preview');
    ambiguous.output_columns.push({
      ...ambiguous.output_columns[0], logical_key: 'reason', current_name: 'answer',
    });
    const ambiguousVariant = createActionCompareSession(
      { context: ambiguous }, { runVariant: vi.fn() },
    ).getSnapshot().variants[0];
    const collision = await createActionCompareRunner(withFreeQuotes(api), vi.fn())({
      context: ambiguous, variant: ambiguousVariant,
      signal: new AbortController().signal, onStarted: vi.fn(),
    });
    expect(collision).toEqual({
      status: 'failed', previewId: 'bad-preview',
      error: 'Comparison context maps "answer" to multiple outputs.',
    });
  });

  it('approves quoted work once and sends each exact request with its own hash', async () => {
    const estimate = { cost: 0.12, rows: 2, billed_cost: 120000, policy_id: 'paid', requires_confirmation: true, promise_set_hash: 'exact-hash' };
    const api = {
      getContext: vi.fn().mockResolvedValue(context('preview')),
      quotePreview: vi.fn().mockResolvedValue({ schema_version: 'frisket.action_compare_quote.v1', estimate }),
      startPreview: vi.fn().mockResolvedValue({ schema_version: 'frisket.action_preview.v1', preview_id: 'p2', total: 2 }),
      getPreview: vi.fn().mockResolvedValue({
        previewId: 'p2', status: 'done', progress: { done: 2, total: 2, preparation: null },
        rows: {}, rowIds: [101, 102], sampled: 2, total: 2, error: null, accounting: null,
      }),
      cancelPreview: vi.fn(),
    } as unknown as ActionComparisonApi;
    const confirm = vi.fn().mockResolvedValue(true);
    const runner = createActionCompareRunner(withFreeQuotes(api), confirm);
    const variant = createActionCompareSession({ context: context('preview') }, { runVariant: vi.fn() })
      .getSnapshot().variants[0];

    await runner({ context: context('preview'), variant, signal: new AbortController().signal, onStarted: vi.fn() });

    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ billed_cost: 120000, rows: 2 }), expect.any(String), expect.anything());
    expect(confirm.mock.calls[0][2]).toBeInstanceOf(AbortSignal);
    expect(confirm.mock.calls[0][2].aborted).toBe(false);
    const quoted = api.quotePreview.mock.calls[0][0].action;
    const started = api.startPreview.mock.calls[0][0].action;
    expect(started).toEqual({ ...quoted, confirmation: 'exact-hash' });
    expect(api.startPreview).toHaveBeenCalledTimes(1);

    const declinedApi = {
      ...api,
      startPreview: vi.fn().mockRejectedValue(new ConfirmationRequiredError(estimate, 'Approve')),
      getPreview: vi.fn(),
      cancelPreview: vi.fn(),
    } as unknown as ActionComparisonApi;
    const declined = await createActionCompareRunner(withFreeQuotes(declinedApi), vi.fn().mockResolvedValue(false))({
      context: context('preview'), variant, signal: new AbortController().signal, onStarted: vi.fn(),
    });
    expect(declined).toEqual({ status: 'cancelled' });
    expect(declinedApi.startPreview).not.toHaveBeenCalled();
    expect(declinedApi.getPreview).not.toHaveBeenCalled();
  });

  it('aborts a pending cost confirmation without retrying admission', async () => {
    const estimate = { cost: 0.12, rows: 2, billed_cost: 120000, requires_confirmation: true, promise_set_hash: 'exact-hash' };
    const api = {
      getContext: vi.fn(),
      quotePreview: vi.fn().mockResolvedValue({ schema_version: 'frisket.action_compare_quote.v1', estimate }),
      startPreview: vi.fn().mockRejectedValue(new ConfirmationRequiredError(estimate, 'Approve')),
      getPreview: vi.fn(),
      cancelPreview: vi.fn(),
    } as unknown as ActionComparisonApi;
    let confirmationSignal: AbortSignal | null = null;
    const confirmationEntered = deferred<void>();
    const confirm = vi.fn<ActionCompareCostConfirmation>((_estimate, _message, signal) => {
      confirmationSignal = signal;
      confirmationEntered.resolve();
      return new Promise<boolean>((resolve) => {
        signal.addEventListener('abort', () => resolve(false), { once: true });
      });
    });
    const session = createActionCompareSession(
      { context: context() },
      { runVariant: createActionCompareRunner(withFreeQuotes(api), confirm) },
    );
    session.addVariant();
    const running = session.runPending();
    await confirmationEntered.promise;

    await session.dispose();
    await running;

    expect(confirmationSignal).not.toBeNull();
    expect(confirmationSignal!.aborted).toBe(true);
    expect(api.startPreview).not.toHaveBeenCalled();
    expect(api.getPreview).not.toHaveBeenCalled();
  });

  it('makes a disappeared known preview retryable without masking authorization failures', async () => {
    vi.useFakeTimers();
    try {
      const unavailableApi = {
        getContext: vi.fn(),
        startPreview: vi.fn().mockResolvedValue({
          schema_version: 'frisket.action_preview.v1', preview_id: 'gone-preview', total: 2,
        }),
        getPreview: vi.fn().mockRejectedValue(new ApiError(404, 'Preview not found.')),
        cancelPreview: vi.fn().mockResolvedValue(undefined),
      } as unknown as ActionComparisonApi;
      const variant = createActionCompareSession(
        { context: context('preview') }, { runVariant: vi.fn() },
      ).getSnapshot().variants[0];
      const unavailable = createActionCompareRunner(withFreeQuotes(unavailableApi), vi.fn())({
        context: context('preview'), variant, signal: new AbortController().signal,
        onStarted: vi.fn(),
      });
      const unavailableOutcome = expect(unavailable).resolves.toEqual({
        status: 'unavailable', previewId: 'gone-preview',
        error: 'Preview no longer available; run this variant again.',
      });
      await vi.advanceTimersByTimeAsync(1_000);
      await unavailableOutcome;

      const forbiddenApi = {
        ...unavailableApi,
        startPreview: vi.fn().mockResolvedValue({
          schema_version: 'frisket.action_preview.v1', preview_id: 'forbidden-preview', total: 2,
        }),
        getPreview: vi.fn().mockRejectedValue(new ApiError(403, 'Not allowed.')),
      } as unknown as ActionComparisonApi;
      const forbidden = expect(createActionCompareRunner(withFreeQuotes(forbiddenApi), vi.fn())({
        context: context('preview'), variant, signal: new AbortController().signal,
        onStarted: vi.fn(),
      })).rejects.toMatchObject({ status: 403, message: 'Not allowed.' });
      await vi.advanceTimersByTimeAsync(1_000);
      await forbidden;
    } finally {
      vi.useRealTimers();
    }
  });
});
