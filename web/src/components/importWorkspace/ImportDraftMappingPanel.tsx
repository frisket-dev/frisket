import { FileText } from 'lucide-react';
import type { ImportDraft, ImportDraftMappingColumn, SheetMeta } from '../../api/open';
import { PanelSelect } from '../PanelSelect';
import { IMPORT_COLUMN_TYPES } from './model';

function safeTestIdPart(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'column';
}

interface ImportDraftMappingPanelProps {
  draft: ImportDraft;
  columns: ImportDraftMappingColumn[];
  sheetName: string;
  sheets: SheetMeta[];
  destinationSheetId: string;
  destinationMode: 'append' | 'update';
  keepExistingOnBlank: boolean;
  busy?: boolean;
  error: string | null;
  onSheetNameChange(value: string): void;
  onDestinationSheetChange(value: string): void;
  onDestinationModeChange(value: 'append' | 'update'): void;
  onKeepExistingOnBlankChange(value: boolean): void;
  onColumnChange(key: string, patch: Partial<ImportDraftMappingColumn>): void;
  onBack(): void;
  onContinue(): void;
}

export function ImportDraftMappingPanel({
  draft,
  columns,
  sheetName,
  sheets,
  destinationSheetId,
  destinationMode,
  keepExistingOnBlank,
  busy = false,
  error,
  onSheetNameChange,
  onDestinationSheetChange,
  onDestinationModeChange,
  onKeepExistingOnBlankChange,
  onColumnChange,
  onBack,
  onContinue,
}: ImportDraftMappingPanelProps) {
  const activeColumns = columns.filter((column) => column.include);
  const activeColumnNames = activeColumns.map((column) => column.name.trim());
  const hasValidColumnNames =
    activeColumnNames.every(Boolean) &&
    new Set(activeColumnNames).size === activeColumnNames.length;
  const destination = sheets.find((sheet) => sheet.id === destinationSheetId);
  const incompatibleColumns = destination
    ? activeColumns.filter((column) => {
      const target = destination.columns.find((candidate) => candidate.name === column.name.trim());
      return !target || target.type !== column.type;
    })
    : [];
  const updateColumns = activeColumns.filter((column) => column.updatePolicy === 'update');
  const keyColumns = activeColumns.filter((column) => column.updatePolicy === 'match');
  const updateMode = Boolean(destination && destinationMode === 'update');
  return (
    <div className="import-draft-step">
      <div className="import-mode-summary">
        <FileText size={16} aria-hidden />
        <strong>Check data</strong>
        <span>{draft.row_count} rows detected</span>
      </div>
      <div className="import-draft-options">
        <label className="import-field-label" htmlFor="import-destination-select">
          Destination
          <PanelSelect
            id="import-destination-select"
            className="form-input"
            value={destinationSheetId}
            onChange={(event) => onDestinationSheetChange(event.target.value)}
          >
            <option value="">New sheet</option>
            {sheets.map((sheet) => <option key={sheet.id} value={sheet.id}>{sheet.name}</option>)}
          </PanelSelect>
        </label>
        {!destinationSheetId && (
        <label className="import-field-label" htmlFor="import-sheet-name-input">
          Sheet name
          <input
            id="import-sheet-name-input"
            className="form-input"
            data-testid="import-sheet-name"
            value={sheetName}
            onChange={(event) => onSheetNameChange(event.target.value)}
          />
        </label>
        )}
        {destination && (
          <label className="import-field-label" htmlFor="import-destination-mode">
            Import method
            <PanelSelect
              id="import-destination-mode"
              className="form-input"
              value={destinationMode}
              onChange={(event) => onDestinationModeChange(event.target.value as 'append' | 'update')}
            >
              <option value="append">Append rows</option>
              <option value="update">Update matching rows</option>
            </PanelSelect>
          </label>
        )}
        <label className="import-check-inline">
          <input type="checkbox" checked readOnly data-testid="import-first-row-header" />
          First row as header
        </label>
      </div>
      {draft.warnings.length > 0 && (
        <div className="import-draft-warnings">
          {draft.warnings.map((warning) => (
            <div key={warning}>{warning}</div>
          ))}
        </div>
      )}
      {error && (
        <div className="import-field-error" role="alert">
          {error}
        </div>
      )}
      {destination && (
        <div className={incompatibleColumns.length ? 'import-field-error' : 'import-draft-warnings'} data-testid="import-append-compatibility">
          {incompatibleColumns.length
            ? `${incompatibleColumns.length} unmapped or incompatible column${incompatibleColumns.length === 1 ? '' : 's'}; map each to an existing column with the same type.`
            : updateMode
              ? 'Choose at least one Match column and one Update column, then preview the changes.'
              : `${draft.row_count} rows will be appended; no existing rows will be changed.`}
        </div>
      )}
      {updateMode && (
        <label className="import-check-inline">
          <input
            type="checkbox"
            checked={keepExistingOnBlank}
            onChange={(event) => onKeepExistingOnBlankChange(event.target.checked)}
          />
          Keep existing values where imported cells are blank
        </label>
      )}
      <div className="import-map-grid">
        <table className="import-map-table">
          <thead>
            <tr>
              <th>Use</th>
              {updateMode && <th>Action</th>}
              <th>Column</th>
              <th>Type</th>
              <th>Sample</th>
            </tr>
          </thead>
          <tbody>
            {columns.map((column) => {
              const testId = safeTestIdPart(column.key);
              const sourceColumn = draft.columns.find((candidate) => candidate.key === column.key);
              return (
                <tr key={column.key}>
                  <td>
                    <input
                      type="checkbox"
                      aria-label={`Use ${column.key}`}
                      data-testid={`import-column-include-${testId}`}
                      checked={column.include}
                      onChange={(event) => onColumnChange(column.key, { include: event.target.checked })}
                    />
                  </td>
                  {updateMode && (
                    <td>
                      <PanelSelect
                        className="form-input import-map-select"
                        aria-label={`${column.key} update action`}
                        data-testid={`import-column-policy-${testId}`}
                        value={column.updatePolicy === 'match' ? 'match' : 'update'}
                        disabled={!column.include}
                        onChange={(event) => {
                          const updatePolicy = event.target.value as 'match' | 'update';
                          onColumnChange(column.key, { updatePolicy });
                        }}
                      >
                        <option value="match">Match</option>
                        <option value="update">Update</option>
                      </PanelSelect>
                    </td>
                  )}
                  <td>
                    <input
                      className="form-input import-map-input"
                      aria-label={`${column.key} column name`}
                      data-testid={`import-column-name-${testId}`}
                      value={column.name}
                      onChange={(event) => onColumnChange(column.key, { name: event.target.value })}
                      disabled={!column.include}
                    />
                  </td>
                  <td>
                    <PanelSelect
                      className="form-input import-map-select"
                      aria-label={`${column.key} column type`}
                      data-testid={`import-column-type-${testId}`}
                      value={column.type}
                      onChange={(event) => onColumnChange(column.key, { type: event.target.value })}
                      disabled={!column.include}
                    >
                      {IMPORT_COLUMN_TYPES.map((type) => (
                        <option key={type} value={type}>{type}</option>
                      ))}
                    </PanelSelect>
                  </td>
                  <td className="import-map-sample">
                    {(sourceColumn?.sample_values ?? []).slice(0, 3).join(', ')}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <div className="import-draft-preview" data-testid="import-draft-preview">
          <table>
            <thead>
              <tr>
                {activeColumns.map((column) => (
                  <th key={column.key}>{column.name || column.key}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {draft.preview_rows.slice(0, 8).map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {activeColumns.map((column) => (
                    <td key={column.key}>{String(row[column.key] ?? '')}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <div className="import-step-actions">
        <button type="button" className="btn" onClick={onBack}>Back</button>
        <button
          type="button"
          className="btn btn-primary import-primary"
          data-testid="import-map-continue"
          disabled={busy || (!destinationSheetId && !sheetName.trim()) || activeColumns.length === 0 || !hasValidColumnNames || incompatibleColumns.length > 0 || (updateMode && (keyColumns.length === 0 || updateColumns.length === 0))}
          onClick={onContinue}
        >
          {busy ? 'Previewing…' : updateMode ? 'Preview updates' : 'Continue'}
        </button>
      </div>
    </div>
  );
}
