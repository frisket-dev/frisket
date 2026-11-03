import { useId, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import { Archive, PanelRightOpen, Copy, Eye, EyeOff, LockKeyhole, Plus, RotateCw, Settings2, X } from 'lucide-react';
import type { ActionComparisonBoundedValue } from '../api/actionComparison';
import type { GeneratedActionDraft } from '../api/types';
import type { EvaluationApi } from '../api/evaluation';
import { Button } from '../components/Button';
import { AnchoredMenu } from '../components/MenuPop';
import { IconButton } from '../components/IconButton';
import { WindowCloseButton } from '../components/WindowCloseButton';
import { useEscapeDismiss } from '../hooks/useEscapeDismiss';
import { OverflowRow } from '../components/OverflowRow';
import { StatusChip, type StatusTone } from '../components/PanelPrimitives';
import { CompareToolbar } from './CompareLayout';
import { CompareRunControl } from './CompareRunControl';
import {
  type ActionCompareSession,
  type ActionCompareUseSettings,
  type ActionCompareVariant,
} from './actionCompareSession';
import { ActionCompareGrid, type ActionCompareStructuredValue } from './ActionCompareGrid';
import { VariantBadge } from './VariantBadge';
import { ComparisonEvaluationWorkspace } from './evaluation/ComparisonEvaluationWorkspace';
import { PanelSelect } from '../components/PanelSelect';
import './ActionCompareTab.css';

export interface ActionCompareVariantEditorRenderProps {
  variantId: string;
  label: string;
  modelOnly?: boolean;
  modelDisabled?: boolean;
  onModelLabelChange?(label: string): void;
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
  className?: string;
  evaluationApi?: EvaluationApi;
  onJoinExpected?(): void;
  initialLens?: 'each_other' | 'expected';
  onExpectedAnswersChanged?(): void;
  onShelved?(): void;
  modelField?: string | null;
  instructionsField?: string | null;
  rowScope?: 'sample' | 'all' | 'expected';
  rowScopePending?: boolean;
  rowScopeTotal?: number | null;
  dataRevision?: number;
  onChangeRowScope?(scope: 'sample' | 'all' | 'expected', lens?: 'each_other' | 'expected'): void;
}


const sampleLabel = (sampled: number, selected: number | null) => selected !== null && selected > sampled
  ? `${sampled} of ${selected} selected rows`
  : `${sampled} sample ${sampled === 1 ? 'row' : 'rows'}`;

function statusTone(status: ActionCompareVariant['status']): StatusTone {
  if (status === 'failed' || status === 'unavailable') return 'error';
  return status === 'running' ? 'info' : 'neutral';
}

export function ActionCompareTab({ projectId, session, rowHeight, wrapText, frozenColumnCount,
  onUseSettings, renderVariantEditor,
  labelForDraft, describeVariantChanges, onOpenBoundedValue, onOpenStructuredValue,
  className, evaluationApi, onJoinExpected, initialLens, onExpectedAnswersChanged, onShelved,
  modelField, instructionsField, rowScope, rowScopePending, rowScopeTotal, dataRevision, onChangeRowScope }: ActionCompareTabProps) {
  const state = useSyncExternalStore(session.subscribe, session.getSnapshot, session.getSnapshot);
  const [variantMenuHost, setVariantMenuHost] = useState<HTMLDivElement | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const tooltipId = useId();
  const sectionRef = useRef<HTMLElement>(null);
  const toolbarRef = useRef<HTMLDivElement>(null);
  const editingAnchor = useRef<HTMLDivElement>(null);
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

  useEscapeDismiss(() => { setEditingId(null); setHoveredId(null); }, {
    enabled: editing !== null || hoveredId !== null, typingGuard: true,
  });


  const controls = <>
    <CompareRunControl testIdPrefix="action-compare" busy={running} disabled={!pending}
      onRun={() => void session.runPending()} onCancel={session.stop} />
    <Button type="button" aria-pressed={state.highlightDifferences}
      aria-label="Highlight differences"
      onClick={() => session.setHighlightDifferences(!state.highlightDifferences)}>
      {state.highlightDifferences ? <Eye size={13} /> : <EyeOff size={13} />} Highlight differences
    </Button>
  </>;

  return <section ref={sectionRef} className={['action-compare', className].filter(Boolean).join(' ')} aria-label="Action comparison">
    {!context.available && <div className="action-compare-notice" role="alert">
      <span>{context.reason?.message ?? 'This action cannot be compared.'}</span>
    </div>}
    <div ref={toolbarRef} className="action-compare-toolbar-shell">
      <CompareToolbar testIdPrefix="action-compare" running={running} controls={controls} label={null} tag={null}>
        {state.variants.map((variant) => {
          const use = session.useVariant(variant.id);
          const displayLabel = variant.readOnly ? variant.label : labelForDraft?.(variant.draft, variant.displayKey) ?? variant.label;
          const changes = editingId === null && hoveredId === variant.id && !variant.baseline && describeVariantChanges
            ? describeVariantChanges(baseline.draft, variant.draft) : [];
          return <div key={variant.id} ref={variant.id === editingId ? editingAnchor : undefined} className={`action-compare-variant${variant.readOnly ? ' action-compare-shelved' : ''}`} data-status={variant.status}
            data-active={editingId === variant.id || hoveredId === variant.id ? 'true' : undefined}
            data-testid={`action-compare-variant-${variant.displayKey}`}
            onMouseEnter={() => setHoveredId(variant.id)} onMouseLeave={() => setHoveredId(null)}
            onFocus={() => setHoveredId(variant.id)} onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget)) setHoveredId(null);
            }}>
            {variant.baseline || variant.readOnly ? <div className="action-compare-variant-body" aria-label={`Variant ${variant.displayKey}${variant.readOnly ? ', shelved, read-only' : ''}`}>
              <VariantBadge label={variant.displayKey} />{variant.readOnly && <Archive size={13} aria-label="Shelved, read-only" />}<span>{displayLabel}</span>
              {variant.status !== 'current' && variant.status !== 'ready' && variant.status !== 'pending' &&
                <StatusChip tone={statusTone(variant.status)} size="sm">{variant.status}</StatusChip>}
            </div> : <button type="button" className="action-compare-variant-body"
              aria-label={`Configure variant ${variant.displayKey}`}
              aria-describedby={changes.length > 0 ? tooltipId : undefined} onClick={() => setEditingId(variant.id)}>
              <VariantBadge label={variant.displayKey} /><span>{displayLabel}</span>
              {variant.status !== 'current' && variant.status !== 'ready' && variant.status !== 'pending' &&
                <StatusChip tone={statusTone(variant.status)} size="sm">{variant.status}</StatusChip>}
            </button>}
            <IconButton icon={Settings2} size="inline" label={`Settings for variant ${variant.displayKey}`} onClick={() => setEditingId(variant.id)} />
            {use && <IconButton icon={PanelRightOpen} size="inline"
              label={`Use settings from variant ${variant.displayKey}`} onClick={() => onUseSettings(use)} />}
            {variant.baseline ? <LockKeyhole className="action-compare-lock" size={12} aria-label="Baseline cannot be removed" />
              : <IconButton icon={X} size="inline" className="action-compare-remove"
                label={`Remove variant ${variant.displayKey}`} onClick={() => {
                  setEditingId((current) => current === variant.id ? null : current); session.removeVariant(variant.id);
                }} />}
            {['failed', 'cancelled', 'unavailable'].includes(variant.status) && <IconButton icon={RotateCw} size="inline"
              label={`Retry variant ${variant.displayKey}`} onClick={() => session.retryVariant(variant.id)} />}
            {changes.length > 0 && <div id={tooltipId} className="action-compare-settings" role="tooltip">
              <strong><VariantBadge label={variant.displayKey} /> {displayLabel}</strong>
              {changes.map((change) => <div key={change.label}><span>{change.label}</span>
                <span className="action-compare-setting-values"><del>{change.before}</del><ins>{change.after}</ins></span>
              </div>)}
              <small>Click to edit</small>
            </div>}
          </div>;
        })}
        {evaluationApi && <div ref={setVariantMenuHost} />}
        {!evaluationApi && <Button type="button" className="action-compare-add" disabled={state.variants.filter((variant) => !variant.baseline && !variant.readOnly).length >= context.limits.max_challengers}
          onClick={() => { const id = session.addVariant(); if (id) setEditingId(id); }}><Plus size={13} /> Variant</Button>
        }
      </CompareToolbar>

      {editing && renderVariantEditor && <AnchoredMenu key={editing.id} anchorRef={editingAnchor} onClose={() => setEditingId(null)} width={380} role="dialog" className="action-compare-editor" aria-label={`Configure variant ${editing.displayKey}`}>
        <header><strong><VariantBadge label={editing.displayKey} /> {editingLabel}</strong>
          <WindowCloseButton size="inline" label="Close variant editor" onClick={() => setEditingId(null)} /></header>
        <div className="action-compare-editor-body">
          <fieldset disabled={editing.baseline || editing.readOnly} style={{ border: 0, padding: 0, margin: 0 }}>
          {renderVariantEditor({
            variantId: editing.id, label: editing.displayKey, draft: editing.draft,
            lockedParams: context.locked_params, structureLocked: true, lockReason: 'Fixed for this comparison',
            onDraftChange: (draft) => session.updateVariant(editing.id, draft),
            onDuplicate: () => { const id = session.addVariant(editing.id); if (id) setEditingId(id); },
            onDismiss: () => setEditingId(null),
          })}
          </fieldset>
          <Button type="button" onClick={() => { const id = session.addVariant(editing.id); if (id) setEditingId(id); }}>
            <Copy size={13} /> Duplicate
          </Button>
        </div>
      </AnchoredMenu>}
    </div>

    {!evaluationApi && <div className="action-compare-visibility"><span>Columns</span>
      <OverflowRow items={visibilityFields} getKey={(field) => field.key} aria-label="Visible comparison fields"
        className="action-compare-visibility-row" overflowLabel="More comparison fields"
        triggerClassName="action-compare-visibility-overflow"
        renderItem={({ key, name }) => { const visible = !hidden.has(key); return <Button type="button" aria-pressed={visible}
          aria-label={name} onClick={() => session.setColumnVisible(key, !visible)}>
          {visible ? '✓' : '+'} {name}
        </Button>; }}
        renderOverflowItem={({ key, name }, closeMenu) => { const visible = !hidden.has(key); return <button type="button"
          role="menuitemcheckbox" aria-checked={visible} className="menu-item" onClick={() => {
            session.setColumnVisible(key, !visible); closeMenu();
          }}><span aria-hidden>{visible ? '✓' : '+'}</span><span className="menu-item-name">{name}</span></button>; }} />
      <span className="action-compare-sample-count" data-testid="action-compare-sample-count">{sampleLabel(
        context.sample_row_ids.length,
        state.capturedSelection?.totalCount ?? null,
      )}</span>
      {onChangeRowScope && <PanelSelect ariaLabel="Comparison rows" value={rowScope ?? 'sample'} disabled={running || rowScopePending}
        options={[{ value: 'sample', label: 'Sample · up to 20 rows' }, { value: 'all', label: 'All · up to 500 rows' }]}
        onValueChange={(value) => onChangeRowScope(value as 'sample' | 'all')} />}
    </div>}

    {evaluationApi ? <ComparisonEvaluationWorkspace key={initialLens ?? 'each_other'} api={evaluationApi} state={state} session={session}
      variantMenuHost={variantMenuHost} initialLens={initialLens} onJoinExpected={onJoinExpected} onExpectedAnswersChanged={onExpectedAnswersChanged}
      onShelved={onShelved} modelField={modelField} instructionsField={instructionsField}
      rowScope={rowScope} rowScopePending={rowScopePending} rowScopeTotal={rowScopeTotal} dataRevision={dataRevision} onChangeRowScope={onChangeRowScope}
      renderVariantEditor={renderVariantEditor} onOpenVariant={setEditingId} onUseSettings={onUseSettings}
      renderGrid={(evaluation) => <ActionCompareGrid projectId={projectId} state={state} rowHeight={rowHeight}
        wrapText={wrapText} frozenColumnCount={frozenColumnCount} labelForDraft={labelForDraft}
        onOpenBoundedValue={onOpenBoundedValue} onOpenStructuredValue={onOpenStructuredValue} evaluation={evaluation} />} />
      : <ActionCompareGrid projectId={projectId} state={state} rowHeight={rowHeight}
      wrapText={wrapText} frozenColumnCount={frozenColumnCount} labelForDraft={labelForDraft}
      onOpenBoundedValue={onOpenBoundedValue} onOpenStructuredValue={onOpenStructuredValue} />}
  </section>;
}
