import type { ActionCatalogEntry } from '../api/types';

export type ActionComparisonKind = 'row_overlay';

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

/** The catalog is the sole authority for generic action-comparison entry.
 * Unsupported/unknown declarations fail closed; action IDs are irrelevant. */
export function actionComparisonKind(
  entry: Pick<ActionCatalogEntry, 'ui_hints'> | null | undefined,
): ActionComparisonKind | null {
  const comparison = record(entry?.ui_hints.comparison);
  return comparison?.kind === 'row_overlay' ? 'row_overlay' : null;
}

export function supportsRowOverlayComparison(
  entry: Pick<ActionCatalogEntry, 'ui_hints'> | null | undefined,
): boolean {
  return actionComparisonKind(entry) === 'row_overlay';
}
