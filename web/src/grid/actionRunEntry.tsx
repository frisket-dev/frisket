import './actionRunEntry.css';
import { GitCompareArrows, Sparkles } from 'lucide-react';
import type { CSSProperties, ReactNode } from 'react';
import { Button } from '../components/Button';
import { countLabel, formatRelativeTime } from '../format';
import type { ActionComparisonKind } from './actionComparisonCatalog';

/** Stable launch identity. A visible column is only an anchor into the run's
 * complete output family; consumers must not infer the family from neighbors. */
export interface ActionRunAnchor {
  sheetId: string;
  runId: string;
  columnId: string;
}

/** Display-safe facts returned by the workspace's lazy comparison-context
 * reader. Values are labels, never raw params, runtime envelopes, or secrets. */
export interface ActionRunEntryDetails {
  actionTitle: string;
  comparisonKind: ActionComparisonKind | null;
  sourceLabels: string[];
  modelLabel?: string | null;
  routeLabel?: string | null;
  outputLabels: string[];
  rowCount?: number | null;
  ranAt?: string | null;
  errorCount?: number | null;
  outcomeLabel?: string | null;
  expected?: { sheetName: string; rowCount: number } | null;
}

export interface ActionColumnMenuEntry {
  anchor: ActionRunAnchor;
  actionTitle: string;
  comparisonKind: ActionComparisonKind;
}

export interface ActionRunEntryCallbacks {
  onCompareActionRun?(anchor: ActionRunAnchor): void;
  onCheckExpected?(anchor: ActionRunAnchor): void;
}

function safeLabels(labels: readonly string[]): string[] {
  return labels.map((label) => label.trim()).filter(Boolean);
}

function runSummary(details: ActionRunEntryDetails): string | null {
  const facts: string[] = [];
  if (details.rowCount != null) facts.push(countLabel(details.rowCount, 'row'));
  const relative = formatRelativeTime(details.ranAt);
  if (relative) facts.push(relative);
  if (details.errorCount != null && details.errorCount > 0) {
    facts.push(countLabel(details.errorCount, 'error'));
  }
  if (details.outcomeLabel?.trim()) facts.push(details.outcomeLabel.trim());
  return facts.length > 0 ? facts.join(' · ') : null;
}

function MetadataRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="action-run-entry-row">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

export function ActionRunEntryCard({
  anchor,
  fallbackTitle,
  details,
  status,
  selectedRowCount,
  layoutControls,
  onCompareActionRun,
  onCheckExpected,
  onPointerEnter,
  onPointerLeave,
  style,
}: {
  anchor: ActionRunAnchor;
  fallbackTitle: string;
  details: ActionRunEntryDetails | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  selectedRowCount: number;
  layoutControls?: ReactNode;
  onPointerEnter?(): void;
  onPointerLeave?(): void;
  style?: CSSProperties;
} & ActionRunEntryCallbacks) {
  const title = details?.actionTitle.trim() || fallbackTitle;
  const sources = safeLabels(details?.sourceLabels ?? []);
  const outputs = safeLabels(details?.outputLabels ?? []);
  const model = safeLabels([details?.modelLabel ?? '', details?.routeLabel ?? '']).join(' · ');
  const ran = details ? runSummary(details) : null;
  const eligible = details?.comparisonKind === 'row_overlay';

  return (
    <section
      className="action-run-entry-card"
      data-testid="action-run-entry-card"
      aria-label={`${title} run details`}
      onPointerEnter={onPointerEnter}
      onPointerLeave={onPointerLeave}
      style={style}
    >
      <header className="action-run-entry-header">
        <Sparkles size={15} aria-hidden />
        <strong>{title}</strong>
      </header>

      {status === 'loading' && (
        <p className="action-run-entry-message" role="status">Loading run details…</p>
      )}
      {status === 'error' && (
        <p className="action-run-entry-message" role="status">Run details are unavailable.</p>
      )}
      {status === 'ready' && !details && (
        <p className="action-run-entry-message">This run can’t be reopened for comparison.</p>
      )}
      {details && (
        <>
          <dl className="action-run-entry-metadata">
            {sources.length > 0 && <MetadataRow label="Source">{sources.join(', ')}</MetadataRow>}
            {model && <MetadataRow label="Model">{model}</MetadataRow>}
            {outputs.length > 0 && <MetadataRow label="Creates">{outputs.join(', ')}</MetadataRow>}
            {ran && <MetadataRow label="Ran">{ran}</MetadataRow>}
            {details.expected && <MetadataRow label="Expected">⛓ {details.expected.sheetName} · {countLabel(details.expected.rowCount, 'row')}</MetadataRow>}
          </dl>
          <div className="action-run-entry-actions">
            {eligible && onCompareActionRun && (
              <Button
                type="button"
                className="btn-primary action-run-entry-primary"
                data-testid="action-run-entry-compare"
                onClick={() => onCompareActionRun(anchor)}
              >
                <GitCompareArrows size={14} aria-hidden />
                {selectedRowCount > 0
                  ? `Compare on ${countLabel(selectedRowCount, 'row')}…`
                  : 'Compare…'}
              </Button>
            )}
            {eligible && details.expected && onCheckExpected && <Button type="button" onClick={() => onCheckExpected(anchor)}>✓ Check against expected</Button>}
          </div>
        </>
      )}
      {layoutControls}
    </section>
  );
}
