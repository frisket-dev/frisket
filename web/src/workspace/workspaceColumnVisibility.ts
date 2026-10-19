import type { ColumnGroupViewSpec } from '../grid/SheetGrid';

export function groupManagedHiddenColumnNames(groups: ColumnGroupViewSpec[]): Set<string> {
  const hidden = new Set<string>();
  for (const group of groups) {
    if (!group.show_justification) {
      for (const name of group.columns) {
        if (name === 'justification' || name.endsWith('_justification')) hidden.add(name);
      }
    }
    if (!group.show_confidence) {
      for (const name of group.columns) {
        if (name === 'confidence' || name.endsWith('_confidence')) hidden.add(name);
      }
    }
  }
  return hidden;
}
