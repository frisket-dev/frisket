import type { ReactNode } from 'react';
import { AlertTriangle, CheckCircle2 } from 'lucide-react';
import { WindowCloseButton } from './WindowCloseButton';
import './ActivityProgress.css';

export type ActivityProgressState = 'active' | 'complete' | 'warning' | 'error';

export function ActivityProgressCard({
  state,
  title,
  detail,
  children,
  footer,
  indicator,
  onDismiss,
  dismissLabel = 'Dismiss activity notification',
  dismissTestId,
  testId,
  stage,
  dataStatus,
  className,
}: {
  state: ActivityProgressState;
  title: ReactNode;
  detail?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  indicator?: ReactNode;
  onDismiss?(): void;
  dismissLabel?: string;
  dismissTestId?: string;
  testId?: string;
  stage?: string;
  dataStatus?: string;
  className?: string;
}) {
  const defaultIndicator = state === 'active'
    ? <span className="run-spinner activity-progress-spinner" aria-hidden />
    : state === 'complete'
      ? <CheckCircle2 size={16} className="activity-progress-complete-icon" aria-hidden />
      : <AlertTriangle size={16} className="run-warning-icon" aria-hidden />;

  return (
    <section
      className={`activity-progress-card activity-state-${state}${className ? ` ${className}` : ''}`}
      data-testid={testId}
      data-stage={stage}
      data-status={dataStatus}
      role={state === 'error' ? 'alert' : 'status'}
      aria-live={state === 'error' ? 'assertive' : 'polite'}
      aria-atomic="true"
    >
      <header className="activity-progress-card-header">
        <div className="activity-progress-card-heading">
          {indicator ?? defaultIndicator}
          <div className="activity-progress-card-copy">
            <strong>{title}</strong>
            {detail && <div className="activity-progress-card-detail">{detail}</div>}
          </div>
        </div>
        {onDismiss && (
          <WindowCloseButton
            size="compact"
            className="activity-progress-card-dismiss"
            data-testid={dismissTestId}
            label={dismissLabel}
            onClick={onDismiss}
          />
        )}
      </header>
      {children && <div className="activity-progress-card-body">{children}</div>}
      {footer && <footer className="activity-progress-card-footer">{footer}</footer>}
    </section>
  );
}

export function ProgressBar({
  label,
  completed,
  total,
  state,
  className,
  testId,
}: {
  label: string;
  completed: number | null;
  total: number | null;
  state: ActivityProgressState;
  className?: string;
  testId?: string;
}) {
  const boundedTotal = total !== null && total > 0 ? total : null;
  const boundedCompleted = boundedTotal !== null && completed !== null
    ? Math.max(0, Math.min(completed, boundedTotal))
    : null;
  const determinate = boundedCompleted !== null && boundedTotal !== null;
  const classes = `activity-progress-bar activity-state-${state}${!determinate && state === 'active' ? ' indeterminate' : ''}${className ? ` ${className}` : ''}`;

  if (boundedCompleted !== null && boundedTotal !== null) {
    return (
      <progress
        className={classes}
        data-testid={testId}
        aria-label={label}
        max={boundedTotal}
        value={boundedCompleted}
      />
    );
  }
  return (
    <span
      className={classes}
      data-testid={testId}
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      {...(state === 'complete' ? { 'aria-valuenow': 100 } : {})}
    >
      <span className="activity-progress-bar-fill" />
    </span>
  );
}

export function ProgressRow({
  label,
  labelMeta,
  completed,
  total,
  state,
  status,
  ariaLabel,
  testId,
  className,
  layout = 'default',
}: {
  label: ReactNode;
  labelMeta?: ReactNode;
  completed: number | null;
  total: number | null;
  state: ActivityProgressState;
  status: ReactNode;
  ariaLabel: string;
  testId?: string;
  className?: string;
  layout?: 'default' | 'file';
}) {
  return (
    <div
      className={`activity-progress-row activity-state-${state}${layout === 'file' ? ' activity-progress-row-file' : ''}${className ? ` ${className}` : ''}`}
      data-testid={testId}
    >
      <span className="activity-progress-label">
        <strong>{label}</strong>
        {labelMeta && <span>{labelMeta}</span>}
      </span>
      <ProgressBar label={ariaLabel} completed={completed} total={total} state={state} />
      <span className="activity-progress-count">{status}</span>
    </div>
  );
}
