import type { EvaluationBoundedValue, EvaluationRequest, EvaluationResponse, EvaluationRule, EvaluationVerdict } from '../../api/evaluation';
import type { JsonValue } from '../../api/types';
import { canonicalJson } from '../../actions/canonicalJson';
import type { ActionCompareSessionSnapshot } from '../actionCompareSession';

export type EvaluationRowFilter = 'all' | 'any_miss' | 'best_misses' | 'everyone_misses' | `variant:${string}`;
export const MATCH_RULES: Array<{ value: EvaluationRule; label: string }> = [
  { value: 'exact', label: 'Exact' }, { value: 'ignore_case', label: 'Ignore case' },
  { value: 'number', label: 'Same number' }, { value: 'date', label: 'Same date' },
  { value: 'set', label: 'Same items' }, { value: 'hand', label: 'Score by hand' },
];

export function comparisonRequest(state: ActionCompareSessionSnapshot, linkId: number | null): EvaluationRequest {
  return {
    basis: state.context.comparison_basis, row_ids: [...state.context.sample_row_ids], link_id: linkId,
    variants: state.variants.flatMap<EvaluationRequest['variants'][number]>((variant) => {
      if (variant.savedId !== undefined && ['ready', 'failed', 'cancelled'].includes(variant.status)) return [{ key: variant.displayKey, label: variant.label,
        source: { kind: 'saved' as const, saved_id: variant.savedId } }];
      if (variant.status === 'current') return [{ key: variant.displayKey, label: variant.label, source: { kind: 'current' as const } }];
      const ids = variant.previewIds ?? (variant.previewId ? [variant.previewId] : []);
      if (!ids.length || !['ready', 'failed'].includes(variant.status)) return [];
      return [{ key: variant.displayKey, label: variant.label, source: { kind: 'preview' as const, preview_ids: [...ids] } }];
    }),
  };
}

export function answerText(value: JsonValue | undefined): string {
  if (value === undefined || value === null) return '';
  return typeof value === 'string' ? value : JSON.stringify(value);
}

export function expectedValueFromText(text: string, existing: JsonValue | undefined, columnType: string): JsonValue {
  if (text === '') return null;
  if (text === '[empty]') return text;
  if ((existing !== undefined && existing !== null && typeof existing !== 'string') || ['json', 'number', 'integer', 'boolean'].includes(columnType)) {
    try { return JSON.parse(text) as JsonValue; } catch { /* Formatted numbers and dates remain text for their match rule. */ }
  }
  return text;
}

export function groupedAnswers(response: EvaluationResponse, rowId: number, field: string) {
  const groups = new Map<string, { key: string; value: JsonValue | undefined; answerText: string;
    variantKeys: string[]; eligibleVariantKeys: string[];
    variantStates: Array<{ key: string; verdict: EvaluationVerdict; eligible: boolean; stale: boolean }>;
    verdict: EvaluationVerdict; failed: boolean; canSelect: boolean; bounded: EvaluationBoundedValue | null }>();
  for (const variant of response.variants) {
    const cell = variant.rows[String(rowId)]?.[field];
    const state = cell?.state ?? 'missing';
    const verdict = variant.verdicts[String(rowId)]?.[field] ?? 'unchecked';
    const stale = (variant as typeof variant & { stale_source_row_ids?: number[] }).stale_source_row_ids?.includes(rowId) ?? false;
    const eligible = (state === 'value' || state === 'null') && verdict !== 'not_run' && !stale;
    const key = cell?.bounded_value && cell.value_hash ? `full:${cell.value_hash}`
      : state === 'value' || state === 'null' ? canonicalJson(cell?.value ?? null) : `${state}:${cell?.error ?? ''}`;
    let group = groups.get(key);
    if (!group) {
      group = { key, value: cell?.value,
        answerText: state === 'error' ? `Failed · ${cell?.error || 'Action failed'}`
          : ['missing', 'pending', 'cancelled'].includes(state) ? 'Not run' : cell?.bounded_value?.preview ?? answerText(cell?.value),
        variantKeys: [], eligibleVariantKeys: [], variantStates: [], verdict,
        failed: state === 'error', canSelect: false, bounded: cell?.bounded_value ?? null };
      groups.set(key, group);
    }
    group.variantKeys.push(variant.key);
    group.variantStates.push({ key: variant.key, verdict, eligible, stale });
    if (eligible) group.eligibleVariantKeys.push(variant.key);
    const eligibleStates = group.variantStates.filter(item => item.eligible);
    group.verdict = eligibleStates.some(item => item.verdict === 'unscored') ? 'unscored'
      : eligibleStates[0]?.verdict ?? (group.failed ? verdict : 'not_run');
    group.canSelect = group.eligibleVariantKeys.length > 0 && !group.bounded;
  }
  return [...groups.values()];
}

export function disagreementRows(response: EvaluationResponse, field: string): number[] {
  if (response.variants.length < 2) return [];
  return response.row_ids.filter((rowId) => {
    const answers = groupedAnswers(response, rowId, field).filter((group) => group.eligibleVariantKeys.length > 0);
    return answers.length > 1;
  });
}

export function filteredEvaluationRows(response: EvaluationResponse, field: string, filter: EvaluationRowFilter): number[] {
  if (filter === 'all') return response.row_ids;
  const best = response.best[field]?.[0];
  return response.row_ids.filter((rowId) => {
    const misses = response.variants.map((variant) => variant.verdicts[String(rowId)]?.[field] === 'miss');
    if (filter === 'any_miss') return misses.some(Boolean);
    if (filter === 'everyone_misses') return misses.length > 0 && misses.every(Boolean);
    const key = filter === 'best_misses' ? best : filter.slice('variant:'.length);
    return response.variants.find((variant) => variant.key === key)?.verdicts[String(rowId)]?.[field] === 'miss';
  });
}

export function scoreLabel(accuracy: number | null | undefined): string {
  return accuracy == null ? '—' : `${Math.round(accuracy * 100)}%`;
}
