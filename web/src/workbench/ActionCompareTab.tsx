import { useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { CircleAlert, Check, Copy, Eye, EyeOff, LockKeyhole, Plus, RefreshCw, RotateCcw, Square, X } from 'lucide-react';
import type { ActionComparisonBoundedValue } from '../api/actionComparison';
import type { GeneratedActionDraft, JsonValue } from '../api/types';
import { Button } from '../components/Button';
import { IconButton } from '../components/IconButton';
import { OverflowRow } from '../components/OverflowRow';
import { StatusChip, type StatusTone } from '../components/PanelPrimitives';
import { CompareToolbar } from './CompareLayout';
import {
  actionCompareCellEquality,
  type ActionCompareResultCell,
  type ActionCompareSession,
  type ActionCompareUseSettings,
  type ActionCompareVariant,
} from './actionCompareSession';
import './ActionCompareTab.css';

export interface ActionCompareVariantEditorRenderProps {
  variantId: string;
  label: string;
  draft: GeneratedActionDraft;
  lockedParams: readonly string[];
  structureLocked: true;
  lockReason: 'Fixed for this comparison';
  onDraftChange(draft: GeneratedActionDraft): void;
  onDuplicate(): void;
  onDismiss(): void;
}

export interface ActionCompareSettingChange { label: string; before: string; after: string }

export interface ActionCompareTabProps {
  session: ActionCompareSession;
  onUseSettings(selection: ActionCompareUseSettings): void;
  renderVariantEditor?(props: ActionCompareVariantEditorRenderProps): ReactNode;
  labelForDraft?(draft: GeneratedActionDraft, displayKey: string): string;
  describeVariantChanges?(baseline: GeneratedActionDraft, variant: GeneratedActionDraft): readonly ActionCompareSettingChange[];
  onOpenBoundedValue?(args: {
    value: ActionComparisonBoundedValue; previewId: string | null; rowId: number; columnKey: string;
  }): void;
  onRefresh?(): void;
  className?: string;
}

function actionCompareEditorMaxHeight(toolbarBottom: number, contentBottom: number): number {
  return Math.max(80, Math.floor(contentBottom - toolbarBottom - 12));
}

const sampleLabel = (sampled: number, selected: number | null) => selected !== null && selected > sampled
  ? `Comparing ${sampled} of ${selected} selected rows`
  : `Comparing ${sampled} sample ${sampled === 1 ? 'row' : 'rows'}`;

function statusTone(status: ActionCompareVariant['status']): StatusTone {
  if (status === 'ready' || status === 'current') return 'success';
  if (status === 'failed' || status === 'expired') return 'error';
  return status === 'running' ? 'info' : 'neutral';
}

function statusLabel(variant: ActionCompareVariant): string {
  return variant.status === 'current' ? 'in table' : variant.status;
}

function Value({ value, depth = 0 }: { value: JsonValue; depth?: number }) {
  if (value === null) return <span className="action-compare-empty">Empty</span>;
  if (typeof value === 'string') return <span className="action-compare-text">{value || 'Empty text'}</span>;
  if (typeof value === 'number') return <span className="action-compare-number">{value}</span>;
  if (typeof value === 'boolean') return <span>{value ? 'True' : 'False'}</span>;
  if (depth >= 2) return <span className="action-compare-empty">Structured value</span>;
  if (Array.isArray(value)) return value.length
    ? <ul className="action-compare-list">{value.map((item, index) => <li key={index}><Value value={item} depth={depth + 1} /></li>)}</ul>
    : <span className="action-compare-empty">Empty list</span>;
  const entries = Object.entries(value);
  return entries.length ? <dl className="action-compare-object">{entries.map(([key, item]) => <div key={key}>
    <dt>{key}</dt><dd><Value value={item} depth={depth + 1} /></dd>
  </div>)}</dl> : <span className="action-compare-empty">Empty object</span>;
}

function CellValue({ cell, previewId, rowId, columnKey, onOpen }: {
  cell: ActionCompareResultCell | undefined;
  previewId: string | null;
  rowId: number;
  columnKey: string;
  onOpen?: ActionCompareTabProps['onOpenBoundedValue'];
}) {
  const [expanded, setExpanded] = useState(false);
  if (!cell || cell.state === 'pending') return <span className="action-compare-empty">Pending</span>;
  if (cell.state === 'cancelled') return <span className="action-compare-empty">Cancelled</span>;
  if (cell.state === 'missing') return <span className="action-compare-empty">No result</span>;
  if (cell.state === 'error') return <span className="action-compare-error"><CircleAlert size={13} aria-hidden />
    <strong>Error:</strong> {cell.error || 'Row failed'}</span>;
  if (cell.state === 'null') return <span className="action-compare-empty">Empty</span>;
  if (cell.bounded_value) return <div className="action-compare-bounded">
    <span>{cell.bounded_value.preview}</span>
    <small>Not compared · {cell.bounded_value.total_bytes.toLocaleString()} bytes</small>
    {onOpen && <Button type="button" variant="text" onClick={() => onOpen({
      value: cell.bounded_value!, previewId, rowId, columnKey,
    })}>Open full value</Button>}
  </div>;
  const value = cell.value ?? null;
  const expandable = typeof value === 'string' ? value.length > 160
    : value !== null && typeof value === 'object';
  return <div className="action-compare-inline-value" data-expandable={expandable ? 'true' : undefined}
    data-expanded={expanded ? 'true' : 'false'}>
    <div className="action-compare-inline-value-content"><Value value={value} /></div>
    {expandable && <Button type="button" variant="text" onClick={() => setExpanded((current) => !current)}>
      {expanded ? 'Collapse' : 'Expand'}
    </Button>}
  </div>;
}

function Origin({ cell }: { cell: ActionCompareResultCell | undefined }) {
  if (cell?.origin?.review_decision === 'reject_clear') return <span className="action-compare-origin">Cleared rejection</span>;
  if (cell?.origin?.review_decision === 'edit' || cell?.origin?.kind === 'manual_edit') {
    return <span className="action-compare-origin">Edited</span>;
  }
  return null;
}

function displayedCell(
  variant: ActionCompareVariant,
  cell: ActionCompareResultCell | undefined,
): ActionCompareResultCell {
  if (cell) return cell;
  if (variant.status === 'pending' || variant.status === 'running') return { state: 'pending' };
  if (variant.status === 'cancelled') return { state: 'cancelled' };
  if (variant.status === 'failed' || variant.status === 'expired') {
    return { state: 'error', error: variant.error ?? 'Preview failed' };
  }
  return { state: 'missing' };
}

export function ActionCompareTab({ session, onUseSettings, renderVariantEditor,
  labelForDraft, describeVariantChanges, onOpenBoundedValue, onRefresh, className }: ActionCompareTabProps) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot, session.getSnapshot);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [editorMaxHeight, setEditorMaxHeight] = useState<number | null>(null);
  const sectionRef = useRef<HTMLElement>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const { context } = state;
  const baseline = state.variants[0];
  const hidden = new Set(state.hiddenColumnKeys);
  const sources = context.source_columns.filter((column) => !hidden.has(`source:${column.name}`));
  const outputs = context.output_columns.filter((column) => !hidden.has(`output:${column.logical_key}`));
  const editing = state.variants.find((variant) => variant.id === editingId) ?? null;
  const editingLabel = editing ? labelForDraft?.(editing.draft, editing.displayKey) ?? editing.label : null;
  const running = state.sequenceStatus === 'running';
  const pending = state.variants.some((variant) => variant.status === 'pending');
  const visibilityFields = [
    ...context.source_columns.map((column) => ({ key: `source:${column.name}`, name: column.name })),
    ...context.output_columns.map((column) => ({ key: `output:${column.logical_key}`, name: column.current_name })),
  ];

  useLayoutEffect(() => {
    if (!editing) return undefined;
    const section = sectionRef.current;
    const toolbar = toolbarRef.current;
    if (!section || !toolbar) return undefined;
    const measure = () => {
      const sectionRect = section.getBoundingClientRect();
      const toolbarRect = toolbar.getBoundingClientRect();
      const footerTop = section.querySelector('footer')?.getBoundingClientRect().top ?? sectionRect.bottom;
      if (sectionRect.height <= 0 || toolbarRect.height <= 0) return;
      const next = actionCompareEditorMaxHeight(toolbarRect.bottom, footerTop);
      setEditorMaxHeight((current) => current === next ? current : next);
    };
    measure();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure);
      return () => window.removeEventListener('resize', measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(section);
    observer.observe(toolbar);
    const footer = section.querySelector('footer');
    if (footer) observer.observe(footer);
    return () => observer.disconnect();
  }, [editing]);

  const controls = <>
    {running ? <Button type="button" onClick={session.stop}><Square size={13} /> Stop</Button>
      : <Button type="button" disabled={!pending} onClick={() => void session.runPending()}><RefreshCw size={13} /> Re-run</Button>}
    <Button type="button" aria-pressed={state.highlightDifferences}
      aria-label={state.highlightDifferences ? 'Hide difference highlighting' : 'Highlight differences'}
      onClick={() => session.setHighlightDifferences(!state.highlightDifferences)}>
      {state.highlightDifferences ? <Eye size={13} /> : <EyeOff size={13} />} Highlight differences
    </Button>
  </>;

  return <section ref={sectionRef} className={['action-compare', className].filter(Boolean).join(' ')} aria-label="Action comparison">
    {!context.available && <div className="action-compare-stale" role="alert">
      <span>{context.reason?.message ?? 'This action cannot be compared.'}</span>
    </div>}
    <div ref={toolbarRef} className="action-compare-toolbar-shell">
      <CompareToolbar testIdPrefix="action-compare" running={running} controls={controls} label="Variants"
        tag={sampleLabel(context.sample_row_ids.length, state.capturedSelection?.totalCount ?? null)}>
        {state.variants.map((variant) => {
          const use = session.useVariant(variant.id);
          const displayLabel = labelForDraft?.(variant.draft, variant.displayKey) ?? variant.label;
          const changes = editingId === null && hoveredId === variant.id && !variant.baseline && describeVariantChanges
            ? describeVariantChanges(baseline.draft, variant.draft) : [];
          return <div key={variant.id} className="action-compare-variant" data-status={variant.status}
            data-active={editingId === variant.id || hoveredId === variant.id ? 'true' : undefined}
            data-testid={`action-compare-variant-${variant.displayKey}`}
            onMouseEnter={() => setHoveredId(variant.id)} onMouseLeave={() => setHoveredId(null)}>
            {variant.baseline ? <div className="action-compare-variant-body" aria-label={`Variant ${variant.displayKey}`}>
              <i className="action-compare-variant-pip" aria-hidden /><strong>{variant.displayKey}</strong><span>{displayLabel}</span>
              <StatusChip tone={statusTone(variant.status)} size="sm">{statusLabel(variant)}</StatusChip>
            </div> : <button type="button" className="action-compare-variant-body"
              aria-label={`Configure variant ${variant.displayKey}`} onClick={() => setEditingId(variant.id)}>
              <i className="action-compare-variant-pip" aria-hidden /><strong>{variant.displayKey}</strong><span>{displayLabel}</span>
              <StatusChip tone={statusTone(variant.status)} size="sm">{statusLabel(variant)}</StatusChip>
            </button>}
            {use && <IconButton icon={Check} size="inline" className="action-compare-use"
              label={`Use settings from variant ${variant.displayKey}`} onClick={() => onUseSettings(use)} />}
            {variant.baseline ? <LockKeyhole className="action-compare-lock" size={12} aria-label="Baseline cannot be removed" />
              : <IconButton icon={X} size="inline" className="action-compare-remove"
                label={`Remove variant ${variant.displayKey}`} onClick={() => {
                  setEditingId((current) => current === variant.id ? null : current); session.removeVariant(variant.id);
                }} />}
            {['failed', 'cancelled', 'expired'].includes(variant.status) && <IconButton icon={RotateCcw} size="inline"
              label={`Retry variant ${variant.displayKey}`} onClick={() => session.retryVariant(variant.id)} />}
            {changes.length > 0 && <div className="action-compare-settings" role="tooltip">
              <strong>{variant.displayKey} · {displayLabel}</strong>
              {changes.map((change) => <div key={change.label}><span>{change.label}</span>
                <span className="action-compare-setting-values"><del>{change.before}</del><span aria-hidden>→</span><ins>{change.after}</ins></span>
              </div>)}
              <small>Click to edit</small>
            </div>}
          </div>;
        })}
        <Button type="button" className="action-compare-add" disabled={state.variants.length >= Math.min(3, context.limits.max_challengers) + 1}
          onClick={() => { const id = session.addVariant(); if (id) setEditingId(id); }}><Plus size={13} /> Variant</Button>
      </CompareToolbar>

      {editing && renderVariantEditor && <aside className="action-compare-editor" style={editorMaxHeight === null
        ? undefined : { maxHeight: editorMaxHeight }} aria-label={`Configure variant ${editing.displayKey}`}>
        <header><strong>{editing.displayKey} · {editingLabel}</strong>
          <IconButton icon={X} size="inline" label="Close variant editor" onClick={() => setEditingId(null)} /></header>
        <div className="action-compare-editor-body">
          {renderVariantEditor({
            variantId: editing.id, label: editing.displayKey, draft: editing.draft,
            lockedParams: context.locked_params, structureLocked: true, lockReason: 'Fixed for this comparison',
            onDraftChange: (draft) => session.updateVariant(editing.id, draft),
            onDuplicate: () => { const id = session.addVariant(editing.id); if (id) setEditingId(id); },
            onDismiss: () => setEditingId(null),
          })}
          <Button type="button" onClick={() => { const id = session.addVariant(editing.id); if (id) setEditingId(id); }}>
            <Copy size={13} /> Duplicate
          </Button>
        </div>
      </aside>}
    </div>

    <div className="action-compare-visibility"><span>Columns</span>
      <OverflowRow items={visibilityFields} getKey={(field) => field.key} aria-label="Visible comparison fields"
        className="action-compare-visibility-row" overflowLabel="More comparison fields"
        triggerClassName="action-compare-visibility-overflow"
        renderItem={({ key, name }) => { const visible = !hidden.has(key); return <Button type="button" aria-pressed={visible}
          aria-label={`${visible ? 'Hide' : 'Show'} ${name}`} onClick={() => session.setColumnVisible(key, !visible)}>
          {visible ? '✓' : '+'} {name}
        </Button>; }}
        renderOverflowItem={({ key, name }, closeMenu) => { const visible = !hidden.has(key); return <button type="button"
          role="menuitemcheckbox" aria-checked={visible} className="menu-item" onClick={() => {
            session.setColumnVisible(key, !visible); closeMenu();
          }}><span aria-hidden>{visible ? '✓' : '+'}</span><span className="menu-item-name">{name}</span></button>; }} />
    </div>

    {(context.origin_summary.mixed_runs || context.origin_summary.includes_manual_edits || context.origin_summary.includes_reject_clear)
      && <div className="action-compare-notice" role="note">
        {context.origin_summary.mixed_runs && <span>Includes other runs.</span>}
        {(context.origin_summary.includes_manual_edits || context.origin_summary.includes_reject_clear)
          && <span>Includes edited values.</span>}
      </div>}
    {state.stale && <div className="action-compare-stale" role="alert"><span>Data changed since this comparison.</span>
      {onRefresh && <Button type="button" onClick={onRefresh}>Refresh comparison</Button>}</div>}

    <div className="action-compare-table-scroll"><table className="action-compare-table"><thead><tr>
      <th rowSpan={2} scope="col" aria-label="Row">#</th>
      {sources.map((column, sourceIndex) => <th key={column.column_id} rowSpan={2} scope="col" className="action-compare-source-sticky"
        style={{ left: 48 + sourceIndex * 150 }}>{column.name}<small>{column.param}</small></th>)}
      {outputs.map((column) => <th key={column.logical_key} colSpan={state.variants.length} scope="colgroup" aria-label={column.current_name}>
        <span aria-hidden>⚡</span> {column.current_name}{column.hidden && <small>hidden output</small>}
      </th>)}
    </tr><tr>{outputs.flatMap((output) => state.variants.map((variant) => <th key={`${output.logical_key}:${variant.id}`} scope="col">
      {variant.displayKey} · {labelForDraft?.(variant.draft, variant.displayKey) ?? variant.label}
    </th>))}</tr></thead><tbody>
      {context.sample_row_ids.map((rowId, index) => <tr key={rowId}><th scope="row">{index + 1}</th>
        {sources.map((source, sourceIndex) => { const cell = context.source_rows[String(rowId)]?.[source.name]; return <td key={source.column_id}
          className="action-compare-source-sticky" style={{ left: 48 + sourceIndex * 150 }}>
          <CellValue cell={cell} previewId={null} rowId={rowId} columnKey={source.name} onOpen={onOpenBoundedValue} />
        </td>; })}
        {outputs.flatMap((output) => state.variants.map((variant) => {
          const displayed = variant.result ?? variant.previousResult;
          const cell = displayed?.rows[String(rowId)]?.[output.logical_key];
          const original = baseline.result?.rows[String(rowId)]?.[output.logical_key];
          const explicitValueState = cell && ['value', 'null'].includes(cell.state)
            && original && ['value', 'null'].includes(original.state);
          const comparison = variant.baseline || !explicitValueState ? undefined : actionCompareCellEquality(original, cell);
          const highlightedComparison = state.highlightDifferences ? comparison : undefined;
          return <td key={`${output.logical_key}:${variant.id}`}
            data-testid={`action-compare-cell-${rowId}-${output.logical_key}-${variant.displayKey}`}
            data-comparison={highlightedComparison}
            data-cell-state={cell?.state ?? variant.status}
            data-previous={!variant.result && variant.previousResult ? 'true' : undefined}>
            {highlightedComparison === 'different' && <span className="sr-only">Differs from A. </span>}
            {!variant.result && variant.previousResult && <small>Previous settings</small>}
            <Origin cell={cell} />
            <CellValue cell={displayedCell(variant, cell)}
              previewId={variant.previewId} rowId={rowId} columnKey={output.logical_key} onOpen={onOpenBoundedValue} />
          </td>;
        }))}
      </tr>)}
    </tbody></table></div>
    <footer><span>{context.baseline_mode === 'current_values'
      ? 'A shows current values' : 'A uses the original settings'}</span>
      <span>Nothing changes until you use a variant.</span></footer>
  </section>;
}
