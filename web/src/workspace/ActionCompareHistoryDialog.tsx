import './ActionCompareHistoryDialog.css';
import { ChevronLeft, ChevronRight, Clock3 } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { ColumnRun, ColumnRunsInfo } from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { Button } from '../components/Button';
import { Modal } from '../components/Modal';
import {
  PanelEmpty,
  PanelError,
  PanelLoading,
  PanelNotice,
} from '../components/PanelPrimitives';
import { formatDuration } from '../format';
import type { ActionRunAnchor } from '../grid/actionRunEntry';

const HISTORY_PAGE_SIZE = 20;

type HistoryLoadState =
  | { columnId: string; status: 'loading'; offset: number; info?: undefined; error?: undefined }
  | { columnId: string; status: 'ready'; offset: number; info: ColumnRunsInfo; error?: undefined }
  | { columnId: string; status: 'error'; offset: number; info?: undefined; error: string };

function runTime(run: ColumnRun): string {
  if (!run.startedAt) return 'Time unavailable';
  const date = new Date(run.startedAt);
  if (!Number.isFinite(date.getTime())) return 'Time unavailable';
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function statusLabel(status: string): string {
  const words = status.trim().replace(/[_-]+/g, ' ');
  return words ? `${words.charAt(0).toUpperCase()}${words.slice(1)}` : 'Unknown status';
}

function rowSummary(run: ColumnRun): string {
  const facts = [
    `${run.completedRows.toLocaleString()} of ${run.totalRows.toLocaleString()} rows completed`,
  ];
  if (run.failedRows > 0) facts.push(`${run.failedRows.toLocaleString()} failed`);
  if (run.durationMs != null) facts.push(formatDuration(run.durationMs));
  return facts.join(' · ');
}

function ActionRunHistoryItem({
  anchor,
  run,
  onChoose,
}: {
  anchor: ActionRunAnchor;
  run: ColumnRun;
  onChoose(anchor: ActionRunAnchor): void;
}) {
  return (
    <li className="action-compare-history-item" data-current={run.current || undefined}>
      <div className="action-compare-history-item-main">
        <div className="action-compare-history-item-title">
          <strong>{run.actionName || 'Action run'}</strong>
          <span className="action-compare-history-status">{statusLabel(run.status)}</span>
          {run.current && <span className="action-compare-history-current">Current</span>}
        </div>
        <div className="action-compare-history-model">
          {run.model?.trim() || 'No model recorded'}
        </div>
        <div className="action-compare-history-meta">
          <span>{rowSummary(run)}</span>
          <span>{runTime(run)}</span>
        </div>
      </div>
      <Button
        type="button"
        data-testid={`action-compare-history-choose-${run.runId}`}
        onClick={() => onChoose({ ...anchor, runId: run.runId })}
      >
        Use settings
      </Button>
    </li>
  );
}

/** Bounded, real stored-run history for one generated-column anchor. Choosing
 * a row only returns its identity; canonical draft restoration remains owned
 * by the comparison-context boundary. */
export function ActionCompareHistoryDialog({
  anchor,
  onChoose,
  onClose,
}: {
  anchor: ActionRunAnchor;
  onChoose(anchor: ActionRunAnchor): void;
  onClose(): void;
}) {
  const { projectApi } = useWorkspaceStores();
  const requestSequence = useRef(0);
  const [state, setState] = useState<HistoryLoadState>({
    columnId: anchor.columnId,
    status: 'loading',
    offset: 0,
  });

  const fetchPage = useCallback((offset: number) => {
    const sequence = ++requestSequence.current;
    void projectApi.getColumnRuns(anchor.columnId, offset, HISTORY_PAGE_SIZE).then(
      (info) => {
        if (requestSequence.current !== sequence) return;
        setState({ columnId: anchor.columnId, status: 'ready', offset, info });
      },
      (error: unknown) => {
        if (requestSequence.current !== sequence) return;
        setState({
          columnId: anchor.columnId,
          status: 'error',
          offset,
          error: error instanceof Error ? error.message : 'Could not load earlier runs.',
        });
      },
    );
  }, [anchor.columnId, projectApi]);

  const loadPage = useCallback((offset: number) => {
    setState({ columnId: anchor.columnId, status: 'loading', offset });
    fetchPage(offset);
  }, [anchor.columnId, fetchPage]);

  useEffect(() => {
    fetchPage(0);
    return () => { requestSequence.current += 1; };
  }, [fetchPage]);

  const visibleState: HistoryLoadState = state.columnId === anchor.columnId
    ? state
    : { columnId: anchor.columnId, status: 'loading', offset: 0 };
  const info = visibleState.status === 'ready' ? visibleState.info : null;
  const newerOffset = visibleState.offset > 0
    ? Math.max(0, visibleState.offset - HISTORY_PAGE_SIZE)
    : null;
  const olderOffset = info?.hasMore ? info.nextOffset : null;
  const pageStart = info && info.runs.length > 0 ? info.offset + 1 : 0;
  const pageEnd = info ? info.offset + info.runs.length : 0;

  return (
    <Modal
      title={<><Clock3 size={15} aria-hidden /> Earlier runs</>}
      onClose={onClose}
      closeLabel="Close earlier runs"
      testId="action-compare-history-dialog"
      size="wide"
    >
      <p className="action-compare-history-intro">
        Choose a stored run to load its settings. Your comparison rows and final run scope stay unchanged.
      </p>

      {visibleState.status === 'loading' && (
        <PanelLoading label="Loading earlier runs…" testId="action-compare-history-loading" />
      )}
      {visibleState.status === 'error' && (
        <>
          <PanelError testId="action-compare-history-error">{visibleState.error}</PanelError>
          <Button type="button" onClick={() => loadPage(visibleState.offset)}>Try again</Button>
        </>
      )}
      {info && (
        <>
          <div className="action-compare-history-summary">
            <strong>{info.columnName}</strong>
            <span>
              {info.totalRuns.toLocaleString()} stored {info.totalRuns === 1 ? 'run' : 'runs'}
            </span>
          </div>
          {info.mixedOrigins && (
            <PanelNotice tone="warning" className="action-compare-history-mixed" role="note">
              Current values include more than one run or manual edits. History restores settings only.
            </PanelNotice>
          )}
          {info.runs.length === 0 ? (
            <PanelEmpty testId="action-compare-history-empty">No stored runs are available.</PanelEmpty>
          ) : (
            <ul className="action-compare-history-list">
              {info.runs.map((run) => (
                <ActionRunHistoryItem
                  key={run.runId}
                  anchor={anchor}
                  run={run}
                  onChoose={onChoose}
                />
              ))}
            </ul>
          )}
          <footer className="action-compare-history-footer">
            <span>
              {pageStart.toLocaleString()}–{pageEnd.toLocaleString()} of{' '}
              {info.totalRuns.toLocaleString()}
            </span>
            <div>
              <Button
                type="button"
                disabled={newerOffset == null}
                onClick={() => newerOffset != null && loadPage(newerOffset)}
              >
                <ChevronLeft size={14} aria-hidden /> Newer
              </Button>
              <Button
                type="button"
                disabled={olderOffset == null}
                onClick={() => olderOffset != null && loadPage(olderOffset)}
              >
                Older <ChevronRight size={14} aria-hidden />
              </Button>
            </div>
          </footer>
        </>
      )}
    </Modal>
  );
}
