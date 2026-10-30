import { useState, type ReactNode } from 'react';
import { ChevronRight, Pencil } from 'lucide-react';
import type { ReviewBundleField } from '../../api/types';
import { Button } from '../../components/Button';
import { FormField, TextArea } from '../../components/FormControls';
import { PanelSelect } from '../../components/PanelSelect';
import { ReviewValue } from '../../components/review/ReviewValue';
import { ReviewDecisionControls, ReviewHeader, ReviewNavigation, ReviewShell, ReviewWorkspace } from '../../components/review/ReviewPresentation';
import { useReviewShortcuts } from '../../components/review/useReviewShortcuts';
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
  toolbar?: ReactNode;
  emptyMessage?: ReactNode;
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
  return <ReviewShell busy={p.busy} onClose={p.onClose}>
    <ReviewHeader busy={p.busy} onClose={p.onClose} closeLabel="Close comparison review">
      <section className={styles.comparisonToolbar}>{p.toolbar}
        <ReviewNavigation label="Comparison review rows" position={p.position} total={p.total} busy={p.busy}
          canPrevious={p.position > 1} canNext={p.position < p.total} onPrevious={p.onPrevious} onNext={p.onNext} />
      </section>
    </ReviewHeader>
    {p.error && <p className={styles.notice} role="alert">{p.error}</p>}
    <ComparisonReviewRow key={`${p.rowId}:${p.fieldLabel}`} {...p} />
  </ReviewShell>;
}

function ComparisonReviewRow(p: ComparisonReviewProps) {
  const [selected, setSelected] = useState(0);
  const [editing, setEditing] = useState(false);
  const selectedGroup = p.groups[Math.min(selected, p.groups.length - 1)];
  const selectable = (group: ComparisonReviewProps['groups'][number]) => group.canSelect !== false && group.verdict !== 'not_run';
  const canAccept = (group: ComparisonReviewProps['groups'][number]) => selectable(group) && !(p.mode === 'disagreements' && group.field?.boundedValue);
  useReviewShortcuts({ disabled: p.busy,
    onMove: (delta) => setSelected((index) => Math.max(0, Math.min(index + delta, p.groups.length - 1))),
    onAccept: () => { if (selectedGroup && canAccept(selectedGroup)) p.onAccept(selectedGroup.key); },
    onReject: () => { if (selectedGroup && selectable(selectedGroup)) p.onReject(selectedGroup.key); },
  });
  const showEditor = p.mode === 'disagreements' || editing;
  return <>
    {p.emptyMessage ? <div className={styles.empty}>{p.emptyMessage}</div> : <ReviewWorkspace testId="comparison-review" label="Comparison answers" busy={p.busy} source={p.source}>
        <div className={styles.resultsHeader}>Results <span>{p.fieldLabel} · row {p.rowId}</span></div>
        <div className={`${styles.fields} ${styles.answerList}`} role="region" aria-label="Distinct answers">
          {p.groups.map((group, index) => <section key={group.key} className={styles.field} data-selected={index === selected}>
            <button type="button" className={styles.fieldSelect} aria-label={`Answer ${index + 1}, variants ${group.variantKeys.join(", ")}, ${verdictLabels[group.verdict]}`} aria-pressed={index === selected} onClick={() => setSelected(index)}>
              <span className={styles.fieldHeading}><strong>Answer {index + 1}</strong>
                {group.variantKeys.map((key) => <small key={key}>{key}</small>)} <small>{verdictLabels[group.verdict]}</small></span>
              {group.variantNotes && <small>{group.variantNotes}</small>}
            </button>
            <ReviewDecisionControls label={`Decision for answer ${index + 1}`} acceptLabel={`Accept answer ${index + 1}`}
              rejectLabel={`Reject answer ${index + 1}`} acceptPressed={group.verdict === 'match'}
              rejectPressed={group.verdict === 'miss' || group.verdict === 'ruled_out'}
              acceptDisabled={p.busy || !canAccept(group)} rejectDisabled={p.busy || !selectable(group)}
              onAccept={() => p.onAccept(group.key)} onReject={() => p.onReject(group.key)} />
            <div className={styles.value}>{group.valueContent ?? (group.field ? <ReviewValue field={group.field} /> : group.answerText || <em>empty answer</em>)}</div>
            {p.mode === 'disagreements' && group.field?.boundedValue && <p className={styles.editUnavailable}>Only an excerpt is shown; it cannot be selected as the expected answer.</p>}
          </section>)}
        </div>
          <section className={`${styles.field} ${styles.expectedAnswer}`} aria-label="Expected answer editor">
            <span className={styles.fieldHeading}><strong>Expected answer</strong></span>
            {p.mode !== 'disagreements' && <button type="button" className={styles.textButton} aria-label="Edit expected answer"
              disabled={p.busy || p.expectedEditable === false || Boolean(p.expectedField?.boundedValue)} onClick={() => setEditing(!editing)}><Pencil size={14} /></button>}
            {showEditor && p.expectedEditable !== false && !p.expectedField?.boundedValue ? <div className={styles.editor}>
              <TextArea rows={2} aria-label="Expected answer" value={p.expectedValue}
                onChange={(event) => p.onExpectedChange(event.target.value)} />
            </div> : <div className={styles.value}>{p.expectedContent ?? (p.expectedField ? <ReviewValue field={p.expectedField} /> : p.expectedValue || <em>Not checked</em>)}</div>}
            {p.expectedField?.boundedValue && <p className={styles.editUnavailable}>Only an excerpt is shown; editing is unavailable.</p>}
            <FormField className={styles.value} label="Match rule">
              <PanelSelect ariaLabel="Match rule" value={p.rule} disabled={p.busy}
                onValueChange={(value) => p.onRuleChange(value as ComparisonReviewRule)}
                options={rules.map(([value, label]) => ({ value, label }))} />
            </FormField>
          </section>
        <footer className={styles.footer}><div className={styles.footerButtons}>
          <span>{p.position} of {p.total}</span><Button disabled={p.busy || p.position >= p.total} onClick={p.onNext}>Next row <ChevronRight size={14} /></Button>
        </div><p className={styles.shortcuts}><kbd>↑↓</kbd> / <kbd>W S</kbd> answer · <kbd>←</kbd>/<kbd>A</kbd> yes · <kbd>→</kbd>/<kbd>D</kbd> no</p></footer>
    </ReviewWorkspace>}
  </>;
}
