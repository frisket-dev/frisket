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
import { ActivityProgressCard, ProgressBar, type ActivityProgressState } from './ActivityProgress';

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
  /** Uses the operation's backend-declared remove capability. */
  uninstall?: (ref: string) => Promise<ModelPullDto>;
  readOnly?: boolean;
  /** A parent already owns this pull's poll loop and supplies fresh snapshots. */
  poll?: boolean;
  /** Surface a parent-owned poll failure without starting another request loop. */
  externalPollError?: boolean;
}

export function ModelPullProgress({
  pull: initialPull,
  onDone,
  onFailed,
  fetchPull = getModelPull,
  cancelPull = cancelModelPull,
  uninstall = uninstallArtifact,
  readOnly = false,
  poll = true,
  externalPollError = false,
}: ModelPullProgressProps) {
  const [pull, setPull] = useState(initialPull);
  const [cancelling, setCancelling] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [pollError, setPollError] = useState(false);
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
    setPollError(false);
  }

  // Selector setup owns one poll for both its closed trigger and open panel.
  // Keep that externally supplied snapshot in this existing renderer while
  // preserving local optimistic cancellation until the parent publishes a
  // newer projection.
  const [lastExternalPull, setLastExternalPull] = useState(initialPull);
  if (!poll && initialPull !== lastExternalPull) {
    setLastExternalPull(initialPull);
    setPull(initialPull);
    setPollError(false);
  }

  const active = ACTIVE_STATUSES.includes(pull.status);

  usePoll(
    async () => {
      let fresh: ModelPullDto;
      try {
        fresh = await fetchPull(pull.id);
      } catch {
        // Keep the latest projection and optimistic cancellation on a failed GET.
        setPollError(true);
        return;
      }
      setPollError(false);
      setPull(fresh);
      if (fresh.status === 'done') onDone?.(fresh);
      else if (fresh.status === 'failed' || fresh.status === 'cancelled') {
        onFailed?.(fresh);
      }
    },
    { intervalMs: 1000, active: poll && active, guardOverlap: true },
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
    percent != null
      ? `${percent}% · ${humanBytes(completed)} / ${humanBytes(total)}`
      : completed != null && completed > 0
        ? humanBytes(completed)
        : null;

  const presentationState: ActivityProgressState = active
    ? 'active'
    : pull.status === 'failed'
      ? 'error'
      : pull.status === 'done' || pull.status === 'uninstalled'
        ? 'complete'
        : 'warning';
  const canCancel = active && !readOnly;
  const canUninstall = pull.status === 'done' && !readOnly && pull.capabilities.remove;
  const cardDetail = pull.status === 'failed'
    ? 'Download failed'
    : pull.status === 'cancelled'
      ? 'Download cancelled'
      : pull.status === 'done'
        ? 'Download complete'
        : pull.status === 'uninstalled'
          ? 'Model uninstalled'
          : pull.phase || undefined;

  return (
    <ActivityProgressCard
      state={presentationState}
      title={pull.display_name}
      detail={cardDetail}
      className="model-pull-progress"
      testId="model-pull-progress"
      dataStatus={pull.status}
      footer={canCancel || canUninstall ? (
        <>
          {canCancel && (
            <button
              type="button"
              className="btn btn-compact"
              data-testid="model-pull-cancel"
              disabled={!pull.capabilities.cancel || cancelling || pull.cancel_requested}
              onClick={() => void cancel()}
            >
              {pull.cancel_requested ? 'Cancelling…' : 'Cancel'}
            </button>
          )}
          {canUninstall && (
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
        </>
      ) : undefined}
    >
      {active && (
        <>
          <ProgressBar
            label={`Downloading ${pull.display_name}`}
            completed={completed}
            total={total}
            state="active"
            className="model-pull-progress-bar"
          />
          {sizeLabel && <span className="model-pull-progress-size">{sizeLabel}</span>}
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
      {(pollError || externalPollError) && (
        <p className="settings-inline-status settings-validation-message is-error" role="alert">
          Could not refresh setup progress. Waiting for the next update.
        </p>
      )}
      {pull.status === 'failed' && pull.error && (
        <p
          className="model-pull-progress-error"
          data-testid="model-pull-error"
          data-error-code={pull.error.code ?? undefined}
          title={pull.error.code ?? undefined}
        >
          {pull.error.message}
        </p>
      )}
      {pull.status === 'done' && (
        <p className="model-pull-progress-done" data-testid="model-pull-done">
          {pull.display_name} installed
          {pull.resolved_size != null ? ` (${humanBytes(pull.resolved_size)})` : ''}
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
    </ActivityProgressCard>
  );
}
