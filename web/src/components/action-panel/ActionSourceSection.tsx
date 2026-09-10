import type { ColumnDef } from '../../api/open';

export function orderedSourceColumns(columns: ColumnDef[]): ColumnDef[] {
  return [
    ...columns.filter((column) => !column.ai),
    ...columns.filter((column) => Boolean(column.ai)),
  ];
}
