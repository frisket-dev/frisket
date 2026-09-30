import type { ColumnDef, ReviewBundleField, Row } from '../../api/types';
import { FieldValue } from '../RowDrawer';
import styles from './ReviewValue.module.css';

type ReviewDisplayField = ReviewBundleField & {
  format?: string | null;
  semanticType?: string | null;
};

export interface ReviewValueProps {
  field: ReviewBundleField;
  onSelectItem?(index: number): void;
  selectableItemIndices?: ReadonlySet<number>;
}

/** Review uses the same typed value presentation as Inspect. */
export function ReviewValue({ field, onSelectItem, selectableItemIndices }: ReviewValueProps) {
  const display = field as ReviewDisplayField;
  const column: ColumnDef = {
    id: field.columnId,
    name: field.columnName,
    type: field.columnType,
    format: display.format,
    semanticType: display.semanticType,
  };
  const row: Row = {
    id: field.rowId,
    index: 0,
    cells: { [field.columnId]: field.value },
    provenance: {},
  };
  return (
    <div className={styles.reviewValue} data-testid="review-value">
      <FieldValue
        col={column}
        columns={[column]}
        row={row}
        sheetId={field.sheetId}
        value={field.value}
        onSelectItem={onSelectItem}
        selectableItemIndices={selectableItemIndices}
      />
    </div>
  );
}
