import type { PreviewTableSampleResult } from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { resolveMediaValue } from '../media/resolveMediaValue';
import { PreviewFieldValue } from './RowDrawer';
import { parsePreviewTemporalValue } from '../temporal/preview';
import { formatTimecode } from '../temporal/model';

/** Ordered sample records, never project rows: no selection/edit/provenance. */
export function PreviewTable({ result }: { result: PreviewTableSampleResult }) {
  const { chromePreferences: { projectId } } = useWorkspaceStores();
  const artifactPrefix = `/api/projects/${encodeURIComponent(projectId)}/actions/v1/preview/${encodeURIComponent(result.previewId)}/artifacts/`;
  const columns = result.columns.filter((column) => !column.hidden);
  return <div className="preview-table" data-testid="preview-table" style={{ overflow: 'auto', flex: 1 }}>
    {result.warnings.length > 0 && <div role="status" data-testid="preview-table-warnings">
      <p>Preview warnings</p>
      <ul>{result.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
    </div>}
    <table aria-label="Preview sample">
      <thead><tr>{columns.map((column) => <th key={column.name} scope="col">{column.name}</th>)}</tr></thead>
      <tbody>{result.rows.map((row, index) => <tr key={index}>
        {columns.map((column) => {
          const cell = row[column.name] ?? { value: null };
          const temporal = parsePreviewTemporalValue(cell.value, column.columnType);
          if (temporal) {
            const items = temporal.items ?? [temporal.item!];
            return <td key={column.name}><div data-testid="preview-temporal-value">
              <small>{temporal.timeline.preview_clock_id ? 'Preview timeline' : 'Source timeline'}</small>
              <ul>{items.map((item) => <li key={item.id}>
                {item.label && <span>{item.label}: </span>}
                {'at_ms' in item ? formatTimecode(item.at_ms)
                  : `${formatTimecode(item.start_ms)} – ${formatTimecode(item.end_ms)}`}
                {item.metadata && Object.keys(item.metadata).length > 0 && <pre>{JSON.stringify(item.metadata)}</pre>}
              </li>)}</ul>
            </div></td>;
          }
          // Only host-issued scratch URLs or durable blob envelopes may trigger
          // a media read. The existing resolver scopes blob reads to this project;
          // arbitrary source/model URLs cannot bypass preview's effect admission.
          const media = ['image', 'audio', 'video', 'file'].includes(column.columnType);
          const artifact = typeof cell.value === 'string' && cell.value.startsWith(artifactPrefix)
            && /^[A-Za-z0-9_-]+$/.test(cell.value.slice(artifactPrefix.length));
          const blobHash = media ? resolveMediaValue(cell.value, projectId)?.blobHash : undefined;
          const durableBlob = blobHash !== undefined && /^[0-9a-f]{64}$/.test(blobHash);
          const displayColumn = media && (artifact || durableBlob) ? column : { ...column, format: 'plain_text' };
          const displayCell = displayColumn.format === 'plain_text'
            && cell.value !== null && typeof cell.value === 'object'
            ? { ...cell, value: JSON.stringify(cell.value) } : cell;
          return <td key={column.name}><PreviewFieldValue showHeading={false}
            preview={{ column: displayColumn, cell: displayCell }} /></td>;
        })}
      </tr>)}</tbody>
    </table>
    {result.rows.length === 0 && <p>No rows in this sample.</p>}
  </div>;
}
