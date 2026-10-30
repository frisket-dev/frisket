import { useEffect, useState, type ReactNode } from 'react';
import { Check, ChevronLeft, ChevronRight, Pencil, X } from 'lucide-react';
import type { ReviewBundleField } from '../../api/types';
import { Button } from '../../components/Button';
import { PanelSelect } from '../../components/PanelSelect';
import { ReviewValue } from '../../components/review/ReviewValue';
import styles from '../../components/review/ReviewWorkspace.module.css';

export type ComparisonReviewRule = 'exact' | 'ignore_case' | 'number' | 'date' | 'set' | 'hand';
export interface ComparisonReviewProps {
  fieldLabel: string;
  rowId: number;
  position: number;
  total: number;
  mode: 'inspect' | 'score' | 'disagreements';
  groups: Array<{ key: string; answerText: string; variantKeys: string[];
    verdict: 'match' | 'miss' | 'not_checked' | 'to_score' | 'failed' | 'not_run' | 'ruled_out';
    canSelect?: boolean; field?: ReviewBundleField; valueContent?: ReactNode; variantNotes?: string }>;
  expectedValue: string;
  expectedField?: ReviewBundleField;
  expectedContent?: ReactNode;
  expectedEditable?: boolean;
  rule: ComparisonReviewRule;
  busy: boolean;
  error?: string | null;
  source: ReactNode;
  onExpectedChange(value: string): void;
  onRuleChange(rule: ComparisonReviewRule): void;
  onAccept(groupKey: string): void;
  onReject(groupKey: string): void;
  onPrevious(): void;
  onNext(): void;
  onClose(): void;
}

const rules: Array<[ComparisonReviewRule, string]> = [
  ['exact', 'Exact'], ['ignore_case', 'Ignore case'], ['number', 'Same number'],
  ['date', 'Same date'], ['set', 'Same items'], ['hand', 'Score by hand'],
];
const verdictLabels: Record<ComparisonReviewProps['groups'][number]['verdict'], string> = {
  match: 'Match', miss: 'Miss', not_checked: 'Not checked', to_score: 'To score', failed: 'Failed', not_run: 'Not run',
  ruled_out: 'Ruled out',
};

/** A presentation-only evaluation queue; the caller owns source identities and writes. */
export function ComparisonReview(p: ComparisonReviewProps) {
  return <ComparisonReviewRow key={`${p.rowId}:${p.fieldLabel}`} {...p} />;
}

function ComparisonReviewRow(p: ComparisonReviewProps) {
  const [selected, setSelected] = useState(0);
  const [editing, setEditing] = useState(false);
  const selectedGroup = p.groups[Math.min(selected, p.groups.length - 1)];
  const selectable = (group: ComparisonReviewProps['groups'][number]) => group.canSelect !== false && group.verdict !== 'not_run';
  const canAccept = (group: ComparisonReviewProps['groups'][number]) => selectable(group) && !(p.mode === 'disagreements' && group.field?.boundedValue);
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.repeat || event.isComposing || event.ctrlKey || event.metaKey || event.altKey || p.busy) return;
      const target = event.target instanceof HTMLElement ? event.target : null;
      if (target?.closest('input, textarea, select, [contenteditable="true"], [role="menu"], [role="listbox"]')) return;
      if (document.querySelector(':popover-open, :modal')) return;
      switch (event.key.toLowerCase()) {
        case 'arrowdown': case 's': case 'j': setSelected((index) => Math.min(index + 1, p.groups.length - 1)); break;
        case 'arrowup': case 'w': case 'k': setSelected((index) => Math.max(index - 1, 0)); break;
        case 'arrowleft': case 'a': if (selectedGroup && canAccept(selectedGroup)) p.onAccept(selectedGroup.key); break;
        case 'arrowright': case 'd': case 'r': if (selectedGroup && selectable(selectedGroup)) p.onReject(selectedGroup.key); break;
        default: return;
      }
      event.preventDefault();
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  });
  const showEditor = p.mode === 'disagreements' || editing;
  return <>
    <nav className={styles.navigation} aria-label="Comparison review rows">
      <span>{p.total ? p.position : 0} / {p.total}</span>
      <button type="button" className="icon-btn" aria-label="Previous row" disabled={p.busy || p.position <= 1} onClick={p.onPrevious}><ChevronLeft size={15} /></button>
      <button type="button" className="icon-btn" aria-label="Next row" disabled={p.busy || p.position >= p.total} onClick={p.onNext}><ChevronRight size={15} /></button>
      <Button disabled={p.busy} onClick={p.onClose}>Close</Button>
    </nav>
    {p.error && <p className={styles.notice} role="alert">{p.error}</p>}
    <main className={styles.workspace} data-testid="comparison-review" aria-busy={p.busy}>
      {p.source}
      <section className={styles.results} aria-label="Comparison answers">
        <div className={styles.resultsHeader}>Results <span>{p.fieldLabel} · row {p.rowId}</span></div>
        <div className={`${styles.fields} comparison-answer-list`} role="region" aria-label="Distinct answers">
          {p.groups.map((group, index) => <section key={group.key} className={styles.field} data-selected={index === selected}>
            <button type="button" className={styles.fieldSelect} aria-label={`Answer ${index + 1}, variants ${group.variantKeys.join(", ")}, ${verdictLabels[group.verdict]}`} aria-pressed={index === selected} onClick={() => setSelected(index)}>
              <span className={styles.fieldHeading}><strong>Answer {index + 1}</strong>
                {group.variantKeys.map((key) => <small key={key}>{key}</small>)} <small>{verdictLabels[group.verdict]}</small></span>
              {group.variantNotes && <small>{group.variantNotes}</small>}
            </button>
            <div className={styles.verdict} role="group" aria-label={`Decision for answer ${index + 1}`}>
              <button type="button" className={styles.accept} aria-label={`Accept answer ${index + 1}`} title="Accept (← or A)"
                aria-pressed={group.verdict === 'match'} disabled={p.busy || !canAccept(group)} onClick={() => p.onAccept(group.key)}><Check size={16} /></button>
              <button type="button" className={styles.reject} aria-label={`Reject answer ${index + 1}`} title="Reject (→ or D)"
                aria-pressed={group.verdict === 'miss' || group.verdict === 'ruled_out'} disabled={p.busy || !selectable(group)} onClick={() => p.onReject(group.key)}><X size={16} /></button>
            </div>
            <div className={styles.value}>{group.valueContent ?? (group.field ? <ReviewValue field={group.field} /> : group.answerText || <em>empty answer</em>)}</div>
            {p.mode === 'disagreements' && group.field?.boundedValue && <p className={styles.editUnavailable}>Only an excerpt is shown; it cannot be selected as the expected answer.</p>}
          </section>)}
        </div>
          <section className={`${styles.field} comparison-expected-answer`} aria-label="Expected answer editor">
            <span className={styles.fieldHeading}><strong>Expected answer</strong></span>
            {p.mode !== 'disagreements' && <button type="button" className={styles.textButton} aria-label="Edit expected answer"
              disabled={p.busy || p.expectedEditable === false || Boolean(p.expectedField?.boundedValue)} onClick={() => setEditing(!editing)}><Pencil size={14} /></button>}
            {showEditor && p.expectedEditable !== false && !p.expectedField?.boundedValue ? <div className={styles.editor}>
              <textarea className="form-input form-textarea" aria-label="Expected answer" value={p.expectedValue}
                onChange={(event) => p.onExpectedChange(event.target.value)} />
            </div> : <div className={styles.value}>{p.expectedContent ?? (p.expectedField ? <ReviewValue field={p.expectedField} /> : p.expectedValue || <em>Not checked</em>)}</div>}
            {p.expectedField?.boundedValue && <p className={styles.editUnavailable}>Only an excerpt is shown; editing is unavailable.</p>}
            <label className={styles.value}>Match rule{' '}
              <PanelSelect ariaLabel="Match rule" value={p.rule} disabled={p.busy}
                onValueChange={(value) => p.onRuleChange(value as ComparisonReviewRule)}
                options={rules.map(([value, label]) => ({ value, label }))} />
            </label>
          </section>
        <footer className={`${styles.footer} comparison-review-footer`}><div className={styles.footerButtons}>
          <span>{p.position} of {p.total}</span><Button disabled={p.busy || p.position >= p.total} onClick={p.onNext}>Next row <ChevronRight size={14} /></Button>
        </div><p className={styles.shortcuts}><kbd>↑↓</kbd> / <kbd>W S</kbd> answer · <kbd>←</kbd>/<kbd>A</kbd> yes · <kbd>→</kbd>/<kbd>D</kbd> no</p></footer>
      </section>
    </main>
  </>;
}
