import type { ActionJob, RunRowErrorGroup } from '../api/open';

// Distinct per-row failure messages grouped with counts + example row ids,
// rendered under the raw run-level error (the receipt's old aggregate —
// "N failed rows" — told you THAT it failed; this tells you WHY, and for
// how many rows exactly).
//
// Lives in its own module (not WorkbenchBottomDock.tsx, where it originated)
// because HistoryPanel.tsx also renders it: HistoryPanel -> WorkbenchBottomDock
// -> contributions -> HistoryPanel was a circular import. This file has no
// dependency back into that cycle.
export function RowErrorGroupList({ groups }: { groups: RunRowErrorGroup[] }) {
  return (
    <ul className="bottom-dock-row-error-groups">
      {groups.map((group, index) => (
        <li
          key={`${group.message}-${index}`}
          className="bottom-dock-row-error-group"
          data-testid={`bottom-dock-row-error-group-${index}`}
        >
          <span className="bottom-dock-row-error-count">{group.count}x:</span>{' '}
          <span className="bottom-dock-row-error-message">{group.message}</span>
          {group.rowIds.length > 0 && (
            <span className="bottom-dock-row-error-rows">
              {' '}(rows {group.rowIds.join(', ')}
              {group.count > group.rowIds.length ? ', ...' : ''})
            </span>
          )}
        </li>
      ))}
    </ul>
  );
}

/** Import diagnostics refer to source lines, including rows that were not added. */
export function ImportErrorGroupList({ groups }: { groups: NonNullable<ActionJob['importErrors']> }) {
  return <ul className="bottom-dock-row-error-groups">
    {groups.map((group) => <li key={group.code} className="bottom-dock-row-error-group">
      <span className="bottom-dock-row-error-count">{group.count.toLocaleString()}×:</span>{' '}
      <span className="bottom-dock-row-error-message">{group.message}</span>
      {(group.examples?.length ?? 0) > 0 && <ul>
        {group.examples?.map((example, index) => <li key={`${example.source_ordinal}:${index}`}>
          {example.line_start == null ? `Row ${example.source_ordinal}` : `Line ${example.line_start}`}
          {example.line_end != null && example.line_end !== example.line_start ? `–${example.line_end}` : ''}
          {example.column_name ? ` · ${example.column_name}` : ''}
          {example.original && <>: <q>{example.original.length > 160 ? `${example.original.slice(0, 160)}…` : example.original}</q></>}
        </li>)}
      </ul>}
    </li>)}
  </ul>;
}
