import { Fragment, useDeferredValue, useMemo, useRef, useState, type DragEvent } from 'react';
import { AlertTriangle, CheckCircle2, LockKeyhole, Search, Upload } from 'lucide-react';

import type {
  TableImportColumn,
  TableImportColumnPatch,
  TableImportDestinationConfig,
  TableImportDestinationSnapshot,
  TableImportDraft,
  TableImportPreview,
  TableImportRequestedSettings,
  TableImportTargetType,
} from '../../../api/tableImports';
import { PanelSelect } from '../../PanelSelect';
import { SegmentedToggle } from '../../PanelPrimitives';
import { ImportDestinationChoice, ImportDestinationOptions } from '../ImportDestinationChoice';
import { ImportStepFooter } from '../ImportStepFooter';
import {
  summarizePasteForDisplay,
  type TableImportSourceIssue,
  type TableImportSourceMode,
} from './model';

import './TableImportFlow.css';

export interface TableImportDestinationSheet {
  columns: Array<{ id: number; name: string }>;
  id: number;
  name: string;
  rowCount: number;
}

function count(value: number, singular: string, plural = `${singular}s`): string {
  return `${value.toLocaleString()} ${value === 1 ? singular : plural}`;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${Math.ceil(value / 1024).toLocaleString()} KB`;
  return `${(value / 1024 ** 2).toFixed(value < 10 * 1024 ** 2 ? 1 : 0)} MB`;
}

function displayPasteSummary(value: string): string {
  const summary = summarizePasteForDisplay(value);
  if (!summary.lines) return 'Paste rows to see a quick size estimate.';
  if (summary.delimiter === 'one') return `${count(summary.lines, 'line')} · one column`;
  return `${count(summary.lines, 'line')} × ${count(summary.columns, 'column')}, separated by ${summary.delimiter}.`;
}

export function AddDataStep({
  busy,
  issue,
  mode,
  selectedFile,
  password,
  passwordFilename,
  pasteText,
  onCancel,
  onChooseFtm,
  onChooseMode,
  onFiles,
  onLargePasteConfirm,
  onMultipleFiles,
  onPasswordChange,
  onPasteChange,
  onPasteSubmit,
  onSelectedFileSubmit,
  onResetIssue,
  onSplitPdf,
  onUnlock,
}: {
  busy: boolean;
  issue: TableImportSourceIssue | null;
  mode: TableImportSourceMode;
  selectedFile: File | null;
  password: string;
  passwordFilename: string | null;
  pasteText: string;
  onCancel(): void;
  onChooseFtm(file: File): void;
  onChooseMode(mode: TableImportSourceMode): void;
  onFiles(files: File[]): void;
  onLargePasteConfirm(): void;
  onMultipleFiles(files?: File[]): void;
  onPasswordChange(value: string): void;
  onPasteChange(value: string): void;
  onPasteSubmit(): void;
  onSelectedFileSubmit(): void;
  onResetIssue(): void;
  onSplitPdf(file: File): void;
  onUnlock(): void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const deferredPasteText = useDeferredValue(pasteText);
  const pasteSummary = useMemo(() => displayPasteSummary(deferredPasteText), [deferredPasteText]);
  const submitPaste = issue?.kind === 'large_paste' ? onLargePasteConfirm : onPasteSubmit;
  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (!busy) onFiles(Array.from(event.dataTransfer.files));
  };

  return <section className="table-import-step table-import-add" aria-label="Add data">
    <div className="table-import-step-body table-import-add-body">
      <SegmentedToggle
        ariaLabel="Table source"
        value={mode}
        disabled={busy}
        onValueChange={(value) => onChooseMode(value as TableImportSourceMode)}
        options={[{ value: 'file', label: 'Choose a file' }, { value: 'paste', label: 'Paste rows' }]}
        buttonTestId={(value) => `table-import-source-${value}`}
      />
      {passwordFilename ? <div className="table-import-message" data-tone="warning">
        <LockKeyhole size={20} aria-hidden />
        <div><small>PASSWORD</small><h3>This workbook is password-protected</h3><p>Enter the password to open {passwordFilename}. It isn’t saved.</p></div>
        <label><span>Password</span><input className="form-input" type="password" autoComplete="off" value={password} disabled={busy} onChange={(event) => onPasswordChange(event.currentTarget.value)} /></label>
        <button type="button" className="btn btn-primary" disabled={busy || !password} onClick={onUnlock}>{busy ? 'Opening…' : 'Open'}</button>
      </div> : issue ? <SourceIssue issue={issue} busy={busy} onFiles={onFiles} onMultipleFiles={onMultipleFiles} onReset={onResetIssue} onSplitPdf={onSplitPdf} onChooseFtm={onChooseFtm} />
        : mode === 'file' ? <>
          <div className="table-import-dropzone" onDragOver={(event) => { event.preventDefault(); event.dataTransfer.dropEffect = busy ? 'none' : 'copy'; }} onDrop={handleDrop}>
            <input ref={inputRef} type="file" multiple hidden onChange={(event) => { onFiles(Array.from(event.currentTarget.files ?? [])); event.currentTarget.value = ''; }} />
            <Upload size={32} aria-hidden />
            <h3>{selectedFile ? selectedFile.name : 'Drop a CSV or Excel file here'}</h3>
            <button type="button" className="btn btn-primary" disabled={busy} onClick={() => inputRef.current?.click()}>{busy ? 'Checking…' : 'Choose file'}</button>
            <p>.csv · .tsv · .txt · .xlsx · .xls · .ods</p>
          </div>
          <p className="table-import-source-note">One file at a time. To bring in many files or a folder, use <button type="button" className="text-btn" onClick={() => onMultipleFiles()}>Multiple files</button>.</p>
        </> : <div className="table-import-paste">
          <textarea aria-label="Paste rows" autoFocus spellCheck={false} value={pasteText} disabled={busy} placeholder={'Name\tAmount\tDate'} onChange={(event) => onPasteChange(event.currentTarget.value)} />
          <p data-ready={pasteText.trim().length > 0 || undefined}><i />{pasteSummary}</p>
        </div>}
    </div>
    <ImportStepFooter>
      <button type="button" className="btn" disabled={busy} onClick={onCancel}>Cancel</button>
      <span>{passwordFilename ? `${passwordFilename} is selected. Enter its password to continue.` : mode === 'paste' ? 'Pasted rows are read the same way as a file.' : selectedFile ? `${selectedFile.name} is selected.` : 'Choose or drop a file to continue.'}</span>
      {mode === 'paste' ? <button type="button" className="btn btn-primary" disabled={busy || !pasteText.trim()} onClick={submitPaste}>{busy ? 'Checking…' : issue?.kind === 'large_paste' ? 'Continue with the paste' : 'Check rows →'}</button>
        : <button type="button" className="btn btn-primary" disabled={busy || !selectedFile || issue !== null || passwordFilename !== null} onClick={onSelectedFileSubmit}>{busy ? 'Checking…' : 'Check rows →'}</button>}
    </ImportStepFooter>
  </section>;
}

function SourceIssue({ issue, busy, onFiles, onMultipleFiles, onReset, onSplitPdf, onChooseFtm }: {
  issue: TableImportSourceIssue;
  busy: boolean;
  onFiles(files: File[]): void;
  onMultipleFiles(files?: File[]): void;
  onReset(): void;
  onSplitPdf(file: File): void;
  onChooseFtm(file: File): void;
}) {
  const chooseRef = useRef<HTMLInputElement | null>(null);
  let title: string;
  let body: string;
  if (issue.kind === 'multiple') {
    title = 'One file at a time';
    const names = issue.files.slice(0, 2).map((file) => file.name).join(' and ');
    body = `You dropped ${names}${issue.files.length > 2 ? ` and ${issue.files.length - 2} more` : ''}. Import one now, or use Multiple files to bring them in together.`;
  } else if (issue.kind === 'pdf') {
    title = `${issue.file.name} is a PDF`;
    body = 'Use Multiple files to import it as a document, or Split a PDF to turn a packet into separate files.';
  } else if (issue.kind === 'follow_the_money') {
    title = `${issue.file.name} is FollowTheMoney data`;
    body = 'Open the existing FollowTheMoney importer to choose a dataset name and import it.';
  } else if (issue.kind === 'empty') {
    title = `${issue.filename} is empty`;
    body = 'Choose a file that contains rows.';
  } else if (issue.kind === 'large_paste') {
    title = `That’s ${formatBytes(issue.size)} of pasted text`;
    body = 'Large pastes are slow to check. Saving the rows as a CSV and choosing the file is faster and gives the same result.';
  } else {
    title = `${issue.filename} isn’t a supported table file`;
    body = issue.message ?? 'Export it as CSV or Excel, then choose the exported file.';
  }
  return <div className="table-import-message" data-tone="warning">
    <AlertTriangle size={20} aria-hidden />
    <div><h3>{title}</h3><p>{body}</p></div>
    <div className="table-import-message-actions">
      {issue.kind === 'multiple' && <><button type="button" className="btn btn-primary" disabled={busy} onClick={() => onFiles([issue.files[0]])}>Import {issue.files[0].name}</button><button type="button" className="btn" disabled={busy} onClick={() => onMultipleFiles(issue.files)}>Use Multiple files</button></>}
      {issue.kind === 'pdf' && <><button type="button" className="btn btn-primary" disabled={busy} onClick={() => onMultipleFiles([issue.file])}>Use Multiple files</button><button type="button" className="btn" disabled={busy} onClick={() => onSplitPdf(issue.file)}>Split a PDF</button></>}
      {issue.kind === 'follow_the_money' && <button type="button" className="btn btn-primary" disabled={busy} onClick={() => onChooseFtm(issue.file)}>Use FollowTheMoney import</button>}
      {(issue.kind === 'unsupported' || issue.kind === 'empty') && <button type="button" className="btn" disabled={busy} onClick={() => chooseRef.current?.click()}>Choose another file</button>}
      {issue.kind === 'large_paste' && <button type="button" className="btn" disabled={busy} onClick={onReset}>Choose a file instead</button>}
    </div>
    <input ref={chooseRef} hidden type="file" onChange={(event) => { onFiles(Array.from(event.currentTarget.files ?? [])); event.currentTarget.value = ''; }} />
  </div>;
}

const DELIMITERS = [
  { value: '', label: 'Auto' },
  { value: ',', label: 'Comma' },
  { value: ';', label: 'Semicolon' },
  { value: '\t', label: 'Tab' },
  { value: '|', label: 'Pipe' },
];

const ENCODINGS = [
  { value: '', label: 'Auto' },
  { value: 'utf-8', label: 'UTF-8' },
  { value: 'windows-1252', label: 'Windows-1252' },
  { value: 'iso-8859-1', label: 'ISO-8859-1' },
  { value: 'mac-roman', label: 'Mac Roman' },
  { value: 'utf-16', label: 'UTF-16' },
];

function resolvedAuto(label: string, value: string | null): string {
  return value ? `${label} (${value === '\t' ? 'Tab' : value === ',' ? 'Comma' : value === ';' ? 'Semicolon' : value === '|' ? 'Pipe' : displayEncoding(value)})` : label;
}

function displayDelimiter(value: string | null | undefined): string {
  if (value === '\t') return 'Tab';
  if (value === ',') return 'Comma';
  if (value === ';') return 'Semicolon';
  if (value === '|') return 'Pipe';
  return value ?? 'Auto';
}

function displayEncoding(value: string | null | undefined): string {
  if (!value) return 'Auto';
  const normalized = value.toLocaleLowerCase();
  if (normalized === 'utf-8' || normalized === 'utf8') return 'UTF-8';
  if (normalized === 'utf-16' || normalized === 'utf16') return 'UTF-16';
  if (normalized === 'windows-1252' || normalized === 'cp1252') return 'Windows-1252';
  if (normalized === 'iso-8859-1' || normalized === 'latin-1' || normalized === 'latin1') return 'ISO-8859-1';
  if (normalized === 'mac-roman' || normalized === 'macroman') return 'Mac Roman';
  return value;
}

function numberSample(draft: TableImportDraft): string | null {
  for (const column of draft.columns) {
    const sample = column.samples.find((candidate) => /^\s*\(?[-+]?\s*[$€£]?\s*\d[\d., ]*(?:%|\))?\s*$/.test(candidate.original) && /\d[., ]\d/.test(candidate.original));
    if (sample) return `${sample.original} → ${sample.display || sample.original}`;
  }
  return null;
}

export function CheckRowsStep({ draft, busy, onBack, onContinue, onSettings }: {
  draft: TableImportDraft;
  busy: boolean;
  onBack(): void;
  onContinue(): void;
  onSettings(settings: TableImportRequestedSettings): void;
}) {
  const settings = draft.settings;
  if (!settings) return <div className="table-import-preview-loading" role="status">Preparing reader settings…</div>;
  const { requested, resolved } = settings;
  const delimited = resolved.kind === 'delimited';
  const preview = draft.preview;
  const update = <K extends keyof TableImportRequestedSettings>(key: K, value: TableImportRequestedSettings[K]) => onSettings({ ...requested, [key]: value });
  const rowCount = draft.analysis.exact_rows ?? draft.analysis.estimated_rows ?? draft.analysis.scanned_rows;
  const footerRows = draft.analysis.complete ? count(rowCount, 'row') : `${count(rowCount, 'row')} so far`;
  const numericExample = numberSample(draft);
  return <section className="table-import-step table-import-rows" aria-label="Check rows">
    <div className="table-import-step-body">
      <div className="table-import-reader-controls">
        {delimited ? <>
          <label><span>Separator</span><PanelSelect value={requested.delimiter ?? ''} disabled={busy} onChange={(event) => update('delimiter', event.currentTarget.value || null)}>{DELIMITERS.map((option) => <option key={option.value} value={option.value}>{option.value === '' ? resolvedAuto('Auto', resolved.delimiter ?? null) : option.label}</option>)}</PanelSelect></label>
          {draft.source.input_kind === 'file' && <label><span>Character encoding</span><PanelSelect value={requested.encoding ?? ''} disabled={busy} onChange={(event) => update('encoding', event.currentTarget.value || null)}>{ENCODINGS.map((option) => <option key={option.value} value={option.value}>{option.value === '' ? resolvedAuto('Auto', resolved.encoding ?? null) : option.label}</option>)}</PanelSelect></label>}
          <label className="table-import-number-format"><span>Numbers{numericExample ? ` · ${numericExample}` : ''}</span><PanelSelect value={`${requested.decimal_separator}:${requested.thousands_separator}`} disabled={busy} onChange={(event) => {
            const [decimal, thousands] = event.currentTarget.value.split(':') as [TableImportRequestedSettings['decimal_separator'], TableImportRequestedSettings['thousands_separator']];
            onSettings({ ...requested, decimal_separator: decimal, thousands_separator: thousands });
          }}><option value="auto:auto">Auto ({resolved.thousands_separator ? `1${resolved.thousands_separator}234${resolved.decimal_separator}56` : `1234${resolved.decimal_separator}56`})</option><option value=".:,">1,234.56</option><option value=",:.">1.234,56</option></PanelSelect></label>
        </> : <label className="table-import-sheet-control"><span>Sheet</span><PanelSelect value={requested.worksheet ?? ''} disabled={busy} onChange={(event) => update('worksheet', event.currentTarget.value)}>{draft.source.sheets.map((sheet) => <option key={sheet.index} value={sheet.name} disabled={sheet.empty}>{sheet.name}{sheet.hidden ? ' (hidden)' : ''}{sheet.empty ? ' · no cells' : ''}</option>)}</PanelSelect></label>}
        <NumberStepper label="Skip rows at start" value={requested.skip_rows_start ?? 0} disabled={busy} onChange={(value) => update('skip_rows_start', value)} />
        <label className="table-import-header-check"><input type="checkbox" checked={requested.first_row_is_header ?? true} disabled={busy} onChange={(event) => update('first_row_is_header', event.currentTarget.checked)} /> First row has column names</label>
        <NumberStepper label="Skip rows at end" value={requested.skip_rows_end ?? 0} disabled={busy} onChange={(value) => update('skip_rows_end', value)} />
      </div>
      {preview ? <TablePreview preview={preview} firstRowIsHeader={requested.first_row_is_header ?? true} onRecordClick={(ordinal) => update('skip_rows_start', Math.max(0, ordinal - 1))} /> : <div className="table-import-preview-loading" role="status">Reading rows…</div>}
      {preview && <PreviewNotice preview={preview} firstRowIsHeader={requested.first_row_is_header ?? true} />}
    </div>
    <ImportStepFooter><button type="button" className="btn" disabled={busy} onClick={onBack}>Back</button><span>{footerRows} · {count(preview?.columns.length ?? draft.columns.length, 'column')}{delimited ? ` · ${displayDelimiter(resolved.delimiter)} · ${displayEncoding(resolved.encoding)}` : ''}</span><button type="button" className="btn btn-primary" disabled={busy || preview === null || preview.columns.length === 0} onClick={onContinue}>Columns →</button></ImportStepFooter>
  </section>;
}

function NumberStepper({ label, value, disabled, onChange }: { label: string; value: number; disabled: boolean; onChange(value: number): void }) {
  return <label className="table-import-number-stepper"><span>{label}</span><span><button type="button" disabled={disabled || value <= 0} aria-label={`Decrease ${label}`} onClick={() => onChange(Math.max(0, value - 1))}>−</button><b>{value}</b><button type="button" disabled={disabled} aria-label={`Increase ${label}`} onClick={() => onChange(value + 1)}>+</button></span></label>;
}

function lineLabel(row: TableImportPreview['head'][number], expectedValues: number): string {
  const actualValues = row.cells.length + row.hidden_cell_count;
  if (row.role === 'header') return 'Names';
  if (row.role === 'skipped_start' || row.role === 'skipped_end') return 'Skipped';
  if (row.role === 'blank') return 'Blank';
  const extra = row.issues.find((issue) => issue.code === 'extra_values');
  if (extra) return `+${Math.max(extra.original_values.length, actualValues - expectedValues, extra.count ?? 0)} values`;
  const missing = row.issues.find((issue) => issue.code === 'missing_values');
  if (missing) return `−${Math.max(expectedValues - actualValues, missing.count ?? 0, 1)} values`;
  return '';
}

function TablePreview({ preview, firstRowIsHeader, onRecordClick }: { preview: TableImportPreview; firstRowIsHeader: boolean; onRecordClick(recordOrdinal: number): void }) {
  const visibleColumns = preview.columns.slice(0, 8);
  const rows = [...preview.head, ...preview.tail];
  const visibleCellCount = Math.min(8, Math.max(visibleColumns.length, ...rows.map((row) => row.cells.length)));
  const visibleCellIndexes = Array.from({ length: visibleCellCount }, (_, index) => index);
  return <div className="table-import-preview-wrap">
    <div className="table-import-preview-heading"><span><strong>Preview</strong> Start and end of the file</span><span>{preview.columns.length > visibleColumns.length ? `First ${visibleColumns.length} of ${preview.columns.length} columns · ` : ''}Click a line number to start there. The import uses these same settings.</span></div>
    <div className="table-import-preview-scroll"><table className="table-import-preview"><tbody>
      {!firstRowIsHeader && <tr data-role="header" aria-label="Generated column names"><th><small>Names</small></th>{visibleCellIndexes.map((index) => <td key={index} data-extra={index === preview.expected_values || undefined}>{visibleColumns[index]?.name || (index === preview.expected_values ? 'extra values' : index > preview.expected_values ? '' : `column ${index + 1}`)}</td>)}</tr>}
      {rows.map((row, index) => <Fragment key={`${row.role}:${row.record_ordinal}`}>
        {index === preview.head.length && preview.tail.length > 0 && <tr className="table-import-preview-gap" key="gap"><th colSpan={visibleCellCount + 1}>⋯ {preview.omitted_rows == null ? 'more rows' : count(preview.omitted_rows, 'more row')}</th></tr>}
        <tr data-role={row.role} data-issue={row.issues.length > 0 || undefined}>
          <th><button type="button" disabled={row.physical_line_start == null} onClick={() => onRecordClick(row.record_ordinal)}>{row.physical_line_start ?? row.record_ordinal}</button>{lineLabel(row, preview.expected_values) && <small>{lineLabel(row, preview.expected_values)}</small>}</th>
          {visibleCellIndexes.map((columnIndex) => {
            const extraCell = columnIndex >= preview.expected_values && columnIndex < row.cells.length;
            const extraHeader = row.role === 'header' && columnIndex === preview.expected_values;
            return <td key={visibleColumns[columnIndex]?.index ?? `extra:${columnIndex}`} data-extra={extraCell || extraHeader || undefined}>{row.cells[columnIndex] ?? (extraHeader ? 'extra values' : '')}</td>;
          })}
        </tr>
      </Fragment>) }
    </tbody></table></div>
  </div>;
}

function PreviewNotice({ preview, firstRowIsHeader }: { preview: TableImportPreview; firstRowIsHeader: boolean }) {
  const notice = preview.notice;
  const rows = [...preview.head, ...preview.tail];
  const firstMismatch = rows.find((row) => row.issues.some((issue) => issue.code === 'extra_values' || issue.code === 'missing_values'));
  const namesLine = rows.find((row) => row.role === 'header')?.physical_line_start ?? 1;
  const mismatchCount = notice.count ?? 0;
  const expected = notice.expected_values ?? preview.expected_values;
  let text: string;
  if (notice.kind === 'unreadable_characters') text = `${count(notice.count ?? 0, 'character')} in the preview can’t be read as ${displayEncoding(notice.encoding ?? null)}. Try another encoding.`;
  else if (notice.kind === 'one_column') text = 'Only one column found. If each line holds several values, try another separator.';
  else if (notice.kind === 'mostly_wrong_width') text = firstRowIsHeader
    ? `Most lines have ${notice.common_values ?? expected} values, but the column names on line ${namesLine} have ${expected}. If the file starts with a title or notes, skip those rows. Otherwise, try another separator.`
    : `Most lines don’t have ${expected} values. Try another separator.`;
  else if (notice.kind === 'some_wrong_width' || notice.kind === 'extra_values') text = mismatchCount === 1
    ? firstMismatch
      ? `Line ${firstMismatch.physical_line_start ?? firstMismatch.record_ordinal} doesn’t have ${expected} values. It will be imported and listed in Errors.`
      : `1 line doesn’t have ${expected} values. It will be imported and listed in Errors.`
    : `${mismatchCount.toLocaleString()} lines don’t have ${expected} values. They will be imported and listed in Errors.`;
  else if (notice.kind === 'counting') text = 'Checking the rest of the file…';
  else text = `Every line shown has ${notice.common_values ?? preview.expected_values} values.`;
  const tone = notice.kind === 'consistent' ? 'success' : notice.kind === 'counting' ? 'neutral' : 'warning';
  return <div className="table-import-preview-notice" data-tone={tone}>{tone === 'success' ? <CheckCircle2 size={15} /> : tone === 'warning' ? <AlertTriangle size={15} /> : null}<span>{text}</span></div>;
}

const TARGET_TYPES: Array<{ value: TableImportTargetType; label: string }> = [
  { value: 'text', label: 'Text' }, { value: 'integer', label: 'Integer' }, { value: 'number', label: 'Number' },
  { value: 'date', label: 'Date' }, { value: 'datetime', label: 'Date & time' }, { value: 'boolean', label: 'Yes/No' }, { value: 'omit', label: 'Leave out' },
];

type ColumnFilter = 'all' | 'review' | 'omitted';

export function ColumnsStep({ draft, busy, onBack, onColumns, onContinue }: {
  draft: TableImportDraft;
  busy: boolean;
  onBack(): void;
  onColumns(columns: TableImportColumnPatch[]): void;
  onContinue(): void;
}) {
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<ColumnFilter>('all');
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const [bulkType, setBulkType] = useState<TableImportTargetType>('text');
  const selectionAnchor = useRef<number | null>(null);
  const shiftHeld = useRef(false);
  const reviewCount = draft.columns.filter((column) => column.needs_review.length > 0).length;
  const omittedCount = draft.columns.filter((column) => !column.included || column.target_type === 'omit').length;
  const shown = useMemo(() => draft.columns.filter((column) => {
    if (!column.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())) return false;
    if (filter === 'review') return column.needs_review.length > 0;
    if (filter === 'omitted') return !column.included || column.target_type === 'omit';
    return true;
  }), [draft.columns, filter, query]);
  const patchFor = (column: TableImportColumn, patch: Partial<TableImportColumnPatch>): TableImportColumnPatch => ({ source_index: column.source_index, included: column.included, target_type: column.target_type, date_order: column.date_order, ...patch });
  const changeOne = (column: TableImportColumn, patch: Partial<TableImportColumnPatch>) => onColumns(draft.columns.map((candidate) => patchFor(candidate, candidate.source_index === column.source_index ? patch : {})));
  const applySelected = (targetType: TableImportTargetType) => {
    onColumns(draft.columns.map((column) => patchFor(column, selected.has(column.source_index) ? { target_type: targetType, included: targetType !== 'omit' } : {})));
    setSelected(new Set());
    selectionAnchor.current = null;
  };
  const selectColumn = (sourceIndex: number, checked: boolean, shiftKey: boolean) => {
    const anchor = selectionAnchor.current;
    setSelected((current) => {
      const next = new Set(current);
      const anchorIndex = anchor === null ? -1 : shown.findIndex((column) => column.source_index === anchor);
      const currentIndex = shown.findIndex((column) => column.source_index === sourceIndex);
      const range = shiftKey && anchorIndex >= 0 && currentIndex >= 0
        ? shown.slice(Math.min(anchorIndex, currentIndex), Math.max(anchorIndex, currentIndex) + 1)
        : shown.filter((column) => column.source_index === sourceIndex);
      range.forEach((column) => checked ? next.add(column.source_index) : next.delete(column.source_index));
      return next;
    });
    selectionAnchor.current = sourceIndex;
  };
  return <section className="table-import-step table-import-columns" aria-label="Columns" onKeyDown={(event) => { if (event.key === 'Shift') shiftHeld.current = true; }} onKeyUp={(event) => { if (event.key === 'Shift') shiftHeld.current = false; }}>
    <div className="table-import-step-body">
      <div className="table-import-column-toolbar">
        {selected.size > 0 ? <><strong>{count(selected.size, 'column')} selected</strong><label>Set type <PanelSelect value={bulkType} onChange={(event) => setBulkType(event.currentTarget.value as TableImportTargetType)}>{TARGET_TYPES.filter((type) => type.value !== 'omit').map((type) => <option key={type.value} value={type.value}>{type.label}</option>)}</PanelSelect></label><button type="button" className="btn" disabled={busy} onClick={() => applySelected(bulkType)}>Apply</button><button type="button" className="btn" disabled={busy} onClick={() => applySelected('omit')}>Leave out</button><button type="button" className="text-btn" onClick={() => { setSelected(new Set()); selectionAnchor.current = null; }}>Clear</button></> : <><label className="table-import-column-search"><Search size={14} /><input value={query} placeholder="Find columns" aria-label="Find columns" onChange={(event) => setQuery(event.currentTarget.value)} /></label><div className="table-import-filter-chips">{([['all', `All ${draft.columns.length}`], ['review', `Needs a look ${reviewCount}`], ['omitted', `Left out ${omittedCount}`]] as Array<[ColumnFilter, string]>).map(([value, label]) => <button type="button" key={value} className={filter === value ? 'active' : ''} onClick={() => setFilter(value)}>{label}</button>)}</div><span>Shift-click to select a range.</span></>}
      </div>
      <div className="table-import-column-list" role="table" aria-label="Columns to import">
        <div role="row" className="table-import-column-heading"><span role="columnheader"><input type="checkbox" aria-label="Select shown columns" checked={shown.length > 0 && shown.every((column) => selected.has(column.source_index))} onChange={(event) => { selectionAnchor.current = null; setSelected((current) => { const next = new Set(current); shown.forEach((column) => event.currentTarget.checked ? next.add(column.source_index) : next.delete(column.source_index)); return next; }); }} /></span><strong role="columnheader">Column</strong><strong role="columnheader">Type</strong><strong role="columnheader">Values as imported</strong><strong role="columnheader">Note</strong></div>
        {shown.map((column) => <ColumnRow key={column.source_index} column={column} selected={selected.has(column.source_index)} busy={busy} onSelected={(checked, shiftKey) => selectColumn(column.source_index, checked, shiftKey || shiftHeld.current)} onChange={(patch) => changeOne(column, patch)} />)}
      </div>
    </div>
    <ImportStepFooter><button type="button" className="btn" disabled={busy} onClick={onBack}>Back</button><span>{draft.columns.filter((column) => column.included && column.target_type !== 'omit').length} of {draft.columns.length} columns will be imported.</span><button type="button" className="btn btn-primary" disabled={busy || draft.columns.every((column) => !column.included || column.target_type === 'omit')} onClick={onContinue}>Continue →</button></ImportStepFooter>
  </section>;
}

function ColumnRow({ column, selected, busy, onSelected, onChange }: { column: TableImportColumn; selected: boolean; busy: boolean; onSelected(checked: boolean, shiftKey: boolean): void; onChange(patch: Partial<TableImportColumnPatch>): void }) {
  const omitted = !column.included || column.target_type === 'omit';
  const dateOrderNeeded = column.ambiguous_date_order && (column.target_type === 'date' || column.target_type === 'datetime');
  const note = omitted ? 'Not imported' : column.conversion_counts.leading_zero_loss > 0 ? `${count(column.conversion_counts.leading_zero_loss, 'value')} ${column.conversion_counts.leading_zero_loss === 1 ? 'loses' : 'lose'} leading zeros` : column.conversion_counts.invalid > 0 ? `${count(column.conversion_counts.invalid, 'value')} won’t convert` : column.conversion_counts.time_dropped > 0 ? 'Times are dropped' : column.stats_complete ? 'All fit' : 'Checking values…';
  const noteTone = omitted || !column.stats_complete ? 'neutral' : dateOrderNeeded || column.conversion_counts.leading_zero_loss > 0 || column.conversion_counts.invalid > 0 || column.conversion_counts.time_dropped > 0 ? 'warning' : 'success';
  return <div role="row" className="table-import-column-row" data-omitted={omitted || undefined}>
    <span role="cell"><input type="checkbox" aria-label={`Select ${column.name}`} checked={selected} onChange={(event) => onSelected(event.currentTarget.checked, 'shiftKey' in event.nativeEvent && event.nativeEvent.shiftKey === true)} /></span>
    <span role="cell"><strong>{column.name}</strong>{column.name_status === 'duplicate' && <small>In the file: {column.original_name}</small>}{column.name_status === 'missing' && <small>No name in the file</small>}</span>
    <span role="cell"><PanelSelect aria-label={`Type for ${column.name}`} value={omitted ? 'omit' : column.target_type} disabled={busy} onChange={(event) => { const next = event.currentTarget.value as TableImportTargetType; onChange({ target_type: next, included: next !== 'omit' }); }}>{TARGET_TYPES.map((type) => <option key={type.value} value={type.value}>{type.label}</option>)}</PanelSelect></span>
    <ColumnSamples column={column} />
    <span role="cell" className="table-import-column-note" data-tone={noteTone}>{dateOrderNeeded ? <PanelSelect aria-label={`Date order for ${column.name}`} value={column.date_order ?? column.inferred_date_order ?? 'month_first'} disabled={busy} onChange={(event) => onChange({ date_order: event.currentTarget.value as 'month_first' | 'day_first' })}><option value="month_first">Month first (03/04 = Mar 4)</option><option value="day_first">Day first (03/04 = Apr 3)</option></PanelSelect> : note}</span>
  </div>;
}

const SAMPLE_PREVIEW_LENGTH = 100;

function ColumnSamples({ column }: { column: TableImportColumn }) {
  const [expanded, setExpanded] = useState(false);
  const samples = column.samples.slice(0, 5);
  const canExpand = samples.some((sample) => sampleDisplay(column, sample) !== sampleDisplay(column, sample, true));
  return <span role="cell" className="table-import-column-values">
    <span className="table-import-column-samples" data-expanded={expanded || undefined}>
      {samples.map((sample, index) => <i key={index} data-outcome={sample.outcome}>{sampleDisplay(column, sample, !expanded)}</i>)}
    </span>
    {canExpand && <button type="button" className="text-btn" aria-expanded={expanded} aria-label={`${expanded ? 'Show less' : 'Show full values'} for ${column.name}`} onClick={() => setExpanded((value) => !value)}>{expanded ? 'Show less' : 'Show full values'}</button>}
  </span>;
}

function sampleDisplay(column: TableImportColumn, sample: TableImportColumn['samples'][number], compact = false): string {
  const display = (text: string) => {
    if (!compact) return text;
    const preview = text.length > SAMPLE_PREVIEW_LENGTH ? `${text.slice(0, SAMPLE_PREVIEW_LENGTH)}…` : text;
    return preview.replace(/\r\n|\r|\n/g, ' ↵ ');
  };
  if (sample.outcome === 'blank') return 'blank';
  if (sample.outcome === 'invalid') return `${display(sample.original || 'blank')} → blank`;
  let imported = sample.display || String(sample.value ?? sample.original);
  if (column.target_type === 'date' && /^\d{4}-\d{2}-\d{2}$/.test(imported)) {
    const [year, month, day] = imported.split('-').map(Number);
    imported = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short', timeZone: 'UTC', year: 'numeric' }).format(new Date(Date.UTC(year, month - 1, day)));
  }
  return sample.outcome === 'changed' ? `${display(sample.original)} → ${display(imported)}` : display(imported);
}

export function ImportTableStep({ draft, destination, destinationSheets, preferredDestinationSheetId = null, busy, onBack, onCommit, onDestination }: {
  draft: TableImportDraft;
  destination: TableImportDestinationConfig;
  destinationSheets: readonly TableImportDestinationSheet[];
  preferredDestinationSheetId?: number | null;
  busy: boolean;
  onBack(): void;
  onCommit(): void;
  onDestination(destination: TableImportDestinationConfig, snapshot?: TableImportDestinationSnapshot | null): void;
}) {
  const comparison = draft.destination?.comparison ?? null;
  const existingSheet = destination.kind === 'existing' ? destinationSheets.find((sheet) => sheet.id === destination.sheet_id) : null;
  const rowTotal = draft.analysis.exact_rows ?? draft.analysis.estimated_rows ?? null;
  const importRows = comparison?.import_rows ?? rowTotal;
  const includedColumns = draft.columns.filter((column) => column.included && column.target_type !== 'omit').length;
  const omittedColumns = draft.columns.length - includedColumns;
  const newColumns = destination.kind === 'existing' && draft.destination?.configuration.kind === 'existing' && draft.destination.configuration.sheet_id === destination.sheet_id
    ? draft.destination.mappings.filter((mapping) => mapping.target.kind === 'new').length : 0;
  const columnSummary = [omittedColumns > 0 ? `${count(omittedColumns, 'column')} left out` : '', newColumns > 0 ? `${count(newColumns, 'new column')} added` : ''].filter(Boolean).join(' · ');
  const invalidValues = draft.columns.reduce((total, column) => total + (column.included && column.target_type !== 'omit' ? column.conversion_counts.invalid : 0), 0);
  const invalidColumnNames = draft.columns.filter((column) => column.included && column.target_type !== 'omit' && column.conversion_counts.invalid > 0).map((column) => column.name);
  const newSheetName = destination.kind === 'new' ? destination.sheet_name : draft.source.input_kind === 'paste' ? 'Pasted rows' : draft.source.filename.replace(/\.[^.]+$/, '');
  return <section className="table-import-step table-import-destination" aria-label="Import">
    <div className="table-import-step-body">
      <ImportDestinationOptions>
        <ImportDestinationChoice name="table-import-destination" selected={destination.kind === 'new'} disabled={busy} title="New sheet" showInactiveChildren onSelect={() => onDestination({ kind: 'new', sheet_name: newSheetName })}>
          <label className="import-destination-choice-field"><span className="sr-only">Sheet name</span><input aria-label="Sheet name" className="form-input" value={newSheetName} disabled={busy || destination.kind !== 'new'} onChange={(event) => onDestination({ kind: 'new', sheet_name: event.currentTarget.value })} /></label>
        </ImportDestinationChoice>
        <ImportDestinationChoice name="table-import-destination" selected={destination.kind === 'existing'} disabled={busy || destinationSheets.length === 0} title="Add to an existing sheet" showInactiveChildren onSelect={() => { const sheet = destinationSheets.find((candidate) => candidate.id === preferredDestinationSheetId) ?? destinationSheets[0]; if (sheet) onDestination({ kind: 'existing', sheet_id: sheet.id }); }}>
          <label className="import-destination-choice-field"><span className="sr-only">Sheet</span><PanelSelect aria-label="Sheet" value={String(destination.kind === 'existing' ? destination.sheet_id : destinationSheets.find((candidate) => candidate.id === preferredDestinationSheetId)?.id ?? destinationSheets[0]?.id ?? '')} disabled={busy || destination.kind !== 'existing' || destinationSheets.length === 0} onChange={(event) => onDestination({ kind: 'existing', sheet_id: Number(event.currentTarget.value) })}>{destinationSheets.length === 0 && <option value="">No sheets yet</option>}{destinationSheets.map((sheet) => <option value={sheet.id} key={sheet.id}>{sheet.name} · {count(sheet.rowCount, 'row')}</option>)}</PanelSelect></label>
        </ImportDestinationChoice>
      </ImportDestinationOptions>
      {destination.kind === 'existing' && <ExistingDestination draft={draft} destination={destination} sheet={existingSheet ?? null} busy={busy} onDestination={onDestination} />}
      <div className="table-import-summary">
        <div><span>Rows to add</span><strong>{importRows == null ? 'Counting…' : `${draft.analysis.complete || comparison?.import_rows != null ? '' : 'About '}${importRows.toLocaleString()}`}</strong><small /></div>
        <div><span>Columns</span><strong>{includedColumns.toLocaleString()}</strong><small>{columnSummary}</small></div>
        {(draft.analysis.issue_counts.extra_values ?? 0) > 0 && <div><span>Lines with extra values</span><strong>{draft.analysis.issue_counts.extra_values!.toLocaleString()}</strong><small>Kept; extra values go in a column named extra values</small></div>}
        {invalidValues > 0 && <div><span>Values left blank</span><strong>{invalidValues.toLocaleString()}</strong><small>{invalidColumnNames.join(', ')}; originals listed in Errors</small></div>}
        {(draft.analysis.issue_counts.missing_values ?? 0) > 0 && <div><span>Lines with missing values</span><strong>{draft.analysis.issue_counts.missing_values!.toLocaleString()}</strong><small>Missing cells stay blank</small></div>}
      </div>
    </div>
    <ImportStepFooter><button type="button" className="btn" disabled={busy} onClick={onBack}>Back</button><span>{destination.kind === 'new' ? 'The new sheet opens when you click Import.' : `Rows are added below the existing ${existingSheet?.rowCount.toLocaleString() ?? 0}.`}</span><button type="button" className="btn btn-primary" disabled={busy || !draft.can_commit || (destination.kind === 'new' && !destination.sheet_name.trim())} onClick={onCommit}>{busy ? 'Importing…' : importRows == null ? 'Import' : `Import ${draft.analysis.complete || comparison?.import_rows != null ? '' : 'about '}${count(importRows, 'row')}`}</button></ImportStepFooter>
  </section>;
}

function ExistingDestination({ draft, destination, sheet, busy, onDestination }: { draft: TableImportDraft; destination: Extract<TableImportDestinationConfig, { kind: 'existing' }>; sheet: TableImportDestinationSheet | null; busy: boolean; onDestination(destination: TableImportDestinationConfig, snapshot?: TableImportDestinationSnapshot | null): void }) {
  const snapshot = draft.destination;
  if (!snapshot || snapshot.configuration.kind !== 'existing' || snapshot.configuration.sheet_id !== destination.sheet_id) return <div className="table-import-destination-loading" role="status">Matching columns…</div>;
  const eligibleKeyIndexes = new Set(snapshot.mappings.filter((mapping) => mapping.target.kind === 'existing').map((mapping) => mapping.source_index));
  const eligibleKeys = draft.columns.filter((column) => column.included && eligibleKeyIndexes.has(column.source_index));
  const changeMapping = (sourceIndex: number, target: TableImportDestinationSnapshot['mappings'][number]['target']) => {
    const mappings = snapshot.mappings.map((candidate) => candidate.source_index === sourceIndex ? { ...candidate, target } : candidate);
    const keyStillMatches = mappings.some((candidate) => candidate.source_index === snapshot.dedup.key_source_index && candidate.target.kind === 'existing');
    onDestination(destination, { ...snapshot, mappings, dedup: keyStillMatches ? snapshot.dedup : { enabled: false, key_source_index: null } });
  };
  return <div className="table-import-existing">
    <header><strong>Columns</strong><span>Matched by name. Change any match with its menu.</span></header>
    <div className="table-import-mappings">{snapshot.mappings.map((mapping) => <div key={mapping.source_index}><strong>{mapping.source_name}</strong><span>→</span><PanelSelect aria-label={`Map ${mapping.source_name}`} value={mapping.target.kind === 'new' ? 'new' : `existing:${mapping.target.column_id}`} disabled={busy} onChange={(event) => {
      const target = event.currentTarget.value === 'new'
        ? { kind: 'new' as const, name: mapping.source_name }
        : { kind: 'existing' as const, column_id: Number(event.currentTarget.value.slice('existing:'.length)) };
      changeMapping(mapping.source_index, target);
    }}><option value="new">+ New column</option>{sheet?.columns.map((column) => <option key={column.id} value={`existing:${column.id}`}>{column.name}</option>)}</PanelSelect><span className="table-import-mapping-status"><b data-match={mapping.match}>{mapping.match === 'matched' ? 'Matched' : mapping.match === 'similar' ? 'Similar name' : mapping.match === 'type_clash' ? mapping.detail ?? 'Type clash' : 'Not in the sheet yet'}</b>{mapping.match === 'type_clash' && <button type="button" className="text-btn" disabled={busy} onClick={() => changeMapping(mapping.source_index, { kind: 'new', name: mapping.source_name })}>Add as new {targetTypeLabel(mapping.source_type)} column</button>}</span></div>)}</div>
    <label className="table-import-dedup"><input type="checkbox" checked={eligibleKeys.length > 0 && snapshot.dedup.enabled} disabled={busy || eligibleKeys.length === 0} onChange={(event) => onDestination(destination, { ...snapshot, dedup: { enabled: event.currentTarget.checked, key_source_index: event.currentTarget.checked ? snapshot.dedup.key_source_index ?? eligibleKeys[0]?.source_index ?? null : null } })} /> Skip rows already in {sheet?.name ?? 'this sheet'}, matched on <PanelSelect aria-label="Duplicate key" value={snapshot.dedup.key_source_index ?? ''} disabled={busy || !snapshot.dedup.enabled || eligibleKeys.length === 0} onChange={(event) => onDestination(destination, { ...snapshot, dedup: { enabled: true, key_source_index: Number(event.currentTarget.value) } })}><option value="">Choose a matched column</option>{eligibleKeys.map((column) => <option key={column.source_index} value={column.source_index}>{column.name}</option>)}</PanelSelect>{eligibleKeys.length === 0 ? <span>Match a column to use this</span> : snapshot.comparison.duplicate_rows != null && <span>{count(snapshot.comparison.duplicate_rows, 'row')} found</span>}</label>
  </div>;
}

function targetTypeLabel(type: string): string {
  return type === 'datetime' ? 'Date & time' : type === 'boolean' ? 'Yes/No' : type.charAt(0).toLocaleUpperCase() + type.slice(1);
}
