import { describe, expect, it } from 'vitest';
import { nerReplayPlan } from '../../src/workbench/nerReplayModel';
import type { ColumnRun } from '../../src/api/open';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';

const entry = syntheticActionCatalogEntry('map.ner', { ui_hints: { form: 'generated' } });
const target = { sheetId: '7', columnId: '12', columnName: 'entities' };
function run(spec: Record<string, unknown>): ColumnRun {
  return {
    runId: '88', actionKind: 'map.ner', actionName: 'Extract entities', model: '',
    status: 'completed', spec, totalRows: 2, completedRows: 2, failedRows: 0,
    cost: 0, startedAt: null, finishedAt: null, durationMs: null, tokensIn: null,
    tokensOut: null, current: true,
  };
}
const params = { source: ['body'], labels: ['person'], engine: 'spacy', threshold: 0.4 };

describe('NER typed replay', () => {
  it('uses recorded Params, the existing output, and a fresh invocation identity', () => {
    const recorded = run({ params: { ...params, confirmed: true, idempotency_key: 'old', arbitrary: 'ignored' } });
    const first = nerReplayPlan(recorded, target, entry);
    const second = nerReplayPlan(recorded, target, entry);
    expect(first.kind).toBe('run');
    if (first.kind !== 'run' || second.kind !== 'run') throw new Error('missing replay');
    expect(first.request).toEqual({
      action_id: 'map.ner', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params, output_names: { entities: 'entities' }, replace_existing: true,
      idempotency_key: expect.any(String),
    });
    expect(first.request.idempotency_key).not.toBe(second.request.idempotency_key);
    expect(first.request).not.toHaveProperty('confirmed');
    expect(first.request.params).not.toHaveProperty('arbitrary');
  });

  it('preserves the saved template and explicitly selected model without inventing consent', () => {
    const saved = { ...params, source: { text: '{{body}} and {{notes}}' }, engine: 'llm',
      model: 'openai/gpt-4o-mini', extra_instructions: 'Ignore signatures.' };
    const result = nerReplayPlan(run({ params: saved }), target, entry);
    if (result.kind !== 'run') throw new Error(result.reason);
    expect(result.request.params).toEqual(saved);
    expect(result.request).not.toHaveProperty('confirmation');
    expect(result.request).not.toHaveProperty('row_ids');
  });

  it.each([
    null,
    { ...run({ params }), actionKind: 'map.ask' },
    run({ input_columns: ['body'], labels: ['person'] }),
    run({ params: { labels: ['person'] } }),
  ])('refuses unavailable or incomplete recorded NER Params', (recorded) => {
    expect(nerReplayPlan(recorded, target, entry).kind).toBe('refused');
  });

  it('refuses a missing catalog or output name', () => {
    expect(nerReplayPlan(run({ params }), target, null).kind).toBe('refused');
    expect(nerReplayPlan(run({ params }), { ...target, columnName: null }, entry).kind).toBe('refused');
  });
});
