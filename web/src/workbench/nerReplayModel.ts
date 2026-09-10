import type { ActionCatalogEntry, ColumnRun, JsonValue, RegisteredActionRequest } from '../api/types';

export type NerReplayPlan =
  | { kind: 'run'; request: RegisteredActionRequest }
  | { kind: 'refused'; reason: string };

/** Stored Params carry lineage. A fresh ordinary invocation owns replacement,
 * pricing and consent, and rebuilds the layer against current source values. */
export function nerReplayPlan(
  sourceRun: ColumnRun | null,
  target: { sheetId: string; columnId: string; columnName: string | null },
  catalogEntry: ActionCatalogEntry | null | undefined,
): NerReplayPlan {
  if (!sourceRun || sourceRun.actionKind !== 'map.ner') {
    return { kind: 'refused', reason: 'This column has no current entity-extraction run to repeat.' };
  }
  if (catalogEntry?.kind !== 'map.ner' || catalogEntry.ui_hints.form !== 'generated') {
    return { kind: 'refused', reason: 'Entity extraction is unavailable in the current action catalog.' };
  }
  const stored = sourceRun.spec.params;
  if (stored === null || typeof stored !== 'object' || Array.isArray(stored)) {
    return { kind: 'refused', reason: 'This run has no recorded entity-extraction Params. Extract entities from the column menu.' };
  }
  const source = stored as Record<string, JsonValue>;
  if (source.source == null || !Array.isArray(source.labels)) {
    return { kind: 'refused', reason: 'This run is missing its source or entity labels.' };
  }
  const sheetId = Number(target.sheetId);
  const outputName = target.columnName?.trim();
  if (!Number.isSafeInteger(sheetId) || sheetId <= 0 || !outputName) {
    return { kind: 'refused', reason: 'The entity output column is no longer available.' };
  }
  const params = Object.fromEntries(
    ['source', 'labels', 'threshold', 'engine', 'model', 'extra_instructions']
      .filter((key) => Object.hasOwn(source, key))
      .map((key) => [key, structuredClone(source[key])]),
  );
  return {
    kind: 'run',
    request: {
      action_id: 'map.ner',
      scope: { kind: 'sheet_rows', sheet_id: sheetId },
      params,
      output_names: { entities: outputName },
      replace_existing: true,
      idempotency_key: crypto.randomUUID(),
    },
  };
}
