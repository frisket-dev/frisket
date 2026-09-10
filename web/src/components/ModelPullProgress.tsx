// Live progress for a single in-app model pull. Renders inline wherever a pull
// needs to be watched: LocalServerGuidance's Download flow and the Settings
// active/recent pulls strip. Polls GET /providers/models/pulls/{id} every ~1s via
// the shared usePoll hook while the pull is still pending/running, and calls
// `onDone` once when a poll observes the status flip to 'done' so the parent can
// refresh its own catalog/list.
//
// Byte progress is indeterminate (no percent, sliding-bar animation) until
// `total_bytes` is known -- Ollama reports progress per layer digest and doesn't
// know a layer's size until it starts streaming it.

import { useState } from 'react';
import { cancelModelPull, getModelPull, uninstallArtifact } from '../api/open';
import type { ModelPullDto } from '../api/types';
import { usePoll } from '../hooks/usePoll';
import { formatNumberDisplay } from '../format';

const ACTIVE_STATUSES: ModelPullDto['status'][] = ['pending', 'running'];

function humanBytes(n: number | null): string | null {
  if (n == null) return null;
  return formatNumberDisplay(n, 'filesize');
}

interface ModelPullProgressProps {
  pull: ModelPullDto;
  /** Called once when a poll observes the pull reach 'done'. */
  onDone?: (pull: ModelPullDto) => void;
  /** Called once when a poll observes the pull reach a terminal FAILED or
   *  CANCELLED state — lets an embedder (the pair picker) clear
   *  back to a retryable state with the error surfaced. */
  onFailed?: (pull: ModelPullDto) => void;
  /** Injectable fetch/cancel, defaulting to the workspace-tier local pull
   *  route (getModelPull/cancelModelPull). The org/team settings card
   *  injects orgGetModelPull/orgCancelModelPull instead -- same ModelPullDto shape,
   *  a different route underneath. Every other caller is unaffected: the
   *  defaults preserve the pre-existing behavior exactly. */
  fetchPull?: (id: number) => Promise<ModelPullDto>;
  cancelPull?: (id: number) => Promise<unknown>;
  /** Uninstall a completed artifact pull (removes bytes, tombstones the
   *  row with the distinct `uninstalled` status). Only shown for a `done`
   *  pull whose `artifact` is non-null (an opus-mt:/hf: pull); Ollama models
   *  are the daemon's to manage. Defaults to the workspace uninstall route. */
  uninstall?: (ref: string) => Promise<ModelPullDto>;
}

export function ModelPullProgress({
  pull: initialPull,
  onDone,
  onFailed,
  fetchPull = getModelPull,
  cancelPull = cancelModelPull,
  uninstall = uninstallArtifact,
}: ModelPullProgressProps) {
  const [pull, setPull] = useState(initialPull);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [uninstalling, setUninstalling] = useState(false);
  const [uninstallError, setUninstallError] = useState<string | null>(null);

  // A new pull identity (e.g. a fresh Download after a prior one finished)
  // resets local state -- the sanctioned adjust-state-during-render pattern
  // used elsewhere in this codebase (OllamaUrlSettings' draft/lastUrl).
  const [lastId, setLastId] = useState(initialPull.id);
  if (initialPull.id !== lastId) {
    setLastId(initialPull.id);
    setPull(initialPull);
    setCancelling(false);
    setCancelError(null);
  }

  const active = ACTIVE_STATUSES.includes(pull.status);

  usePoll(
    async () => {
      const fresh = await fetchPull(pull.id);
      setPull(fresh);
      if (fresh.status === 'done') onDone?.(fresh);
      else if (fresh.status === 'failed' || fresh.status === 'cancelled') {
        onFailed?.(fresh);
      }
    },
    { intervalMs: 1000, active, guardOverlap: true },
  );

  const cancel = async () => {
    setCancelling(true);
    setCancelError(null);
    try {
      await cancelPull(pull.id);
      setPull((current) => ({ ...current, cancel_requested: true }));
    } catch (caught) {
      setCancelError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setCancelling(false);
    }
  };

  const doUninstall = async () => {
    setUninstalling(true);
    setUninstallError(null);
    try {
      const next = await uninstall(pull.model);
      setPull(next);
    } catch (caught) {
      setUninstallError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setUninstalling(false);
    }
  };

  const total = pull.total_bytes;
  const completed = pull.completed_bytes;
  const percent =
    total != null && total > 0 && completed != null
      ? Math.max(0, Math.min(100, Math.round((completed / total) * 100)))
      : null;

  const sizeLabel =
    total != null && completed != null
      ? `${humanBytes(completed)} / ${humanBytes(total)}`
      : completed != null
        ? humanBytes(completed)
        : null;

  return (
    <div
      className="model-pull-progress"
      data-testid="model-pull-progress"
      data-status={pull.status}
    >
      <div className="model-pull-progress-header">
        <span className="model-pull-progress-name">{pull.model}</span>
        {pull.phase && <span className="model-pull-progress-phase">{pull.phase}</span>}
      </div>
      {active && (
        <>
          <span
            className={`model-pull-progress-bar${percent === null ? ' indeterminate' : ''}`}
            role="progressbar"
            aria-label={`Downloading ${pull.model}`}
            aria-valuemin={0}
            aria-valuemax={100}
            {...(percent !== null ? { 'aria-valuenow': percent } : {})}
          >
            <span
              className="model-pull-progress-fill"
              style={percent !== null ? { width: `${percent}%` } : undefined}
            />
          </span>
          <span className="model-pull-progress-size">{sizeLabel ?? 'starting…'}</span>
          <button
            type="button"
            className="btn btn-compact"
            data-testid="model-pull-cancel"
            disabled={cancelling || pull.cancel_requested}
            onClick={() => void cancel()}
          >
            {pull.cancel_requested ? 'Cancelling…' : 'Cancel'}
          </button>
          {cancelError && (
            <p
              className="settings-inline-status settings-validation-message is-error"
              data-testid="model-pull-cancel-error"
            >
              {cancelError}
            </p>
          )}
        </>
      )}
      {pull.status === 'failed' && pull.error && (
        <p className="model-pull-progress-error" data-testid="model-pull-error">
          {pull.error.code}: {pull.error.message}
        </p>
      )}
      {pull.status === 'done' && (
        <p className="model-pull-progress-done" data-testid="model-pull-done">
          {pull.model} installed
          {pull.resolved_size != null ? ` (${humanBytes(pull.resolved_size)})` : ''}
          {/* Uninstall is offered only for a pulled artifact (opus-mt:/hf:);
              Ollama models are managed by the daemon. */}
          {pull.artifact != null && (
            <button
              type="button"
              className="btn btn-compact"
              data-testid="model-pull-uninstall"
              disabled={uninstalling}
              onClick={() => void doUninstall()}
            >
              {uninstalling ? 'Uninstalling…' : 'Uninstall'}
            </button>
          )}
        </p>
      )}
      {uninstallError && (
        <p
          className="settings-inline-status settings-validation-message is-error"
          data-testid="model-pull-uninstall-error"
        >
          {uninstallError}
        </p>
      )}
      {pull.status === 'cancelled' && (
        <p className="model-pull-progress-cancelled" data-testid="model-pull-cancelled">
          Cancelled.
        </p>
      )}
      {pull.status === 'uninstalled' && (
        <p
          className="model-pull-progress-cancelled"
          data-testid="model-pull-uninstalled"
        >
          {pull.model} uninstalled.
        </p>
      )}
    </div>
  );
}
