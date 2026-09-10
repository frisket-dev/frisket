import type { ColumnDef, Row } from '../api/types';
import { FieldValue } from '../components/RowDrawer';
import { PanelSelect } from '../components/PanelSelect';

interface DerivedColumnPaneProps {
  columns: ColumnDef[];
  row: Row | null;
  selectedColumnId: string | null;
  onChangeColumn(columnId: string | null): void;
}

export function DerivedColumnPane({
  columns,
  row,
  selectedColumnId,
  onChangeColumn,
}: DerivedColumnPaneProps) {
  const derivedColumns = columns.filter((column) => column.ai);
  const selectedColumn = derivedColumns.find((column) => column.id === selectedColumnId) ?? null;

  return (
    <aside className="document-derived-pane" data-testid="document-derived-pane" aria-label="Derived value">
      <header className="document-derived-pane-header">
        <label className="document-derived-pane-picker">
          <span>Compare output</span>
          <PanelSelect
            data-testid="document-derived-column-select"
            value={selectedColumn?.id ?? ''}
            onChange={(event) => onChangeColumn(event.target.value || null)}
          >
            <option value="">Choose a derived column</option>
            {derivedColumns.map((column) => (
              <option key={column.id} value={column.id}>
                {column.name}
              </option>
            ))}
          </PanelSelect>
        </label>
      </header>
      <div className="document-derived-pane-body">
        {selectedColumn === null ? (
          <p className="document-derived-pane-empty">
            {derivedColumns.length === 0
              ? 'No derived columns are available for this sheet.'
              : 'Choose a derived column to compare with the source.'}
          </p>
        ) : row === null ? (
          <p className="document-derived-pane-empty">Choose a document to view its derived value.</p>
        ) : (
          <FieldValue
            col={selectedColumn}
            columns={columns}
            row={row}
            value={row.cells[selectedColumn.id] ?? null}
          />
        )}
      </div>
    </aside>
  );
}
