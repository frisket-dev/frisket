import '../PanelPrimitives.css';
import './ImportWorkspaceDialog.css';
import { Button } from '../Button';
import type { BulkImportPlan, BulkImportFailedOutput } from '../../api/open';
import { bulkPlanValidationError } from './fileIntake';

export function BulkImportPlanPanel({
  plan,
  decisions,
  busy,
  error,
  successMessage,
  warnings,
  failures,
  failuresOmitted,
  onDecisionChange,
  onExecute,
}: {
  plan: BulkImportPlan | null;
  decisions: Record<string, string>;
  busy: boolean;
  error: string | null;
  successMessage: string | null;
  warnings: string[];
  failures: BulkImportFailedOutput[];
  failuresOmitted: number;
  onDecisionChange(questionId: string, value: string): void;
  onExecute(): void;
}) {
  const validationError = plan ? bulkPlanValidationError(plan) : null;
  if (!plan) {
    return (
      <>
        {busy ? <div role="status">Preparing import plan…</div> : null}
        {error ? <div className="panel-error" role="alert">{error}</div> : null}
        {successMessage ? <div role="status">{successMessage}</div> : null}
        {(failures.length || failuresOmitted > 0) ? (
          <section aria-label="Import failures" data-testid="bulk-import-failures">
            {failures.length ? (
              <ul>
                {failures.map((failure, index) => (
                  <li key={`${failure.logical_paths.join('\u0000')}-${index}`}>
                    <span>{failure.logical_paths.join(', ') || 'Unknown file'}</span>: <span>{failure.error}</span>
                    {failure.logical_paths_omitted > 0 ? (
                      <div>
                        {failure.logical_paths_omitted} additional path{failure.logical_paths_omitted === 1 ? '' : 's'} were omitted.
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            ) : null}
            {failuresOmitted > 0 ? (
              <div>{failuresOmitted} additional failure{failuresOmitted === 1 ? '' : 's'} were omitted.</div>
            ) : null}
          </section>
        ) : null}
        {warnings.length ? (
          <section aria-label="Import warnings" data-testid="bulk-import-warnings">
            <h3>Import warnings</h3>
            <ul>
              {warnings.map((warning, index) => <li key={index}>{warning}</li>)}
            </ul>
          </section>
        ) : null}
      </>
    );
  }
  if (validationError) {
    return <div className="panel-error" role="alert">{validationError}</div>;
  }
  if (!plan.proposed_outputs.length) {
    return (
      <div role="status">
        {plan.message || 'No importable files found.'}
      </div>
    );
  }
  return (
    <section aria-label="Bulk import plan">
      {busy ? <div role="status">Executing import…</div> : null}
      {error ? <div className="panel-error" role="alert">{error}</div> : null}
      {plan.message ? <div role="status">{plan.message}</div> : null}
      <h3>Proposed outputs</h3>
      <ul aria-label="Proposed outputs">
        {plan.proposed_outputs.map((output) => (
          <li key={output.id}>
            <strong>{output.sheet_name}</strong> ({output.kind})
            <ul>
              {output.logical_paths.map((path) => <li key={path}>{path}</li>)}
            </ul>
          </li>
        ))}
      </ul>
      {plan.questions.map((question, index) => {
        const legendId = `bulk-import-question-${index}-legend`;
        return (
          <fieldset key={question.id} disabled={busy} aria-labelledby={legendId}>
            <legend id={legendId}>Matching CSV files {index + 1}</legend>
            <ul aria-label={`Files for matching CSV files ${index + 1}`}>
              {question.logical_paths.map((path) => <li key={path}>{path}</li>)}
            </ul>
            <label>
              <input
                type="radio"
                name={`bulk-import-${question.id}`}
                value="combine"
                checked={decisions[question.id] === 'combine'}
                onChange={() => onDecisionChange(question.id, 'combine')}
              />{' '}
              Combine into one sheet
            </label>
            <label>
              <input
                type="radio"
                name={`bulk-import-${question.id}`}
                value="separate"
                checked={decisions[question.id] === 'separate'}
                onChange={() => onDecisionChange(question.id, 'separate')}
              />{' '}
              Import as separate sheets
            </label>
          </fieldset>
        );
      })}
      <Button
        type="button"
        className="btn-primary import-primary"
        disabled={busy}
        onClick={onExecute}
      >
        {busy ? 'Importing…' : 'Execute import'}
      </Button>
    </section>
  );
}
