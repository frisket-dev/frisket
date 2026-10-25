import './ActionCompareHistoryMenu.css';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ColumnRun, ColumnRunsInfo } from '../api/types';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { AnchoredMenu } from '../components/MenuPop';
import { countLabel } from '../format';
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

function runStatus(run: ColumnRun): string {
  if (run.current) return 'In table';
  const words = run.status.trim().replace(/[_-]+/g, ' ');
  return words ? `${words.charAt(0).toUpperCase()}${words.slice(1)}` : 'Unknown';
}

function ActionRunHistoryItem({
  anchor,
  columnName,
  run,
  onChoose,
}: {
  anchor: ActionRunAnchor;
  columnName: string;
  run: ColumnRun;
  onChoose(anchor: ActionRunAnchor): void;
}) {
  return (
    <li>
      <button
        type="button"
        role="menuitem"
        className="menu-item action-compare-history-item"
        data-active={run.current || undefined}
        data-testid={`action-compare-history-choose-${run.runId}`}
        onClick={() => onChoose({ ...anchor, runId: run.runId })}
      >
        <span className="menu-item-copy">
          <span className="menu-item-label">
            <span className="action-compare-history-column">{columnName}</span>
            <span className="action-compare-history-status">{runStatus(run)}</span>
          </span>
          <span className="menu-item-description">
            {run.model?.trim() || 'No model'} · {countLabel(run.totalRows, 'row')} · {runTime(run)}
          </span>
        </span>
      </button>
    </li>
  );
}

/** Bounded stored-run history anchored to the action-header clock. Choosing a
 * real run returns only its identity; canonical settings restoration remains
 * owned by the comparison-context boundary. */
export function ActionCompareHistoryMenu({
  anchor,
  trigger,
  onChoose,
  onClose,
}: {
  anchor: ActionRunAnchor;
  trigger: HTMLButtonElement;
  onChoose(anchor: ActionRunAnchor): void;
  onClose(): void;
}) {
  const { projectApi } = useWorkspaceStores();
  const triggerRef = useMemo(() => ({ current: trigger }), [trigger]);
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

  return (
    <AnchoredMenu
      anchorRef={triggerRef}
      onClose={onClose}
      width={360}
      align="right"
      dismissOnFocusOutside
      className="action-compare-history-menu"
      data-testid="action-compare-history-menu"
      aria-label="Earlier runs"
    >
      <div className="menu-section-label">Earlier runs</div>
      {visibleState.status === 'loading' && (
        <div className="menu-empty" role="status">Loading…</div>
      )}
      {visibleState.status === 'error' && (
        <>
          <div className="menu-error" role="alert">{visibleState.error}</div>
          <button type="button" className="menu-item" onClick={() => loadPage(visibleState.offset)}>
            Try again
          </button>
        </>
      )}
      {info && (
        <>
          <div className="action-compare-history-summary">
            {info.columnName} · {countLabel(info.totalRuns, 'run')}
          </div>
          {info.runs.length === 0 ? (
            <div className="menu-empty" data-testid="action-compare-history-empty">
              No stored runs.
            </div>
          ) : (
            <ul className="action-compare-history-list">
              {info.runs.map((run) => (
                <ActionRunHistoryItem
                  key={run.runId}
                  anchor={anchor}
                  columnName={info.columnName}
                  run={run}
                  onChoose={onChoose}
                />
              ))}
            </ul>
          )}
          <footer className="action-compare-history-footer">
            <span>
              {info.offset + (info.runs.length > 0 ? 1 : 0)}–{info.offset + info.runs.length}
              {' '}of {info.totalRuns.toLocaleString()}
            </span>
            <div>
              <button type="button" className="menu-item" disabled={newerOffset == null}
                onClick={() => newerOffset != null && loadPage(newerOffset)}>
                <ChevronLeft size={14} aria-hidden /> Newer
              </button>
              <button type="button" className="menu-item" disabled={olderOffset == null}
                onClick={() => olderOffset != null && loadPage(olderOffset)}>
                Older <ChevronRight size={14} aria-hidden />
              </button>
            </div>
          </footer>
        </>
      )}
    </AnchoredMenu>
  );
}
