import { useMemo, useState, type FormEvent } from 'react';
import { Download, Loader2 } from 'lucide-react';
import { type ActionCatalogEntry, type SheetMeta, type V1Receipt } from '../api/open';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { projectBlobUrl } from '../api/raw/projectResources';
import { columnTablesExportRequest } from '../actions/columnTablesExport';
import { PanelSelect } from './PanelSelect';

export interface ExportColumnTablesModalProps {
  projectId: string;
  currentSheet: SheetMeta;
  catalogEntry?: ActionCatalogEntry | null;
  onClose: () => void;
}

function terminal(status: string): boolean {
  return [
    'complete',
    'completed',
    'succeeded',
    'partial',
    'failed',
    'error',
    'cancelled',
    'canceled',
  ].includes(status.toLowerCase());
}

function outputDownload(
  projectId: string,
  receipt: V1Receipt,
  selectedName: string,
): { url: string; filename: string } | null {
  const output = receipt.outputs.find((item) => item.name === 'column_tables');
  if (!output) return null;
  const ref = output.ref ?? {};
  const hash = typeof ref.blob_hash === 'string' ? ref.blob_hash : null;
  if (!hash) return null;
  const path = typeof ref.project_path === 'string' ? ref.project_path : '';
  const filename = path.split('/').pop() || `${selectedName || 'column'}_tables.zip`;
  return { url: projectBlobUrl(projectId, hash), filename };
}

export function ExportColumnTablesModal({
  projectId,
  currentSheet,
  catalogEntry,
  onClose,
}: ExportColumnTablesModalProps) {
  const { projectApi: api } = useWorkspaceStores();
  const tableColumns = useMemo(
    () => currentSheet.columns.filter((column) => column.type.toLowerCase() === 'json'),
    [currentSheet.columns],
  );
  const [columnId, setColumnId] = useState(tableColumns[0]?.id ?? '');
  const [groupBy, setGroupBy] = useState('');
  const [excludeColumns, setExcludeColumns] = useState('');
  const [state, setState] = useState<'idle' | 'running' | 'done' | 'error'>('idle');
  const [error, setError] = useState<string | null>(null);
  const [download, setDownload] = useState<{ url: string; filename: string } | null>(null);

  const selected = tableColumns.find((column) => column.id === columnId);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!columnId || !selected || !catalogEntry || state === 'running') return;
    setState('running');
    setError(null);
    setDownload(null);
    try {
      const normalizedGroupBy = groupBy.trim();
      const normalizedExclusions = excludeColumns
        .split(/[\n,]/)
        .map((name) => name.trim())
        .filter(Boolean);
      const request = columnTablesExportRequest({
        sheetId: currentSheet.id,
        columnId,
        ...(normalizedGroupBy ? { groupBy: normalizedGroupBy } : {}),
        ...(normalizedExclusions.length ? { excludeColumns: normalizedExclusions } : {}),
      });
      const invocation = { projectId };
      const launch = await api.runAction(request, invocation);
      if (!launch.receiptId) throw new Error('Export did not return a receipt.');
      let receipt = await api.getReceipt(launch.receiptId, invocation);
      const deadline = Date.now() + 120_000;
      while (!terminal(receipt.status) && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 600));
        receipt = await api.getReceipt(launch.receiptId, invocation);
      }
      if (!terminal(receipt.status)) throw new Error('Export is still running; please try again shortly.');
      if (!['complete', 'completed', 'succeeded'].includes(receipt.status.toLowerCase())) {
        throw new Error(receipt.errors[0]?.message || `Export ${receipt.status}.`);
      }
      const result = outputDownload(invocation.projectId, receipt, selected.name);
      if (!result) throw new Error('Export completed without a column_tables ZIP output.');
      setDownload(result);
      setState('done');
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Export failed.');
      setState('error');
    }
  };

  return (
    <div
      className="modal-backdrop"
      data-testid="export-column-tables-modal"
      role="dialog"
      aria-modal="true"
      aria-labelledby="export-column-tables-title"
    >
      <form className="modal-card" onSubmit={submit}>
        <div className="modal-title" id="export-column-tables-title">
          <Download size={15} /> Export tables as ZIP
        </div>
        {tableColumns.length === 0 ? (
          <p className="modal-body-text">This sheet has no JSON columns to export.</p>
        ) : (
          <>
            <label className="form-field">
              JSON column
              <PanelSelect
                className="form-input"
                data-testid="export-column-tables-column"
                value={columnId}
                onChange={(event) => setColumnId(event.target.value)}
                disabled={state === 'running'}
              >
                {tableColumns.map((column) => (
                  <option key={column.id} value={column.id}>{column.name}</option>
                ))}
              </PanelSelect>
            </label>
            <label className="form-field">
              Group by nested field (optional)
              <input
                className="form-input"
                data-testid="export-column-tables-group-by"
                value={groupBy}
                onChange={(event) => setGroupBy(event.target.value)}
                disabled={state === 'running'}
                placeholder="table_index"
              />
            </label>
            <label className="form-field">
              Exclude nested fields (optional)
              <textarea
                className="form-input"
                data-testid="export-column-tables-exclude-columns"
                value={excludeColumns}
                onChange={(event) => setExcludeColumns(event.target.value)}
                disabled={state === 'running'}
                placeholder={'source_row_id\nsource_filename'}
                rows={4}
              />
              <p className="form-hint">One field per line, or comma-separated.</p>
            </label>
          </>
        )}
        {!catalogEntry && (
          <div
            className="form-error"
            role="alert"
            data-testid="export-column-tables-catalog-unavailable"
          >
            Column-table export is unavailable because its action catalog entry is missing.
          </div>
        )}
        {state === 'running' && (
          <p className="modal-body-text" role="status">
            <Loader2 size={14} className="spin" /> Preparing ZIP…
          </p>
        )}
        {error && <div className="form-error" role="alert">{error}</div>}
        {download && (
          <a
            className="btn"
            data-testid="export-column-tables-download"
            href={download.url}
            download={download.filename}
          >
            Download ZIP
          </a>
        )}
        <div className="form-actions">
          <button
            type="button"
            className="btn"
            data-testid="export-column-tables-cancel"
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn-primary"
            data-testid="export-column-tables-submit"
            disabled={!selected || !catalogEntry || state === 'running'}
          >
            {state === 'running' ? 'Exporting…' : 'Export'}
          </button>
        </div>
      </form>
    </div>
  );
}
