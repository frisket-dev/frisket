import { useEffect, useMemo, useState } from 'react';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { JsonMiniTable } from './RowDrawer';

// The 8 metadata keys media.extract_pdf_tables prepends to every flattened
// table row (extract_pdf_tables.py _MEDIA_PDF_TABLE_METADATA_COLUMN_TYPES /
// ops/media.py _pdf_table_record). They are hidden from the preview by default
// and excluded from the materialized child sheet.
const PDF_TABLE_METADATA_KEYS: ReadonlySet<string> = new Set([
  'source_row_id',
  'source_filename',
  'source_blob_hash',
  'page_start',
  'page_end',
  'table_index',
  'table_row_index',
  'raw_cells_json',
]);

interface TableGroup {
  tableIndex: number;
  items: Record<string, unknown>[];
  realColumns: string[];
}

/** Flatten every JSON list cell of `columnId`, group the items by their
 *  `table_index`, and record each group's real (non-metadata) column order. */
function groupRowsByTable(
  rows: Array<{ cells: Record<string, unknown> }>,
  columnId: string,
): TableGroup[] {
  const byIndex = new Map<number, Record<string, unknown>[]>();
  for (const row of rows) {
    const cell = row.cells[columnId];
    if (cell == null) continue;
    let parsed: unknown = cell;
    if (typeof cell === 'string') {
      try {
        parsed = JSON.parse(cell);
      } catch {
        continue;
      }
    }
    if (!Array.isArray(parsed)) continue;
    for (const item of parsed) {
      if (item === null || typeof item !== 'object' || Array.isArray(item)) continue;
      const record = item as Record<string, unknown>;
      const rawIndex = record.table_index;
      const tableIndex = typeof rawIndex === 'number' ? rawIndex : 0;
      const bucket = byIndex.get(tableIndex) ?? [];
      bucket.push(record);
      byIndex.set(tableIndex, bucket);
    }
  }
  return Array.from(byIndex.entries())
    .toSorted(([a], [b]) => a - b)
    .map(([tableIndex, items]) => {
      const realColumns: string[] = [];
      const seen = new Set<string>();
      for (const item of items) {
        for (const key of Object.keys(item)) {
          if (PDF_TABLE_METADATA_KEYS.has(key) || seen.has(key)) continue;
          seen.add(key);
          realColumns.push(key);
        }
      }
      return { tableIndex, items, realColumns };
    });
}

export interface PdfTablesPickerProps {
  sheetId: string;
  column: { id: string; name: string };
  defaultTargetName: string;
  onMaterialize(args: { columnName: string; includeColumns: string[]; targetName: string }): void;
  /** Bulk table export: explode every extracted
   *  table into a zip of per-table CSVs + manifest.json via export.column_tables,
   *  grouped by table_index with the same metadata keys excluded from the
   *  materialized-sheet path. Optional so callers that only need Materialize
   *  (e.g. tests seeding a bare picker) are unaffected. */
  onExportTables?(args: { columnName: string; groupBy: string; excludeColumns: string[] }): void;
  onClose(): void;
}

/** Post-run review surface for media.extract_pdf_tables: renders a structured per-table_index preview of the
 *  extraction column and materializes a clean child sheet via the ordinary
 *  derive.table_from_list column source with a metadata-excluding projection. */
export function PdfTablesPicker({
  sheetId,
  column,
  defaultTargetName,
  onMaterialize,
  onExportTables,
  onClose,
}: PdfTablesPickerProps) {
  const { projectApi: api } = useWorkspaceStores();
  const [rows, setRows] = useState<Array<{ cells: Record<string, unknown> }> | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [showMetadata, setShowMetadata] = useState(false);
  const [targetName, setTargetName] = useState(defaultTargetName);

  useEffect(() => {
    let cancelled = false;
    api
      .getSheetData(sheetId, 0, 500, null)
      .then((page) => {
        if (!cancelled) setRows(page.rows as Array<{ cells: Record<string, unknown> }>);
      })
      .catch((e: unknown) => {
        if (!cancelled) setLoadError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [sheetId]);

  const groups = useMemo(
    () => (rows ? groupRowsByTable(rows, column.id) : []),
    [rows, column.id],
  );

  // Every table group must expose the same real column set; a divergence means
  // the extraction shapes do not line up and the child sheet would be ragged.
  const canonicalColumns = groups[0]?.realColumns ?? [];
  const shapeMismatch =
    groups.length > 1 &&
    groups.some(
      (group) =>
        group.realColumns.length !== canonicalColumns.length ||
        group.realColumns.some((name, i) => name !== canonicalColumns[i]),
    );
  const hasTables = groups.length > 0 && canonicalColumns.length > 0;
  const canMaterialize = hasTables && !shapeMismatch && targetName.trim().length > 0;

  return (
    <div className="pdf-tables-picker" data-testid="pdf-tables-picker" aria-label="Review PDF tables">
      <div className="pdf-tables-picker-head">
        <div>
          <h3 className="pdf-tables-picker-title">Review tables</h3>
          <p className="pdf-tables-picker-sub">
            {groups.length === 1
              ? '1 table found'
              : `${groups.length} tables found`}{' '}
            in {column.name}
          </p>
        </div>
        <div className="pdf-tables-picker-actions">
          <label className="pdf-tables-metadata-label">
            <input
              type="checkbox"
              data-testid="pdf-table-metadata-toggle"
              checked={showMetadata}
              onChange={(e) => setShowMetadata(e.target.checked)}
            />
            Show metadata columns
          </label>
          <button type="button" className="btn" data-testid="pdf-table-picker-close" onClick={onClose}>
            Close
          </button>
        </div>
      </div>

      {loadError && <p className="form-error" role="alert">{loadError}</p>}
      {rows !== null && !hasTables && !loadError && (
        <p className="pdf-tables-picker-empty" data-testid="pdf-table-empty">
          No extracted tables found in this column yet.
        </p>
      )}

      {shapeMismatch && (
        <p className="form-error" role="alert" data-testid="pdf-table-shape-error">
          These tables have mismatched columns, so they cannot be merged into one
          sheet. Re-run extraction with a matching shape policy, or pick a single
          table.
        </p>
      )}

      <div className="pdf-tables-groups">
        {groups.map((group) => (
          <section
            className="pdf-tables-group"
            data-testid={`pdf-table-group-${group.tableIndex}`}
            key={group.tableIndex}
          >
            <header className="pdf-tables-group-head">
              <span className="pdf-tables-group-title">Table {group.tableIndex + 1}</span>
              <span className="pdf-tables-group-meta">
                {group.items.length} {group.items.length === 1 ? 'row' : 'rows'}
              </span>
            </header>
            <JsonMiniTable
              items={group.items}
              hiddenKeys={PDF_TABLE_METADATA_KEYS}
              showHidden={showMetadata}
              testId="pdf-table-preview"
            />
          </section>
        ))}
      </div>

      <div className="pdf-tables-picker-foot">
        <label className="form-label" htmlFor="pdf-table-target-name">
          New sheet name
        </label>
        <input
          id="pdf-table-target-name"
          className="form-input"
          data-testid="pdf-table-target-name"
          value={targetName}
          onChange={(e) => setTargetName(e.target.value)}
        />
        <button
          type="button"
          className="btn btn-primary"
          data-testid="pdf-table-materialize"
          disabled={!canMaterialize}
          onClick={() =>
            onMaterialize({
              columnName: column.name,
              includeColumns: canonicalColumns,
              targetName: targetName.trim(),
            })
          }
        >
          Create sheet from tables
        </button>
        {onExportTables && (
          <button
            type="button"
            className="btn"
            data-testid="pdf-table-export-tables"
            disabled={!hasTables || shapeMismatch}
            onClick={() =>
              onExportTables({
                columnName: column.name,
                groupBy: 'table_index',
                excludeColumns: [...PDF_TABLE_METADATA_KEYS],
              })
            }
          >
            Export all tables…
          </button>
        )}
      </div>
    </div>
  );
}
