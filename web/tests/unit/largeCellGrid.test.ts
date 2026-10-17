import { describe, expect, it } from 'vitest';
import { GridCellKind } from '@glideapps/glide-data-grid';
import { buildCell, oneClickFacetValue } from '../../src/grid/cells';
import { gridReadColumnIds } from '../../src/components/fieldValueDependencies';
import { rowTitle } from '../../src/workbench/rowTitle';
import type { ColumnDef, Row, SheetMeta } from '../../src/api/types';

const column: ColumnDef = { id: '1', name: 'Text', type: 'text' };
const row: Row = { id: '2', index: 0, cells: {}, provenance: {}, boundedValues: {
  '1': { preview: 'A display excerpt', value_kind: 'text', total_characters: 100_000,
    total_bytes: 100_000, retrieval_revision: 3, truncated: true, manual_editable: false, manual_edit_byte_limit: 65_536 },
} };

describe('large cell grid values', () => {
  it('projects visible columns while retaining the existing image-region dependencies', () => {
    const hidden = { id: '2', name: 'Hidden', type: 'json' } as ColumnDef;
    expect(gridReadColumnIds([column, hidden], ['Hidden'])).toEqual(['1']);
    expect(gridReadColumnIds([{ ...column, type: 'image' }, hidden], ['Hidden'])).toEqual(['1', '2']);
  });
  it('shows the preview read-only and never copies or filters it as an exact value', () => {
    const cell = buildCell(column, row, { editable: true });
    expect(cell.kind).toBe(GridCellKind.Text);
    if (cell.kind !== GridCellKind.Text) throw new Error('Expected a text cell');
    expect(cell.displayData).toContain('A display excerpt');
    expect(cell.displayData).toContain('[excerpt]');
    expect(cell.data).toBe('');
    expect(cell.copyData).not.toContain('A display excerpt');
    expect(cell.allowOverlay).toBe(false);
    expect(cell.readonly).toBe(true);
    expect(oneClickFacetValue(column, row)).toBeNull();
  });
  it('uses an explicitly abbreviated row title', () => {
    const sheet = { id: '1', name: 'Sheet', rowCount: 1, columns: [column] } as SheetMeta;
    expect(rowTitle(sheet, row)).toBe('A display excerpt…');
  });
});
