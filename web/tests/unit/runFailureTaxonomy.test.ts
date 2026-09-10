// Failure-triage bucketing: RunRowErrorGroup message-groups aggregate into
// outcome buckets straight from the backend taxonomy fields (outcome/terminal
// on the wire) — never by parsing message text — and the `failed` grid-filter
// operator round-trips through the client filter-spec helpers.

import { describe, expect, it } from 'vitest';
import { bucketRowErrors, failureOutcomeLabel } from '../../src/runFailureTaxonomy';
import { gridFilterLabel, normalizeGridFilterSpec } from '../../src/workspace/gridColumnState';
import type { RunRowErrorGroup } from '../../src/api/open';



const group = (over: Partial<RunRowErrorGroup> = {}): RunRowErrorGroup => ({
  message: 'provider rate limited',
  count: 8,
  code: 'provider_rate_limited',
  outcome: 'model_error',
  terminal: false,
  rowIds: ['1', '2'],
  ...over,
});

describe('bucketRowErrors', () => {
  it('merges message groups sharing an outcome and sorts largest first', () => {
    const buckets = bucketRowErrors([
      group({ message: 'rate limited', count: 5 }),
      group({ message: 'server exploded', count: 3 }),
      group({
        message: 'no output for non-empty source',
        count: 4,
        code: 'empty_output',
        outcome: 'empty_output',
        terminal: true,
      }),
    ]);
    expect(buckets.map((b) => [b.outcome, b.count, b.terminal])).toEqual([
      ['model_error', 8, false],
      ['empty_output', 4, true],
    ]);
  });

  it('keeps an unclassified (null-outcome) bucket rather than dropping rows', () => {
    const buckets = bucketRowErrors([group({ outcome: null, count: 2 })]);
    expect(buckets).toHaveLength(1);
    expect(buckets[0].outcome).toBeNull();
    expect(buckets[0].label).toBe('failed');
  });

  it('labels taxonomy buckets and falls back readably for unknown outcomes', () => {
    expect(failureOutcomeLabel('model_error')).toBe('model errors');
    expect(failureOutcomeLabel('invalid_output')).toBe('invalid output');
    expect(failureOutcomeLabel('empty_output')).toBe('empty output');
    expect(failureOutcomeLabel('some_future_bucket')).toBe('some future bucket');
  });
});

describe("'failed' grid-filter operator client plumbing", () => {
  it('normalizeGridFilterSpec preserves a failed condition', () => {
    expect(normalizeGridFilterSpec({ result: { failed: 'any' } })).toEqual({
      result: { failed: 'any' },
    });
    expect(normalizeGridFilterSpec({ result: { failed: 'empty_output' } })).toEqual({
      result: { failed: 'empty_output' },
    });
  });

  it('gridFilterLabel words the failed filter honestly', () => {
    expect(gridFilterLabel({ result: { failed: 'any' } })).toBe('result failed cells');
    expect(gridFilterLabel({ result: { failed: 'empty_output' } })).toBe(
      'result failed: empty output',
    );
  });
});
