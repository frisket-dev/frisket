import { useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { Check, Copy, Eye, EyeOff, LockKeyhole, Play, Plus, RotateCcw, Square, X } from 'lucide-react';
import type { ActionComparisonBoundedValue } from '../api/actionComparison';
import type { GeneratedActionDraft } from '../api/types';
import { Button } from '../components/Button';
import { IconButton } from '../components/IconButton';
import { OverflowRow } from '../components/OverflowRow';
import { StatusChip, type StatusTone } from '../components/PanelPrimitives';
import { CompareToolbar } from './CompareLayout';
import {
  type ActionCompareSession,
  type ActionCompareUseSettings,
  type ActionCompareVariant,
} from './actionCompareSession';
import { ActionCompareGrid, type ActionCompareStructuredValue } from './ActionCompareGrid';
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
  projectId: string;
  session: ActionCompareSession;
  rowHeight: number;
  wrapText: boolean;
  frozenColumnCount: number;
  onUseSettings(selection: ActionCompareUseSettings): void;
  renderVariantEditor?(props: ActionCompareVariantEditorRenderProps): ReactNode;
  labelForDraft?(draft: GeneratedActionDraft, displayKey: string): string;
  describeVariantChanges?(baseline: GeneratedActionDraft, variant: GeneratedActionDraft): readonly ActionCompareSettingChange[];
  onOpenBoundedValue?(args: {
    value: ActionComparisonBoundedValue; previewId: string | null; rowId: number; columnKey: string;
  }): void;
  onOpenStructuredValue?(value: ActionCompareStructuredValue): void;
  onRefresh?(): void;
  className?: string;
}

function actionCompareEditorMaxHeight(toolbarBottom: number, contentBottom: number): number {
  return Math.max(80, Math.floor(contentBottom - toolbarBottom - 12));
}

const sampleLabel = (sampled: number, selected: number | null) => selected !== null && selected > sampled
  ? `${sampled} of ${selected} selected rows`
  : `${sampled} sample ${sampled === 1 ? 'row' : 'rows'}`;

function statusTone(status: ActionCompareVariant['status']): StatusTone {
  if (status === 'ready' || status === 'current') return 'success';
  if (status === 'failed' || status === 'expired') return 'error';
  return status === 'running' ? 'info' : 'neutral';
}

function statusLabel(variant: ActionCompareVariant): string {
  return variant.status === 'current' ? 'in table' : variant.status;
}

export function ActionCompareTab({ projectId, session, rowHeight, wrapText, frozenColumnCount,
  onUseSettings, renderVariantEditor,
  labelForDraft, describeVariantChanges, onOpenBoundedValue, onOpenStructuredValue,
  onRefresh, className }: ActionCompareTabProps) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot, session.getSnapshot);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const [editorMaxHeight, setEditorMaxHeight] = useState<number | null>(null);
  const sectionRef = useRef<HTMLElement>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const { context } = state;
  const baseline = state.variants[0];
  const hidden = new Set(state.hiddenColumnKeys);
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
      if (sectionRect.height <= 0 || toolbarRect.height <= 0) return;
      const next = actionCompareEditorMaxHeight(toolbarRect.bottom, sectionRect.bottom);
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
    return () => observer.disconnect();
  }, [editing]);

  const controls = <>
    {running ? <Button type="button" onClick={session.stop}><Square size={13} /> Stop</Button>
      : <Button type="button" disabled={!pending} onClick={() => void session.runPending()}><Play size={13} /> Run comparison</Button>}
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
      <CompareToolbar testIdPrefix="action-compare" running={running} controls={controls} label={null} tag={null}>
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
              <strong className="action-compare-variant-key">{variant.displayKey}</strong><span>{displayLabel}</span>
              <StatusChip tone={statusTone(variant.status)} size="sm">{statusLabel(variant)}</StatusChip>
            </div> : <button type="button" className="action-compare-variant-body"
              aria-label={`Configure variant ${variant.displayKey}`} onClick={() => setEditingId(variant.id)}>
              <strong className="action-compare-variant-key">{variant.displayKey}</strong><span>{displayLabel}</span>
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
              <strong><span className="action-compare-variant-key">{variant.displayKey}</span> {displayLabel}</strong>
              {changes.map((change) => <div key={change.label}><span>{change.label}</span>
                <span className="action-compare-setting-values"><del>{change.before}</del><ins>{change.after}</ins></span>
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
        <header><strong><span className="action-compare-variant-key">{editing.displayKey}</span> {editingLabel}</strong>
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
      <span className="action-compare-sample-count" data-testid="action-compare-sample-count">{sampleLabel(
        Math.min(20, context.limits.max_sample_rows, context.sample_row_ids.length),
        state.capturedSelection?.totalCount ?? null,
      )}</span>
    </div>

    {state.stale && <div className="action-compare-stale" role="alert"><span>Data changed since this comparison.</span>
      {onRefresh && <Button type="button" onClick={onRefresh}>Refresh comparison</Button>}</div>}

    <ActionCompareGrid projectId={projectId} state={state} rowHeight={rowHeight}
      wrapText={wrapText} frozenColumnCount={frozenColumnCount} labelForDraft={labelForDraft}
      onOpenBoundedValue={onOpenBoundedValue} onOpenStructuredValue={onOpenStructuredValue} />
  </section>;
}
