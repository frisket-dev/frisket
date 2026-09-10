import { useCallback, useRef } from 'react';
import type { ReactNode } from 'react';
import type { ColumnDef } from '../../api/open';
import { PanelSelect } from '../PanelSelect';
import { MultiColumnPicker } from '../MultiColumnPicker';
import { SegmentedToggle } from '../PanelPrimitives';
import {
  columnSelectOptions,
  handleAutoResizeTextareaInput,
  resizeTextareaToContent,
  type SourceMode,
} from './formControlHelpers';

export type { SourceMode };

/** The mode axis this control can render. The two-mode `SourceMode` covers a
 * single column or template; `columns` (ordered multi-select) and `all`
 * (every requirement-compatible column) are enabled by `multiColumn`. */
export type SourceInputMode = SourceMode | 'columns' | 'all';

/** The multi-column extension. Present = three modes (All / Column(s) /
 *  Template); absent = today's exact two-mode control. */
export interface SourceInputMultiColumn {
  /** Column NAMES, in the order they will be read and serialized. */
  selectedColumns: string[];
  onSelectedColumnsChange(names: string[]): void;
  allModeLabel?: string;
  columnsModeLabel?: string;
}
const TEMPLATE_SOURCE_SELECT_VALUE = '__frisket_template_source__';
const COLUMN_SOURCE_SELECT_VALUE = '__frisket_column_source__';

function templateSegments(
  template: string,
  columns: ColumnDef[],
  knownPrefixes: string[] = [],
): ReactNode[] {
  const nodes: ReactNode[] = [];
  const columnNames = new Set(columns.map((column) => column.name));
  const tokenPattern = /\{\{\s*([^}]+?)\s*\}\}/g;
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = tokenPattern.exec(template)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(template.slice(lastIndex, match.index));
    }
    const tokenName = match[1].trim();
    const known = columnNames.has(tokenName)
      || knownPrefixes.some((prefix) => tokenName.startsWith(prefix));
    nodes.push(
      <span
        key={`${match.index}-${match[0]}`}
        className={known ? 'template-token known' : 'template-token unknown'}
      >
        {match[0]}
      </span>,
    );
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < template.length) nodes.push(template.slice(lastIndex));
  return nodes.length ? nodes : [''];
}

/**
 * Token composer for {{column}} templates: a highlighted textarea plus an
 * insert bar (column chips + a "+ more" PanelSelect that reaches every
 * column). Shared by the extract/summarize "Template" input mode and the
 * standalone Template action so both compose tokens the same way.
 */
export function TemplateComposer({
  value,
  columns,
  textareaTestId,
  insertTestId,
  ariaLabel,
  placeholder = '{{headline}} — {{body}}',
  knownTokenPrefixes = [],
  hideHint = false,
  tightInsert = false,
  compactInsert = false,
  onChange,
  onUseColumn,
}: {
  value: string;
  columns: ColumnDef[];
  textareaTestId: string;
  insertTestId: string;
  ariaLabel: string;
  placeholder?: string;
  /** Token name prefixes that highlight as "known" alongside column names
   *  (e.g. `secret.` so `{{secret.TOKEN}}` is not flagged unknown). */
  knownTokenPrefixes?: string[];
  /** Suppress the built-in compose hint (callers that repeat the composer per
   *  field supply their own single hint instead). */
  hideHint?: boolean;
  /** Insert tokens with NO surrounding whitespace — for structured fields (a
   *  URL, a JSON string) where a prose space would corrupt the value. */
  tightInsert?: boolean;
  /** Keep only the full column select in the insert bar. Useful when several
   *  compact composers appear in a structured pair editor. */
  compactInsert?: boolean;
  onChange(value: string): void;
  /** When provided, the insert menu offers "Use a column" (source-mode switch). */
  onUseColumn?(): void;
}) {
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const setNode = useCallback((node: HTMLTextAreaElement | null) => {
    textareaRef.current = node;
    resizeTextareaToContent(node);
  }, []);
  const insertColumnToken = (columnName: string) => {
    if (!columnName) return;
    const token = `{{${columnName}}}`;
    const node = textareaRef.current;
    const start = node?.selectionStart ?? value.length;
    const end = node?.selectionEnd ?? value.length;
    const prefix = value.slice(0, start);
    const suffix = value.slice(end);
    const spacerBefore = !tightInsert && prefix && !/\s$/.test(prefix) ? ' ' : '';
    const spacerAfter = !tightInsert && suffix && !/^\s/.test(suffix) ? ' ' : '';
    onChange(`${prefix}${spacerBefore}${token}${spacerAfter}${suffix}`);
    requestAnimationFrame(() => {
      const cursor = start + spacerBefore.length + token.length + spacerAfter.length;
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(cursor, cursor);
      resizeTextareaToContent(textareaRef.current);
    });
  };
  const onInsertSelect = (selected: string) => {
    if (!selected || selected === TEMPLATE_SOURCE_SELECT_VALUE) return;
    if (selected === COLUMN_SOURCE_SELECT_VALUE) {
      onUseColumn?.();
      return;
    }
    insertColumnToken(selected);
  };
  return (
    <div className="template-compose-row">
      <div className="template-composer">
        <div className="template-input-shell">
          <div className="template-highlight-layer" aria-hidden>
            {templateSegments(value, columns, knownTokenPrefixes)}
          </div>
          <textarea
            ref={setNode}
            className="form-input template-input"
            data-testid={textareaTestId}
            value={value}
            rows={1}
            spellCheck={false}
            placeholder={placeholder}
            aria-label={ariaLabel}
            onInput={handleAutoResizeTextareaInput}
            onChange={(e) => onChange(e.target.value)}
          />
        </div>
        <div className={`template-insert-bar${compactInsert ? ' compact' : ''}`}>
          {!compactInsert && <span className="template-insert-label">Insert</span>}
          {!compactInsert && columns.slice(0, 4).map((c) => (
            <button
              key={c.id}
              type="button"
              className="template-insert-chip"
              onClick={() => insertColumnToken(c.name)}
            >
              {c.name}
            </button>
          ))}
          {/* PanelSelect keeps a real select trigger so keyboard and e2e reach
              every column, not just the first few. */}
          <PanelSelect
            className="template-column-insert"
            data-testid={insertTestId}
            aria-label="Insert column in template"
            value={TEMPLATE_SOURCE_SELECT_VALUE}
            onChange={(e) => onInsertSelect(e.target.value)}
          >
            <option value={TEMPLATE_SOURCE_SELECT_VALUE}>
              {compactInsert ? 'Insert column…' : '+ more'}
            </option>
            {onUseColumn && <option value={COLUMN_SOURCE_SELECT_VALUE}>Use a column</option>}
            {columns.length > 0 && (
              <optgroup label="Insert column">
                {columns.map((c) => (
                  <option key={c.id} value={c.name}>
                    {c.name}
                  </option>
                ))}
              </optgroup>
            )}
          </PanelSelect>
        </div>
      </div>
      {!hideHint && (
        <p className="form-hint template-compose-hint">
          Type freely and drop in <code>{'{{column}}'}</code> tokens — the model reads the
          composed text, not each column separately.
        </p>
      )}
    </div>
  );
}

export function SourceInputControl({
  mode,
  columns,
  selectedColumn,
  template,
  label = 'Input',
  testIdPrefix,
  templateInputTestId,
  emptyMessage,
  ariaLabel,
  allowTemplate = true,
  multiColumn,
  onModeChange,
  onSelectedColumnChange,
  onTemplateChange,
}: {
  mode: SourceInputMode;
  columns: ColumnDef[];
  selectedColumn: string;
  template: string;
  label?: string;
  testIdPrefix: string;
  templateInputTestId?: string;
  emptyMessage: string;
  ariaLabel: string;
  allowTemplate?: boolean;
  multiColumn?: SourceInputMultiColumn;
  onModeChange(mode: SourceInputMode): void;
  onSelectedColumnChange(value: string): void;
  onTemplateChange(value: string): void;
}) {
  const selectColumnSource = (value: string) => {
    if (value === TEMPLATE_SOURCE_SELECT_VALUE) {
      onModeChange('template');
      return;
    }
    onModeChange('column');
    onSelectedColumnChange(value);
  };

  const isTemplate = mode === 'template' && allowTemplate;
  // The three-mode (All / Column(s) / Template) rendering is entirely behind
  // `multiColumn`; callers that omit it retain the single-column path.
  if (multiColumn) {
    const isAll = mode === 'all';
    const isMultiTemplate = mode === 'template' && allowTemplate;
    return (
      <div
        className="action-source-block source-input-control"
        data-testid={`${testIdPrefix}-source-mode`}
      >
        <div className="source-input-head">
          <div className="form-label">{label}</div>
          <SegmentedToggle
            className="source-mode-switch"
            fullWidth={false}
            ariaLabel={`${label} mode`}
            value={isMultiTemplate ? 'template' : isAll ? 'all' : 'columns'}
            onValueChange={(next) => onModeChange(next as SourceInputMode)}
            buttonTestId={(value) => `${testIdPrefix}-source-mode-${value}`}
            options={[
              { value: 'all', label: multiColumn.allModeLabel ?? 'All text columns' },
              { value: 'columns', label: multiColumn.columnsModeLabel ?? 'Column(s)' },
              ...(allowTemplate
                ? [{ value: 'template', label: '{ } Template' }]
                : []),
            ]}
          />
        </div>
        {isMultiTemplate ? (
          <TemplateComposer
            value={template}
            columns={columns}
            textareaTestId={templateInputTestId ?? `${testIdPrefix}-source-template-input`}
            insertTestId={`${testIdPrefix}-source-template-column-insert`}
            ariaLabel={`${ariaLabel} template`}
            onChange={onTemplateChange}
            onUseColumn={() => onModeChange('columns')}
          />
        ) : isAll ? (
          // "All" is a UI convenience only: it is expanded to this EXPLICIT
          // ordered column list at submit, never sent as an empty list or a
          // boolean. Naming the columns here is the same promise the receipt
          // will record.
          <p className="form-hint" data-testid={`${testIdPrefix}-source-all-columns`}>
            {columns.length
              ? `Reads ${columns.length === 1 ? 'the 1 compatible column' : `all ${columns.length} compatible columns`}, in order: ${columns.map((column) => column.name).join(', ')}.`
              : emptyMessage}
          </p>
        ) : (
          <>
            <MultiColumnPicker
              testId={`${testIdPrefix}-source-columns`}
              ariaLabel={ariaLabel}
              value={multiColumn.selectedColumns}
              options={columns.map((column) => ({
                name: column.name,
                type: column.type,
                ai: Boolean(column.ai),
              }))}
              onValueChange={multiColumn.onSelectedColumnsChange}
            />
            {!columns.length && (
              <p className="form-hint" data-testid={`${testIdPrefix}-source-empty`}>
                {emptyMessage}
              </p>
            )}
          </>
        )}
      </div>
    );
  }
  return (
    <div className="action-source-block source-input-control" data-testid={`${testIdPrefix}-source-mode`}>
      <div className="source-input-head">
        <div className="form-label">{label}</div>
        {allowTemplate && (
          <SegmentedToggle
            className="source-mode-switch"
            fullWidth={false}
            ariaLabel={`${label} mode`}
            value={isTemplate ? 'template' : 'column'}
            onValueChange={(next) => onModeChange(next as SourceMode)}
            buttonTestId={(value) => `${testIdPrefix}-source-mode-${value}`}
            options={[
              { value: 'column', label: 'Column' },
              { value: 'template', label: '{ } Template' },
            ]}
          />
        )}
      </div>
      {!isTemplate ? (
        <>
          <PanelSelect
            testId={`${testIdPrefix}-source-column-select`}
            ariaLabel={ariaLabel}
            value={selectedColumn || ''}
            onValueChange={selectColumnSource}
            options={[
              ...(columns.length === 0
                ? [{ value: '', label: 'No compatible columns', disabled: true }]
                : []),
              ...columnSelectOptions(columns),
              ...(allowTemplate
                ? [{
                    value: TEMPLATE_SOURCE_SELECT_VALUE,
                    label: 'Template',
                    description: 'Compose the input from several columns with {{tokens}}',
                  }]
                : []),
            ]}
          />
          {!columns.length && (
          <p className="form-hint" data-testid={`${testIdPrefix}-source-empty`}>
            {emptyMessage}
          </p>
          )}
        </>
      ) : (
        <TemplateComposer
          value={template}
          columns={columns}
          textareaTestId={templateInputTestId ?? `${testIdPrefix}-source-template-input`}
          insertTestId={`${testIdPrefix}-source-template-column-insert`}
          ariaLabel={`${ariaLabel} template`}
          onChange={onTemplateChange}
          onUseColumn={() => onModeChange('column')}
        />
      )}
    </div>
  );
}
