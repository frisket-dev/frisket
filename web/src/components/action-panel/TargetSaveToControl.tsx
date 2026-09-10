import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import type { ColumnDef, SheetMeta } from '../../api/open';
import { existingColumnByName, NEW_COLUMN } from './formControlHelpers';
import styles from './TargetSaveToControl.module.css';

export { NEW_COLUMN };

function outputNameOptionId(testid: string): string {
  return `${testid}-options`;
}

function outputNameListboxId(testid: string): string {
  return `${testid}-listbox`;
}

function optionSlug(value: string): string {
  const slug = value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  return slug || 'column';
}

function outputNameRichOptionId(testid: string, columnId: string): string {
  return `${testid}-option-id-${optionSlug(columnId)}`;
}

function filterOutputColumns(columns: ColumnDef[], query: string): ColumnDef[] {
  const normalized = query.trim().toLowerCase();
  if (!normalized) return columns;
  return columns.filter((column) => (
    column.name.toLowerCase().includes(normalized) ||
    column.type.toLowerCase().includes(normalized)
  ));
}

export function OutputNameCombobox({
  columns,
  value,
  onChange,
  label = 'Save to',
  inputTestId = 'new-column-name',
  allowExistingTargets = true,
}: {
  columns: ColumnDef[];
  value: string;
  onChange(value: string): void;
  label?: string;
  inputTestId?: string;
  allowExistingTargets?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [filterQuery, setFilterQuery] = useState('');
  const [activeOptionId, setActiveOptionId] = useState<string | null>(null);
  const [warningColumnName, setWarningColumnName] = useState<string | null>(null);
  const comboboxRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const generatedInputId = useId();
  const overwriteColumn = existingColumnByName(columns, value);
  // A typed name that doesn't match any existing column is a NEW column —
  // surface that plainly instead of only implying it via the datalist's
  // empty-state text (the "New column" line inside the options popover,
  // which only shows while the popover is open).
  const isNewColumnName = value.trim().length > 0 && !overwriteColumn;
  const datalistId = outputNameOptionId(inputTestId);
  const listboxId = outputNameListboxId(inputTestId);
  const visibleColumns = useMemo(
    () => allowExistingTargets ? filterOutputColumns(columns, filterQuery) : [],
    [allowExistingTargets, columns, filterQuery],
  );
  const warningText = overwriteColumn
    ? `This will overwrite the existing "${overwriteColumn.name}" column.`
    : '';
  const warningOpen = Boolean(overwriteColumn && warningColumnName === overwriteColumn.name);
  const effectiveActiveOptionId = open
    ? (
      visibleColumns.some((column) => column.id === activeOptionId)
        ? activeOptionId
        : visibleColumns.find((column) => column.name === value)?.id
          ?? visibleColumns[0]?.id
          ?? null
    )
    : null;

  useEffect(() => {
    if (!effectiveActiveOptionId) return;
    const activeOption = document.getElementById(
      outputNameRichOptionId(inputTestId, effectiveActiveOptionId),
    );
    if (typeof activeOption?.scrollIntoView === 'function') {
      activeOption.scrollIntoView({ block: 'nearest' });
    }
  }, [effectiveActiveOptionId, inputTestId]);

  useEffect(() => {
    if (!open) return;
    const closeIfOutside = (event: PointerEvent | FocusEvent) => {
      const target = event.target;
      if (target instanceof Node && comboboxRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener('pointerdown', closeIfOutside);
    document.addEventListener('focusin', closeIfOutside);
    return () => {
      document.removeEventListener('pointerdown', closeIfOutside);
      document.removeEventListener('focusin', closeIfOutside);
    };
  }, [open]);

  return (
    <>
      <div className={styles.row}>
        <label className={`form-label ${styles.label}`} htmlFor={generatedInputId}>{label}</label>
        <div className={styles.combobox} ref={comboboxRef}>
          <input
            ref={inputRef}
            id={generatedInputId}
            className={`form-input ${styles.input}`}
            value={value}
            list={allowExistingTargets ? datalistId : undefined}
            aria-expanded={open}
            aria-controls={listboxId}
            aria-autocomplete="list"
            aria-activedescendant={effectiveActiveOptionId
              ? outputNameRichOptionId(inputTestId, effectiveActiveOptionId)
              : undefined}
            data-testid={inputTestId}
            onFocus={() => {
              setFilterQuery('');
              setActiveOptionId(columns.find((column) => column.name === value)?.id ?? columns[0]?.id ?? null);
              setOpen(true);
            }}
            onClick={() => {
              setFilterQuery('');
              setActiveOptionId(columns.find((column) => column.name === value)?.id ?? columns[0]?.id ?? null);
              setOpen(true);
            }}
            onChange={(e) => {
              const nextValue = e.target.value;
              const nextVisibleColumns = filterOutputColumns(columns, nextValue);
              onChange(nextValue);
              setFilterQuery(nextValue);
              setActiveOptionId((current) => (
                nextVisibleColumns.some((column) => column.id === current)
                  ? current
                  : nextVisibleColumns[0]?.id ?? null
              ));
              setOpen(true);
              setWarningColumnName(null);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Escape') {
                e.preventDefault();
                setOpen(false);
                return;
              }
              if (e.key === 'Enter') {
                e.preventDefault();
                const activeColumn = visibleColumns.find(
                  (column) => column.id === effectiveActiveOptionId,
                );
                if (activeColumn) {
                  onChange(activeColumn.name);
                  setFilterQuery('');
                  setWarningColumnName(null);
                }
                setOpen(false);
                return;
              }
              if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
              e.preventDefault();
              if (!open) {
                setActiveOptionId(
                  visibleColumns.find((column) => column.name === value)?.id
                    ?? visibleColumns[0]?.id
                    ?? null,
                );
                setOpen(true);
                return;
              }
              setOpen(true);
              setActiveOptionId(() => {
                if (!visibleColumns.length) return null;
                const currentIndex = visibleColumns.findIndex(
                  (column) => column.id === effectiveActiveOptionId,
                );
                if (currentIndex < 0) return visibleColumns[0].id;
                const delta = e.key === 'ArrowDown' ? 1 : -1;
                const nextIndex = Math.max(0, Math.min(visibleColumns.length - 1, currentIndex + delta));
                return visibleColumns[nextIndex].id;
              });
            }}
          />
          {allowExistingTargets && (
            <button
              type="button"
              className={styles.toggle}
              data-testid={`${inputTestId}-toggle`}
              aria-label="Show existing columns"
              tabIndex={-1}
              onClick={() => {
                setFilterQuery('');
                if (open) {
                  setOpen(false);
                } else {
                  setActiveOptionId(columns.find((column) => column.name === value)?.id ?? columns[0]?.id ?? null);
                  setOpen(true);
                  inputRef.current?.focus();
                }
              }}
            />
          )}
          <datalist id={datalistId}>
            {allowExistingTargets && columns.map((column) => (
              <option key={column.id} value={column.name}>
                {column.name} ({column.type}){column.ai ? ' ⚡' : ''}
              </option>
            ))}
          </datalist>
          {open && (
            <div
              className={styles.options}
              role="listbox"
              id={listboxId}
              data-testid={listboxId}
            >
              {visibleColumns.length ? visibleColumns.map((column) => (
                <button
                  key={column.id}
                  type="button"
                  role="option"
                  id={outputNameRichOptionId(inputTestId, column.id)}
                  className={`${styles.option} ${effectiveActiveOptionId === column.id ? 'is-active' : ''}`}
                  data-testid={`${inputTestId}-option-${optionSlug(column.name)}`}
                  aria-selected={column.name === value}
                  tabIndex={-1}
                  onMouseDown={(e) => e.preventDefault()}
                  onMouseEnter={() => setActiveOptionId(column.id)}
                  onClick={() => {
                    onChange(column.name);
                    setFilterQuery('');
                    setOpen(false);
                    setWarningColumnName(null);
                  }}
                >
                  <span>{column.name}</span>
                  <span className="muted">{column.type}{column.ai ? ' AI' : ''}</span>
                </button>
              )) : (
                <div className={styles.optionEmpty} data-testid={`${inputTestId}-no-options`}>
                  New column
                </div>
              )}
            </div>
          )}
        </div>
        {allowExistingTargets && overwriteColumn ? (
          <button
            type="button"
            className={`icon-btn ${styles.warning}`}
            data-testid="save-to-overwrite-warning"
            aria-label={warningText}
            title={warningText}
            onClick={() => setWarningColumnName((current) => (
              current === overwriteColumn.name ? null : overwriteColumn.name
            ))}
          >
            <AlertTriangle size={14} />
          </button>
        ) : isNewColumnName ? (
          <span
            className={styles.newBadge}
            data-testid="save-to-new-column-badge"
            title={`"${value.trim()}" will be created as a new column.`}
          >
            New
          </span>
        ) : null}
      </div>
      {allowExistingTargets && warningOpen && overwriteColumn && (
        <p className={`form-hint ${styles.warningText}`} data-testid="save-to-overwrite-message">
          {warningText}
        </p>
      )}
    </>
  );
}

function targetColumnName(sheet: SheetMeta, targetCol: string, newColName: string): string {
  if (targetCol === NEW_COLUMN) return newColName;
  return sheet.columns.find((column) => String(column.id) === targetCol)?.name ?? newColName;
}

export function TargetSaveToControl({
  sheet,
  targetCol,
  newColName,
  label = 'Save to',
  error = null,
  defaultColumnNameCollision = null,
  allowExistingTargets = true,
  onTargetColChange,
  onNewColNameChange,
}: {
  sheet: SheetMeta;
  targetCol: string;
  newColName: string;
  label?: string;
  error?: string | null;
  defaultColumnNameCollision?: { original: string; renamed: string } | null;
  allowExistingTargets?: boolean;
  onTargetColChange(value: string): void;
  onNewColNameChange(value: string): void;
}) {
  const value = targetColumnName(sheet, targetCol, newColName);
  return (
    <>
      <OutputNameCombobox
        columns={sheet.columns}
        value={value}
        label={label}
        allowExistingTargets={allowExistingTargets}
        onChange={(next) => {
          const existing = existingColumnByName(sheet.columns, next);
          if (allowExistingTargets && existing) {
            onTargetColChange(String(existing.id));
            onNewColNameChange(existing.name);
          } else {
            onTargetColChange(NEW_COLUMN);
            onNewColNameChange(next);
          }
        }}
      />
      {error && <p className="form-error" data-testid="new-column-error">{error}</p>}
      {defaultColumnNameCollision &&
        targetCol === NEW_COLUMN &&
        newColName === defaultColumnNameCollision.renamed && (
          <p className="form-hint" data-testid="default-column-name-collision-note">
            A "{defaultColumnNameCollision.original}" column already exists — saving to "
            {defaultColumnNameCollision.renamed}".
          </p>
        )}
    </>
  );
}
