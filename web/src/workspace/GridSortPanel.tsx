// Toolbar-grade grid sort panel, opened from the column header menu's
// "Advanced sort…".
import { X } from 'lucide-react';
import type { GridSortDirection, SheetMeta } from '../api/open';
import { PanelSelect } from '../components/PanelSelect';

export function GridSortPanel({
  sheet,
  column,
  direction,
  onColumnChange,
  onDirectionChange,
  onApply,
  onClose,
}: {
  sheet: SheetMeta;
  column: string;
  direction: GridSortDirection;
  onColumnChange(column: string): void;
  onDirectionChange(direction: GridSortDirection): void;
  onApply(): void;
  onClose(): void;
}) {
  return (
    <section className="grid-control-panel" data-testid="grid-sort-panel" aria-label="Grid sort">
      <button
        type="button"
        className="grid-control-panel-close"
        data-testid="grid-sort-panel-close"
        aria-label="Close sort panel"
        title="Close"
        onClick={onClose}
      >
        <X size={14} aria-hidden />
      </button>
      <label>
        <span>Column</span>
        <PanelSelect
          className="row-height-select"
          data-testid="grid-sort-column"
          value={column}
          onChange={(e) => onColumnChange(e.target.value)}
        >
          {sheet.columns.map((col) => (
            <option key={col.id} value={col.name}>{col.name}</option>
          ))}
        </PanelSelect>
      </label>
      <label>
        <span>Direction</span>
        <PanelSelect
          className="row-height-select"
          data-testid="grid-sort-direction"
          value={direction}
          onChange={(e) => onDirectionChange(e.target.value as GridSortDirection)}
        >
          <option value="asc">ascending</option>
          <option value="desc">descending</option>
        </PanelSelect>
      </label>
      <button
        type="button"
        className="btn btn-primary"
        data-testid="apply-grid-sort"
        onClick={onApply}
      >
        Apply
      </button>
    </section>
  );
}
