import { Activity, AlertTriangle, ListChecks } from 'lucide-react';
import type { SourceHealth } from '../../api/open';
import { PanelEmpty, StatusChip } from '../PanelPrimitives';
import { SourceStatus } from './sourcePresentation';
import { formatWhen, formatDuration, runDeltaText } from './sourceFormatting';
import '../SourcesPanel.css';

export function SourceHealthSummary({ health, sourceId }: { health: SourceHealth; sourceId: number }) {
  return <>
<div className="source-health-summary" data-testid={`source-health-summary-${sourceId}`}>
            <div>
              <span className="panel-item-meta">Status</span>
              <SourceStatus value={health.summary.status} testId={`source-health-status-${sourceId}`} />
            </div>
            <div>
              <span className="panel-item-meta">Last success</span>
              <strong className="panel-item-description" data-testid={`source-health-last-success-${sourceId}`}>
                {formatWhen(health.summary.lastSuccessAt)}
              </strong>
            </div>
            <div>
              <span className="panel-item-meta">Last failure</span>
              <strong className="panel-item-description" data-testid={`source-health-last-failure-${sourceId}`}>
                {formatWhen(health.summary.lastFailureAt)}
              </strong>
            </div>
            <div>
              <span className="panel-item-meta">Failures</span>
              <strong className="panel-item-description" data-testid={`source-health-consecutive-failures-${sourceId}`}>
                {health.summary.consecutiveFailures}
              </strong>
            </div>
          </div>

          <div className="control-row" data-testid={`source-health-deltas-${sourceId}`}>
            <StatusChip tone="neutral" size="sm">+{health.summary.newRowsRecent} new</StatusChip>
            <StatusChip tone="neutral" size="sm">{health.summary.changedRowsRecent} changed</StatusChip>
            <StatusChip tone="neutral" size="sm">{health.summary.skippedRowsRecent} skipped</StatusChip>
            <StatusChip tone="neutral" size="sm">{health.summary.revisionsRecent} revised</StatusChip>
          </div>
  </>;
}

export function SourceHealthRuns({ health, sourceId }: { health: SourceHealth; sourceId: number }) {
  return <>
<div className="content-stack">
            <h4 className="control-row panel-item-title"><ListChecks size={12} aria-hidden /> Recent runs</h4>
            <ul className="panel-list" data-testid={`source-health-runs-${sourceId}`}>
              {health.runs.length === 0 ? (
                <li className="panel-empty">No runs yet.</li>
              ) : health.runs.map((run) => (
                <li key={run.id} className="panel-card">
                  <div className="control-row panel-item-description">
                    <SourceStatus value={run.status} />
                    <span>{runDeltaText(run)}</span>
                  </div>
                  <div className="control-row panel-item-meta">
                    <span>{formatWhen(run.startedAt)}</span>
                    {formatDuration(run.durationMs) && <span>{formatDuration(run.durationMs)}</span>}
                    {run.warningCount > 0 && <span>{run.warningCount} warnings</span>}
                  </div>
                  {run.errorSummary && <div className="panel-item-meta source-run-error">{run.errorSummary}</div>}
                </li>
              ))}
            </ul>
          </div>

          <div className="content-stack">
            <h4 className="control-row panel-item-title"><Activity size={12} aria-hidden /> Downstream jobs</h4>
            {health.downstreamJobs.length === 0 ? (
              <PanelEmpty testId={`source-health-jobs-empty-${sourceId}`}>
                No downstream jobs recorded for loaded runs.
              </PanelEmpty>
            ) : (
              <ul className="panel-list" data-testid={`source-health-jobs-${sourceId}`}>
                {health.downstreamJobs.map((job) => (
                  <li key={job.jobId} className="panel-card panel-item-description" data-layout="row">
                    <span>{job.kind}</span>
                    <SourceStatus value={job.status} />
                    {job.errorSummary && <span className="panel-item-meta source-run-error">{job.errorSummary}</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>

          {(health.alerts.length > 0 || health.warnings.length > 0) && (
            <div className="content-stack" data-testid={`source-health-warnings-${sourceId}`}>
              <h4 className="control-row panel-item-title"><AlertTriangle size={12} aria-hidden /> Warnings</h4>
              {[...health.alerts, ...health.warnings].map((notice) => (
                <div key={`${notice.code ?? 'notice'}:${notice.message}`} className="panel-notice" data-tone="warning">
                  {notice.message}
                </div>
              ))}
            </div>
          )}

          <div className="panel-item-meta" data-testid={`source-health-redactions-${sourceId}`}>
            Redacted: {health.source.redactions.join(', ') || 'none'} / cursor {health.summary.lastCursorSummary}
          </div>
  </>;
}
