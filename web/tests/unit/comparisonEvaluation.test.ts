import { describe, expect, it } from 'vitest';
import type { EvaluationResponse, EvaluationVariant } from '../../src/api/evaluation';
import { disagreementRows, expectedValueFromText, filteredEvaluationRows, groupedAnswers } from '../../src/workbench/evaluation/comparisonEvaluation';

const variant = (key: string, rows: EvaluationVariant['rows'], verdicts: EvaluationVariant['verdicts']): EvaluationVariant => ({
  key, label: key, source: { kind: 'current' }, settings: {}, rows, verdicts, source_fingerprints: {}, scores: {},
});
function response(): EvaluationResponse {
  return { sheet_id: 7, action_id: 'map.extract', link: null, row_ids: [1, 2, 3], output_columns: [], expected: {}, rule_previews: {}, best: { answer: ['B'] },
    variants: [
      variant('A', { 1: { answer: { state: 'value', value: 'wrong' } }, 2: { answer: { state: 'error', error: 'Provider failed' } } },
        { 1: { answer: 'miss' }, 2: { answer: 'miss' }, 3: { answer: 'not_run' } }),
      variant('B', { 1: { answer: { state: 'value', value: 'right' } }, 2: { answer: { state: 'value', value: 'right' } } },
        { 1: { answer: 'match' }, 2: { answer: 'match' }, 3: { answer: 'not_run' } }),
      variant('E', { 1: { answer: { state: 'value', value: 'wrong' } }, 2: { answer: { state: 'value', value: 'right' } } },
        { 1: { answer: 'miss' }, 2: { answer: 'match' }, 3: { answer: 'not_run' } }),
    ] };
}
describe('comparison evaluation projections', () => {
  it('groups each distinct answer once while preserving its variant letters', () => {
    const groups = groupedAnswers(response(), 1, 'answer');
    expect(groups.map((group) => [group.answerText, group.variantKeys])).toEqual([['wrong', ['A', 'E']], ['right', ['B']]]);
    expect(groupedAnswers(response(), 2, 'answer')[0]).toMatchObject({ failed: true, canSelect: false });
  });
  it('filters misses using verdicts and excludes unavailable rows from disagreement work', () => {
    const result = response();
    expect(filteredEvaluationRows(result, 'answer', 'any_miss')).toEqual([1, 2]);
    expect(filteredEvaluationRows(result, 'answer', 'best_misses')).toEqual([]);
    expect(filteredEvaluationRows(result, 'answer', 'variant:A')).toEqual([1, 2]);
    expect(filteredEvaluationRows(result, 'answer', 'everyone_misses')).toEqual([]);
    expect(disagreementRows(result, 'answer')).toEqual([1]);
  });
  it('uses full-value hashes to distinguish equal excerpts and includes them in disagreements', () => {
    const result = response();
    result.row_ids = [1];
    result.variants = result.variants.slice(0, 2);
    for (const entry of result.variants) entry.rows['1'].answer = { state: 'value', value: null,
      bounded_value: { preview: 'same prefix', total_characters: 100000, total_bytes: 100000, truncated: true }, value_hash: entry.key };
    expect(groupedAnswers(result, 1, 'answer')).toHaveLength(2);
    expect(groupedAnswers(result, 1, 'answer').every((group) => !group.canSelect)).toBe(true);
    expect(disagreementRows(result, 'answer')).toEqual([1]);
    result.variants[1].rows['1'].answer.value_hash = 'A';
    expect(groupedAnswers(result, 1, 'answer')).toHaveLength(1);
  });
  it('retains typed expected values and distinguishes unchecked from intentional empty', () => {
    expect(expectedValueFromText('', 'before', 'text')).toBeNull();
    expect(expectedValueFromText('[empty]', undefined, 'text')).toBe('[empty]');
    expect(expectedValueFromText('["A","B"]', [], 'json')).toEqual(['A', 'B']);
    expect(expectedValueFromText('925000', 1, 'number')).toBe(925000);
    expect(expectedValueFromText('$925,000', 1, 'number')).toBe('$925,000');
    expect(expectedValueFromText('925000', undefined, 'text')).toBe('925000');
  });
});
