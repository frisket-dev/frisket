import type { Row, SheetMeta } from '../../api/types';

export const EMBEDDING_COST_SAMPLE_ROWS = 100;

export interface EmbeddingCostEstimate {
  firstRunUsd: number;
  per100RowsUsd: number;
  estimatedTokens: number;
  sampledRows: number;
  sampledEmbeddableRows: number;
}

function cellText(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

/** Mirror the refresh's selected-column payload closely enough to quote a
 * stable pre-run estimate. We sample at most 100 rows, use Frisket's shared
 * four-characters-per-token estimate, then scale to the sheet's row count. */
export function estimateEmbeddingCost({
  rows,
  sheet,
  sourceColumns,
  inputUsdPerMillionTokens,
}: {
  rows: Row[];
  sheet: SheetMeta;
  sourceColumns: string[];
  inputUsdPerMillionTokens: number;
}): EmbeddingCostEstimate {
  const columnIds = sourceColumns
    .map((name) => sheet.columns.find((column) => column.name === name)?.id)
    .filter((id): id is string => id != null);
  let sampledTokens = 0;
  let sampledEmbeddableRows = 0;
  for (const row of rows) {
    const payload = columnIds
      .map((id) => cellText(row.cells[id]).trim())
      .filter(Boolean)
      .join('\n');
    if (!payload) continue;
    sampledEmbeddableRows += 1;
    sampledTokens += Math.max(1, Math.floor(payload.length / 4));
  }

  const estimatedTokens = rows.length === 0
    ? 0
    : Math.ceil((sampledTokens / rows.length) * sheet.rowCount);
  const firstRunUsd = estimatedTokens * inputUsdPerMillionTokens / 1_000_000;
  const per100RowsUsd = sheet.rowCount > 0
    ? firstRunUsd / sheet.rowCount * 100
    : 0;
  return {
    firstRunUsd,
    per100RowsUsd,
    estimatedTokens,
    sampledRows: rows.length,
    sampledEmbeddableRows,
  };
}
