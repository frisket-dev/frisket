import type { ColumnDef } from '../api/types';

const CAPTION_COLUMN_TERMS = ['transcript', 'caption', 'captions', 'subtitle', 'subtitles'];

export function captionColumnScore(columnName: string, mediaCol: ColumnDef): number | null {
  const normalizedName = normalizeCaptionColumnName(columnName);
  if (!CAPTION_COLUMN_TERMS.some((term) => normalizedName.split(' ').includes(term))) {
    return null;
  }

  const normalizedMediaName = normalizeCaptionColumnName(mediaCol.name);
  if (normalizedMediaName && normalizedName.includes(normalizedMediaName)) return 0;
  if (normalizedName === 'transcript') return 1;
  if (
    mediaCol.type === 'video' &&
    ['caption', 'captions', 'subtitle', 'subtitles'].includes(normalizedName)
  ) {
    return 2;
  }
  if (normalizedName.includes('transcript')) return 3;
  return null;
}

function normalizeCaptionColumnName(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

/** Additional row cells consumed by FieldValue for media presentation.
 * Images deliberately inspect every sibling cell for region-shaped JSON, so
 * the projection must retain every sibling to preserve that existing rule. */
export function fieldValueDependencyColumnIds(
  columns: ColumnDef[],
  mediaCol: ColumnDef,
): string[] {
  if (mediaCol.type === 'image') {
    return columns
      .filter((column) => column.id !== mediaCol.id)
      .map((column) => String(column.id));
  }
  if (mediaCol.type !== 'video') return [];
  return columns
    .filter((candidate) => (
      candidate.id !== mediaCol.id &&
      candidate.type === 'text' &&
      captionColumnScore(candidate.name, mediaCol) !== null
    ))
    .map((candidate) => String(candidate.id));
}

/** Grid images use the same sibling-region scan as the inspector. All fetched
 * values still use bounded mode; ordinary grids only request visible columns. */
export function gridReadColumnIds(columns: ColumnDef[], hiddenColumnNames: readonly string[]): string[] {
  const visible = columns.filter((column) => !hiddenColumnNames.includes(column.name));
  return [...new Set(visible.flatMap((column) => [column.id,
    ...(column.type === 'image' ? fieldValueDependencyColumnIds(columns, column) : []),
  ]))];
}
