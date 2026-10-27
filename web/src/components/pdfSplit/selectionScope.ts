import type { SelectedGridRows } from '../../workspace/workspaceState';

/** Convert the grid's possibly sparse cache to the server's selection contract. */
export function selectionScope(selection: SelectedGridRows) {
  if (!selection.rowIndexes.length || selection.rowIndexes.length === selection.rowIds.length) {
    return { selected_row_ids: selection.rowIds.map(Number) };
  }
  const ordered = [...new Set(selection.rowIndexes)].sort((a, b) => a - b);
  const spans: Array<{ start: number; end: number }> = [];
  for (const index of ordered) {
    const last = spans.at(-1);
    if (last && last.end + 1 === index) last.end = index;
    else spans.push({ start: index, end: index });
  }
  const anchors = (selection.cachedAnchors ?? []).map(({ index, rowId }) => ({ index, row_id: Number(rowId) }));
  if (!anchors.length) throw new Error('Selection is not loaded. Refresh and select the rows again.');
  return { selected_index_ranges: spans, cached_anchors: anchors };
}
