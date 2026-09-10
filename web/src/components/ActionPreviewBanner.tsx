import type { PreviewGridView } from '../state/previewViewStore';
import { formatUsd } from '../format';

export function ActionPreviewBanner({ view, onRun, onClose }: {
  view: PreviewGridView; onRun(): void; onClose(): void;
}) {
  return <div className="preview-view-banner" data-testid="preview-tab-banner" data-status={view.status}>
    <span className="preview-view-dot" aria-hidden />
    <span className="preview-view-title">{view.actionName} preview</span>
    {view.status === 'running' ? (
      <span className="preview-view-scope" data-testid="preview-tab-stats">
        Previewing… {view.progress.done.toLocaleString()}
        {view.progress.total !== null && <>/{view.progress.total.toLocaleString()}</>}
      </span>
    ) : view.status === 'error' ? (
      <span className="preview-view-scope preview-view-error" data-testid="preview-tab-stats" role="alert">
        {view.error ?? 'Preview failed.'}
      </span>
    ) : view.status === 'cancelled' ? (
      <span className="preview-view-scope" data-testid="preview-tab-stats">Preview cancelled</span>
    ) : (
      <span className="preview-view-scope" data-testid="preview-tab-stats">
        Preview · {view.rowCount.toLocaleString()}
        {view.totalRows !== null && <> of {view.totalRows.toLocaleString()}</>} rows
      </span>
    )}
    {view.accounting && <span className="preview-view-scope" data-testid="preview-accounting">
      Model cost: {view.accounting.cost_actual === null ? 'unknown' : formatUsd(view.accounting.cost_actual)}
      {' · '}{(view.accounting.elapsed_ms / 1000).toFixed(1)}s
    </span>}
    <div className="preview-view-spacer" />
    {view.status === 'done' && <button type="button" className="preview-view-commit"
      data-testid="preview-run-for-real-button" title="Run this action for real and keep the results"
      onClick={onRun}>Run for real</button>}
    <button type="button" className="preview-view-discard" data-testid="preview-close-button"
      title="Close the preview (sampled values are not saved)" onClick={onClose}>Close</button>
  </div>;
}
