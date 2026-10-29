import type { ColumnDef, GeneratedActionCatalogEntry } from '../api/types';

/** File cells on the declared PDF-rich channel are documents; MIME admission
 * remains per row on the server, since column metadata has no MIME roster. */
export function sourceReferencesPdf(source: unknown, columns: readonly Pick<ColumnDef, 'name' | 'type'>[]): boolean {
  const files = new Set(columns.filter((column) => column.type === 'file').map((column) => column.name));
  if (!files.size) return false;
  if (Array.isArray(source)) return source.some((name) => typeof name === 'string' && files.has(name));
  if (!source || typeof source !== 'object' || !('text' in source) || typeof source.text !== 'string') return false;
  return [...source.text.matchAll(/\{\{\s*([^}]+?)\s*\}\}/g)].some((match) => files.has(match[1].trim()));
}

export function draftUsesPdf(
  entry: Pick<GeneratedActionCatalogEntry, 'ui_hints'>,
  draft: Readonly<Record<string, unknown>>,
  columns: readonly Pick<ColumnDef, 'name' | 'type'>[],
): boolean {
  return (entry.ui_hints.source_requirements ?? []).some((requirement) => (
    requirement.param !== undefined
    && entry.ui_hints.semantic_controls[requirement.param] === 'rich_source'
    && requirement.accepted_column_types?.includes('file')
    && sourceReferencesPdf(draft[requirement.param], columns)
  ));
}
