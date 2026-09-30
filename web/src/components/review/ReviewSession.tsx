import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Check, ChevronLeft, ChevronRight, Pencil, X } from 'lucide-react';
import type { CellValue, ReviewBundleField } from '../../api/types';
import { useEscapeDismiss } from '../../hooks/useEscapeDismiss';
import { handleAutoResizeTextareaInput, resizeTextareaToContent } from '../action-panel/formControlHelpers';
import { ReviewSourcePreview } from './ReviewSourcePreview';
import { ReviewValue } from './ReviewValue';
import type { ReviewItemLocation, ReviewItemSelection } from './reviewItemLocations';
import { decisionLabel, isDecided } from './reviewDecisions';
import { isPlainTextReviewField, useReviewSession, type ReviewSessionController, type ReviewSessionOptions } from './useReviewSession';
import styles from './ReviewWorkspace.module.css';

export interface ReviewSessionProps extends ReviewSessionOptions {
  onClose?(): void;
  renderToolbar?(navigation: ReactNode, leave: (action: () => void) => void, busy: boolean): ReactNode;
}

function RowNavigation({ controller: c }: { controller: ReviewSessionController }) {
  return <nav className={styles.navigation} aria-label="Review rows">
    <span data-testid="review-page-status">{c.page?.total ? c.page.offset + c.cursor + 1 : 0} / {c.page?.total ?? 0}</span>
    <button className="icon-btn" aria-label="Previous row" disabled={c.busy || !c.canPrevious} onClick={() => c.moveRow(-1)}><ChevronLeft size={15} /></button>
    <button className="icon-btn" aria-label="Next row" disabled={c.busy || !c.canNext} onClick={() => c.moveRow(1)}><ChevronRight size={15} /></button>
  </nav>;
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
  return <section ref={sectionRef} className={styles.field} data-selected={selected} data-review-state={field.reviewState}
    data-testid={`review-field-${field.columnName}`} data-review-changed={field.changed ? 'true' : 'false'} data-review-decision={field.reviewDecision}>
    <button type="button" className={styles.fieldSelect} aria-pressed={selected}
      aria-label={`Select ${field.columnName} review field`} disabled={c.busy} onClick={() => c.selectField(field.id)}>
      <span className={styles.fieldHeading}><strong>{field.columnName}</strong>
        {isDecided(field) && <small>{decisionLabel(field)}</small>}
      </span>
      {field.confidence !== null && <small className={styles.confidence}>Confidence {field.confidence.toFixed(2)}</small>}
    </button>
    <div className={styles.verdict} role="group" aria-label={`Decision for ${field.columnName}`}>
      <button type="button" aria-label={`Accept ${field.columnName}`} title="Accept (← or A)"
        aria-pressed={field.reviewDecision === 'accept'} disabled={c.busy || c.readOnly}
        className={styles.accept} onClick={() => c.toggle(field, 'accept')}><Check size={16} /></button>
      <button type="button" aria-label={`Reject ${field.columnName}`} title="Reject (→ or D)"
        aria-pressed={field.reviewState === 'rejected'} disabled={c.busy || c.readOnly}
        className={styles.reject} onClick={() => c.toggle(field, 'reject')}><X size={16} /></button>
      <button type="button" aria-label={`Edit ${field.columnName}`} title="Edit (E) — counts as incorrect"
        aria-pressed={editing || field.reviewDecision === 'edit'} disabled={c.busy || c.readOnly || !editable}
        className={styles.edit} onClick={() => c.startEdit(field)}><Pencil size={14} /></button>
    </div>
    <div className={styles.value} onClick={(event) => {
      const target = event.target;
      if (!c.busy && (!(target instanceof Element) || !target.closest('a, button, input, select, textarea, [role="button"]'))) c.selectField(field.id);
    }}>
      <ReviewValue field={field} onSelectItem={c.busy ? undefined : onSelectItem} selectableItemIndices={selectableItemIndices} />
    </div>
    {editing && <div className={styles.editor}>
      <textarea ref={(node) => { if (node && document.activeElement !== node) { node.focus(); resizeTextareaToContent(node); } }}
        className="form-input form-textarea form-textarea-autogrow" rows={2}
        aria-label={`Edit proposed ${field.columnName} value`} data-testid="review-edit-input"
        value={c.editValue} disabled={c.busy} onInput={handleAutoResizeTextareaInput}
        onChange={(event) => c.setEditValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Escape') { event.stopPropagation(); c.cancelEdit(); }
          if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void c.resolve([field], 'edit', c.editValue); }
        }} />
      <button className="btn" disabled={c.busy} onClick={() => { void c.resolve([field], 'edit', c.editValue); }}>Save edit</button>
      <button className="btn" disabled={c.busy} onClick={c.cancelEdit}>Cancel</button>
    </div>}
    {selected && field.justification && <p className={styles.justification}>{field.justification}</p>}
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
    <textarea className={`form-input form-textarea ${styles.note}`} rows={2}
      placeholder="Note for this row (optional)" aria-label="Review note for this row" data-testid="review-note-input"
      value={c.note} disabled={c.readOnly || c.busy || c.savingNote} onChange={(event) => c.changeNote(event.target.value)}
      onBlur={() => { void c.saveNote(); }} />
    <div className={styles.footerButtons}>
      {remaining ? <button className="btn btn-primary" disabled={c.busy || c.readOnly} onClick={c.acceptRemaining} data-testid="review-accept-remaining">
        <Check size={14} /> {count ? 'Accept remaining' : 'Accept all'}
      </button> : <span className={styles.rowDone}><Check size={14} /> Row done</span>}
      <button className="btn" disabled={c.busy || !c.canNext} onClick={() => c.moveRow(1)}>Next row <ChevronRight size={14} /></button>
    </div>
    <p className={styles.shortcuts}><kbd>↑↓</kbd> / <kbd>W S</kbd> field · <kbd>←</kbd>/<kbd>A</kbd> yes · <kbd>→</kbd>/<kbd>D</kbd> no · <kbd>Shift A</kbd> accept + next</p>
  </footer>;
}

export function ReviewSession({ onClose, renderToolbar, ...options }: ReviewSessionProps) {
  const c = useReviewSession(options);
  const [selectedItem, setSelectedItem] = useState<ReviewItemSelection | null>(null);
  const [itemLocations, setItemLocations] = useState<ReviewItemLocation[]>([]);
  useEscapeDismiss(() => { if (onClose && !c.editingId) c.leave(onClose); }, { enabled: !!onClose, typingGuard: true });
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || event.isComposing || event.ctrlKey || event.metaKey || event.altKey || c.busy || c.editingId) return;
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target?.closest('input, textarea, select, [contenteditable="true"], [role="menu"], [role="listbox"]')) return;
      if (document.querySelector(':popover-open, :modal')) return;
      switch (event.key.toLowerCase()) {
        case 'arrowdown': case 's': case 'j': c.moveField(1); break;
        case 'arrowup': case 'w': case 'k': c.moveField(-1); break;
        case 'arrowleft': if (c.field) c.toggle(c.field, 'accept'); break;
        case 'arrowright': case 'd': case 'r': if (c.field) c.toggle(c.field, 'reject'); break;
        case 'a': if (event.shiftKey) c.acceptRemaining(); else if (c.field) c.toggle(c.field, 'accept'); break;
        case 'e': c.startEdit(); break;
        default: return;
      }
      event.preventDefault();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, [c]);

  const navigation = <RowNavigation controller={c} />;
  return <>
    {renderToolbar ? renderToolbar(navigation, c.leave, c.busy && !c.loading) : navigation}
    {c.problem && <div className={styles.notice} role="alert">{c.problem}
      {c.loading === false && !c.page && <button className="btn" onClick={c.retry}>Retry</button>}
      {c.note && <button className="btn" onClick={() => { void c.saveNote(); }}>Save note</button>}
    </div>}
    {c.readOnly && <p className={styles.notice}>Review complete. Reopen it to make more decisions.</p>}
    {c.loading && !c.page ? <div className={styles.empty}>Loading review…</div>
      : !c.bundle ? <div className={styles.empty} data-testid="review-empty">No results to review.</div>
        : <main className={styles.workspace} data-testid="review-card" aria-busy={c.busy}>
          <ReviewSourcePreview bundle={c.bundle} activeField={c.field} selectedItem={selectedItem} onItemLocations={setItemLocations}
            sourceEntries={Object.entries(c.bundle.source) as Array<[string, CellValue]>} />
          <section className={styles.results} data-testid="review-output-panel" aria-label="Result fields">
            <div className={styles.resultsHeader}>Results <span>{c.bundle.fields.length} {c.bundle.fields.length === 1 ? 'field' : 'fields'}</span></div>
            <div className={styles.fields} data-testid="review-bundle-fields">
              {c.bundle.fields.map((field) => <FieldDecision key={field.id} field={field} controller={c}
                selectableItemIndices={new Set(itemLocations.filter((location) => location.fieldId === field.id).map((location) => location.index))}
                onSelectItem={(index) => { c.selectField(field.id); setSelectedItem({ fieldId: field.id, index }); }} />)}
            </div>
            <ReviewFooter controller={c} />
          </section>
        </main>}
  </>;
}
