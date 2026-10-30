import { isResearchAction } from '../research/researchAction';
import { Button } from '../Button';
import { TextArea } from '../FormControls';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Check, ChevronRight } from 'lucide-react';
import type { CellValue, ReviewBundleField } from '../../api/types';
import { useEscapeDismiss } from '../../hooks/useEscapeDismiss';
import { handleAutoResizeTextareaInput, resizeTextareaToContent } from '../action-panel/formControlHelpers';
import { ReviewSourcePreview } from './ReviewSourcePreview';
import { ReviewDecisionControls, ReviewNavigation, ReviewWorkspace } from './ReviewPresentation';
import { useReviewShortcuts } from './useReviewShortcuts';
import { ReviewValue } from './ReviewValue';
import { ReviewExcerpt } from './ReviewExcerpt';
import { ReviewResearch } from './ReviewResearch';
import type { ReviewItemLocation, ReviewItemSelection } from './reviewItemLocations';
import { decisionLabel, isDecided } from './reviewDecisions';
import { isPlainTextReviewField, useReviewSession, type ReviewSessionController, type ReviewSessionOptions } from './useReviewSession';
import styles from './ReviewWorkspace.module.css';

export interface ReviewSessionProps extends ReviewSessionOptions {
  onClose?(): void;
  renderToolbar?(navigation: ReactNode, leave: (action: () => void) => void, busy: boolean): ReactNode;
  renderFrame?(content: ReactNode, leave: (action: () => void) => void, busy: boolean, editing: boolean): ReactNode;
}

function RowNavigation({ controller: c }: { controller: ReviewSessionController }) {
  return <ReviewNavigation label="Review rows" position={c.page?.total ? c.position : 0} total={c.page?.total ?? 0}
    busy={c.busy} canPrevious={c.canPrevious} canNext={c.canNext} onPrevious={() => c.moveRow(-1)} onNext={() => c.moveRow(1)} />;
}

function FieldDecision({ field, controller: c, onSelectItem, selectableItemIndices }: {
  field: ReviewBundleField; controller: ReviewSessionController;
  onSelectItem(index: number): void; selectableItemIndices: ReadonlySet<number>;
}) {
  const selected = c.field?.id === field.id;
  const editing = c.editingId === field.id;
  const sectionRef = useRef<HTMLElement>(null);
  useEffect(() => {
    if (selected) sectionRef.current?.scrollIntoView?.({ block: 'nearest' });
  }, [selected]);
  const editable = isPlainTextReviewField(field);
  const editUnavailable = Boolean(field.boundedValue) || field.canEdit === false;
  const editUnavailableMessage = field.boundedValue
    ? 'Only an excerpt is shown; editing is unavailable.'
    : 'This output was replaced or edited after the run. Editing it would not safely update the latest cell.';
  return <section ref={sectionRef} className={styles.field} data-selected={selected} data-review-state={field.reviewState}
    data-testid={`review-field-${field.columnName}`} data-review-changed={field.changed ? 'true' : 'false'} data-review-decision={field.reviewDecision}>
    <button type="button" className={styles.fieldSelect} aria-pressed={selected}
      aria-label={`Select ${field.columnName} review field`} disabled={c.busy} onClick={() => c.selectField(field.id)}>
      <span className={styles.fieldHeading}><strong>{field.columnName}</strong>
        {isDecided(field) && <small>{decisionLabel(field)}</small>}
      </span>
      {field.confidence !== null && <small className={styles.confidence}>Confidence {field.confidence.toFixed(2)}</small>}
    </button>
    <ReviewDecisionControls label={`Decision for ${field.columnName}`} acceptLabel={`Accept ${field.columnName}`}
      rejectLabel={`Reject ${field.columnName}`} acceptPressed={field.reviewDecision === 'accept'}
      rejectPressed={field.reviewState === 'rejected'} acceptDisabled={c.busy || c.readOnly} rejectDisabled={c.busy || c.readOnly}
      onAccept={() => c.toggle(field, 'accept')} onReject={() => c.toggle(field, 'reject')}
      edit={{ label: `Edit ${field.columnName}`, pressed: editing || field.reviewDecision === 'edit',
        disabled: c.busy || c.readOnly || !editable || editUnavailable,
        title: editUnavailable ? editUnavailableMessage : 'Edit (E) — counts as incorrect', onClick: () => c.startEdit(field) }} />
    <div className={styles.value} onClick={(event) => {
      const target = event.target;
      if (!c.busy && (!(target instanceof Element) || !target.closest('a, button, input, select, textarea, [role="button"]'))) c.selectField(field.id);
    }}>
      <ReviewValue field={field} onSelectItem={c.busy ? undefined : onSelectItem} selectableItemIndices={selectableItemIndices} />
    </div>
    {editUnavailable && <p className={styles.editUnavailable}>{editUnavailableMessage} {field.boundedValue
      ? 'Accept, reject, and notes are still available.' : 'Accept, reject, clear, and notes are still available.'}</p>}
    {editing && !editUnavailable && <div className={styles.editor}>
      <TextArea ref={(node) => { if (node && document.activeElement !== node) { node.focus(); resizeTextareaToContent(node); } }}
        className="form-textarea-autogrow" rows={2}
        aria-label={`Edit proposed ${field.columnName} value`} data-testid="review-edit-input"
        value={c.editValue} disabled={c.busy} onInput={handleAutoResizeTextareaInput}
        onChange={(event) => c.setEditValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') { event.stopPropagation(); c.cancelEdit(); }
          if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void c.resolve([field], 'edit', c.editValue); }
        }} />
      <Button disabled={c.busy} onClick={() => { void c.resolve([field], 'edit', c.editValue); }}>Save edit</Button>
      <Button disabled={c.busy} onClick={c.cancelEdit}>Cancel</Button>
    </div>}
    {selected && (field.boundedJustification
      ? <div className={styles.justification} aria-label="Reason excerpt"><ReviewExcerpt excerpt={field.boundedJustification} /></div>
      : field.justification && <p className={styles.justification}>{field.justification}</p>)}
    {selected && c.bundle && isResearchAction(c.bundle.actionKind) && field.outputKey !== 'sources' &&
      <ReviewResearch key={`${field.runId}:${field.rowId}:${field.columnId}`} bundle={c.bundle} field={field} />}
  </section>;
}

function ReviewFooter({ controller: c }: { controller: ReviewSessionController }) {
  const fields = c.bundle?.fields ?? [];
  const count = fields.filter(isDecided).length;
  const remaining = fields.length - count;
  return <footer className={styles.footer}>
    <div className={styles.footerLine}>
      <span>{count} of {fields.length} decided</span>
      <button className={styles.textButton} disabled={c.busy || c.readOnly || !count} onClick={c.reset}>Reset</button>
    </div>
    <TextArea className={`form-input form-textarea ${styles.note}`} rows={2}
      placeholder="Note for this row (optional)" aria-label="Review note for this row" data-testid="review-note-input"
      value={c.note} disabled={c.readOnly || c.busy || c.savingNote} onChange={(event) => c.changeNote(event.target.value)}
      onBlur={() => { void c.saveNote(); }} />
    <div className={styles.footerButtons}>
      {remaining ? <Button className="btn-primary" disabled={c.busy || c.readOnly} onClick={c.acceptRemaining} data-testid="review-accept-remaining">
        <Check size={14} /> {count ? 'Accept remaining' : 'Accept all'}
      </Button> : <span className={styles.rowDone}><Check size={14} /> Row done</span>}
      <Button disabled={c.busy || !c.canNext} onClick={() => c.moveRow(1)}>Next row <ChevronRight size={14} /></Button>
    </div>
    <p className={styles.shortcuts}><kbd>↑↓</kbd> / <kbd>W S</kbd> field · <kbd>←</kbd>/<kbd>A</kbd> yes · <kbd>→</kbd>/<kbd>D</kbd> no · <kbd>Shift A</kbd> accept + next</p>
  </footer>;
}

export function ReviewSession({ onClose, renderToolbar, renderFrame, ...options }: ReviewSessionProps) {
  const c = useReviewSession(options);
  const [selectedItem, setSelectedItem] = useState<ReviewItemSelection | null>(null);
  const [itemLocations, setItemLocations] = useState<ReviewItemLocation[]>([]);
  useEscapeDismiss(() => { if (onClose && !c.editingId) c.leave(onClose); }, { enabled: !!onClose && !renderFrame, typingGuard: true });
  useReviewShortcuts({ disabled: c.busy || Boolean(c.editingId), onMove: (delta) => c.moveField(delta),
    onAccept: () => { if (c.field) c.toggle(c.field, 'accept'); },
    onReject: () => { if (c.field) c.toggle(c.field, 'reject'); },
    onEdit: () => c.startEdit(), onAcceptRemaining: () => c.acceptRemaining() });

  const navigation = <RowNavigation controller={c} />;
  const content = <>
    {renderToolbar ? renderToolbar(navigation, c.leave, c.busy && !c.loading) : navigation}
    {c.problem && <div className={styles.notice} role="alert">{c.problem}
      {c.loading === false && c.canRetryPageLoad && <Button onClick={c.retry}>Retry</Button>}
      {c.note && <Button onClick={() => { void c.saveNote(); }}>Save note</Button>}
    </div>}
    {c.readOnly && <p className={styles.notice}>Review complete. Reopen it to make more decisions.</p>}
    {c.loading && !c.page ? <div className={styles.empty}>Loading review…</div>
      : !c.bundle ? <div className={styles.empty} data-testid="review-empty">No results to review.</div>
        : <ReviewWorkspace testId="review-card" label="Result fields" busy={c.busy}
          source={<ReviewSourcePreview bundle={c.bundle} activeField={c.field} selectedItem={selectedItem} onItemLocations={setItemLocations}
            sourceEntries={Object.entries(c.bundle.source) as Array<[string, CellValue]>} />}>
            <div className={styles.resultsHeader}>Results <span>{c.bundle.fields.length} {c.bundle.fields.length === 1 ? 'field' : 'fields'}</span></div>
            <div className={styles.fields} data-testid="review-bundle-fields">
              {c.bundle.fields.map((field) => <FieldDecision key={field.id} field={field} controller={c}
                selectableItemIndices={new Set(itemLocations.filter((location) => location.fieldId === field.id).map((location) => location.index))}
                onSelectItem={(index) => { c.selectField(field.id); setSelectedItem({ fieldId: field.id, index }); }} />)}
            </div>
            <ReviewFooter controller={c} />
        </ReviewWorkspace>}
  </>;
  return renderFrame ? renderFrame(content, c.leave, c.busy && !c.loading, Boolean(c.editingId)) : content;
}
