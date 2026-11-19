import './HistoryPanel.css';
import { CopyButton } from './CopyButton';
import { useState, type ReactNode } from 'react';
import { PanelEmpty } from './PanelPrimitives';
import {
  AlertTriangle, ArrowDownToLine, ChevronDown, ChevronUp, FileInput,
  GitBranch, ListChecks, Wand2,
} from 'lucide-react';
import type { HistoryChange, HistoryChangeRun, HistoryState } from '../api/open';
import { formatUsd } from '../actions/model';
import { runLinkHref, useRoute } from '../routes';
import {
  ListDetailGrid,
  ListDetailHeader,
  WorkbenchListDetailPanel,
  type ListDetailColumn,
} from '../workbench/WorkbenchListDetailPanel';
import { RowErrorGroupList } from '../workbench/RowErrorGroupList';

const KIND_ICONS: Record<HistoryChange['kind'], typeof Wand2> = {
  import: FileInput,
  map: Wand2,
  derive: GitBranch,
  edit: ArrowDownToLine,
  'review-batch': ListChecks,
  sort: ArrowDownToLine,
};
const EMPTY_HISTORY_CHANGES: HistoryChange[] = [];

export interface HistoryPanelProps {
  history: HistoryState | null;
  onRevert(changeId: string): Promise<void>;
  onRestore(changeId: string): Promise<void>;
  onLoadPage(offset: number, limit: number): Promise<void>;
  /** The change a run link asked for; selected instead of the cursor change. */
  focusChangeId: string | null;
}

function changeStateLabel(change: HistoryChange): string {
  return change.status === 'applied' ? 'Applied' : change.status === 'undone' ? 'Reverted' : 'Archived';
}

function formatOpTime(at: string): string {
  return new Date(at).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function changeIcon(change: HistoryChange) {
  return change.barrier ? AlertTriangle : KIND_ICONS[change.kind];
}

function rowOutcomeSummary(run: HistoryChangeRun): string {
  if (run.totalRows <= 0) return '—';
  if (run.failedRows > 0) {
    return `${run.failedRows.toLocaleString()} of ${run.totalRows.toLocaleString()} rows failed`;
  }
  return `${run.completedRows.toLocaleString()} of ${run.totalRows.toLocaleString()} rows succeeded`;
}

function generatedColumnsSummary(run: HistoryChangeRun): string {
  return run.generatedColumns.length > 0
    ? run.generatedColumns.map((c) => c.name).join(', ')
    : '—';
}

/**
 * The change history uses the same list/detail layout as Jobs. Inspect a
 * change, then explicitly revert or restore it without walking the Undo cursor.
 * Older/Newer paging surfaces above/below the list.
 */
export function HistoryPanel({ history, onRevert, onRestore, onLoadPage, focusChangeId }: HistoryPanelProps) {
  const [pendingOperation, setPendingOperation] = useState<string | null>(null);
  const [loadingPage, setLoadingPage] = useState<'older' | 'newer' | null>(null);
  const loadPage = (offset: number, limit: number, direction: 'older' | 'newer') => {
    if (loadingPage !== null) return;
    setLoadingPage(direction);
    void onLoadPage(offset, limit).catch(() => undefined).finally(() => setLoadingPage(null));
  };

  const route = useRoute();
  const deepLinkFor = (runId: string): string | null => (route.kind === 'project'
    ? runLinkHref(route.projectId, route.sheetId, runId, window.location.origin)
    : null);

  const changes = history?.changes ?? EMPTY_HISTORY_CHANGES;
  const pointer = history?.cursorIndex ?? -1;
  const currentChangeId = changes.find((change) => change.index === pointer)?.id ?? null;
  const focusedChangeId = changes.some((change) => change.id === focusChangeId) ? focusChangeId : null;

  const columns: Array<ListDetailColumn<HistoryChange>> = [
    {
      key: 'op',
      label: 'Operation',
      defaultWidth: 240,
      render: (change) => {
        const Icon = changeIcon(change);
        return (
          <>
            <span className="history-icon"><Icon size={13} /></span>
            <span className="history-label">{change.label}</span>
            {change.id === history?.undoTarget?.id && <span className="history-pointer">undo next</span>}
          </>
        );
      },
    },
    {
      key: 'rows',
      label: 'Rows',
      defaultWidth: 90,
      render: (change) => (change.rowsAffected > 0 ? change.rowsAffected.toLocaleString() : '—'),
    },
    {
      key: 'cost',
      label: 'Cost',
      defaultWidth: 90,
      render: (change) => (change.cost != null ? formatUsd(change.cost) : '—'),
    },
    {
      key: 'when',
      label: 'When',
      defaultWidth: 150,
      render: (change) => formatOpTime(change.at),
    },
    {
      key: 'state',
      label: 'State',
      defaultWidth: 96,
      render: (change) => changeStateLabel(change),
    },
  ];

  const renderDetail = (change: HistoryChange): ReactNode => {
    const stateLabel = changeStateLabel(change);
    const time = formatOpTime(change.at);
    const Icon = changeIcon(change);
    const run = change.run;
    const deepLink = run ? deepLinkFor(run.runId) : null;
    const paramsJson = run ? JSON.stringify(run.params, null, 2) : null;
    return (
      <div data-testid={`history-step-detail-op-${change.id}`}>
        <ListDetailHeader
          title={
            <>
              <span className="history-icon"><Icon size={13} /></span>
              {change.label}
            </>
          }
          status={stateLabel}
        />
        <ListDetailGrid
          rows={[
            ['Step', String(change.index + 1)],
            ['State', stateLabel],
            ['When', time],
            ['Rows', change.rowsAffected > 0 ? change.rowsAffected.toLocaleString() : '—'],
            ['Cost', change.cost != null ? formatUsd(change.cost) : '—'],
            ...(run
              ? ([
                  [
                    'Run ID',
                    <span key="run-id" className="history-run-id-row">
                      <code data-testid={`history-run-id-${change.id}`}>{run.runId}</code>
                      <CopyButton text={run.runId} testId={`history-copy-run-id-${change.id}`} />
                    </span>,
                  ],
                  [
                    'Deep link',
                    deepLink
                      ? (
                        <span key="deep-link" className="history-deep-link-row">
                          <code data-testid={`history-deep-link-${change.id}`}>{deepLink}</code>
                          <CopyButton
                            text={deepLink}
                            testId={`history-copy-deep-link-${change.id}`}
                          />
                        </span>
                      )
                      : '—',
                  ],
                  [
                    'Worker version',
                    <code key="worker-version" data-testid={`history-worker-version-${change.id}`}>
                      {run.workerVersion ?? 'unknown'}
                    </code>,
                  ],
                  [
                    'Generated columns',
                    <span key="output-columns" data-testid={`history-output-columns-${change.id}`}>
                      {generatedColumnsSummary(run)}
                    </span>,
                  ],
                  [
                    'Row outcomes',
                    <span
                      key="row-outcomes"
                      data-testid={`history-row-failure-summary-${change.id}`}
                    >
                      {rowOutcomeSummary(run)}
                    </span>,
                  ],
                ] satisfies Array<[ReactNode, ReactNode]>)
              : []),
          ]}
        />
        {run && run.rowErrors && run.rowErrors.groups.length > 0 && (
          <section
            className="bottom-dock-detail-section"
            data-testid={`history-row-errors-${change.id}`}
          >
            <h4>Row errors ({run.rowErrors.totalFailedRows.toLocaleString()})</h4>
            <RowErrorGroupList groups={run.rowErrors.groups} />
          </section>
        )}
        {run && paramsJson && (
          <section className="bottom-dock-detail-section">
            <div className="history-params-header">
              <strong>Params</strong>
              <CopyButton text={paramsJson} testId={`history-copy-params-${change.id}`} />
            </div>
            <pre
              className="history-params-json"
              data-testid={`history-run-params-${change.id}`}
            >
              {paramsJson}
            </pre>
          </section>
        )}
        <section className="bottom-dock-detail-section">
          <button
            type="button"
            className="history-page-button"
            data-testid={`history-${change.status === 'applied' ? 'revert' : 'restore'}-op-${change.id}`}
            disabled={!change.executable || change.status === 'discarded' || pendingOperation !== null}
            aria-label={`${change.status === 'applied' ? 'Revert' : 'Restore'} ${change.label}`}
            onClick={() => {
              if (pendingOperation !== null) return;
              setPendingOperation(change.id);
              const transition = change.status === 'applied' ? onRevert : onRestore;
              void transition(change.id).finally(() => setPendingOperation(null));
            }}
          >
            {pendingOperation === change.id ? 'Working…' : change.status === 'applied' ? 'Revert change' : 'Restore change'}
          </button>
          {!change.executable && <p>This change is read-only.</p>}
        </section>
      </div>
    );
  };

  const listHeader = history?.hasMoreBefore
    ? history.prevOffset !== null
      ? (
        <div className="history-page-control">
          <button
            type="button"
            className="history-page-button"
            data-testid="history-load-older"
            disabled={loadingPage !== null}
            onClick={() => loadPage(history.prevOffset!, history.limit, 'older')}
          >
            <ChevronUp size={12} aria-hidden />
            {loadingPage === 'older' ? 'Loading' : 'Older'}
          </button>
        </div>
      )
      : <PanelEmpty>older operations not loaded</PanelEmpty>
    : null;

  const listFooter = history?.hasMoreAfter
    ? history.nextOffset !== null
      ? (
        <div className="history-page-control">
          <button
            type="button"
            className="history-page-button"
            data-testid="history-load-newer"
            disabled={loadingPage !== null}
            onClick={() => loadPage(history.nextOffset!, history.limit, 'newer')}
          >
            <ChevronDown size={12} aria-hidden />
            {loadingPage === 'newer' ? 'Loading' : 'Newer'}
          </button>
        </div>
      )
      : <PanelEmpty>newer operations not loaded</PanelEmpty>
    : null;

  const emptyText = history === null
    ? 'loading…'
    : 'No operations yet — import a CSV to start the log.';

  return (
    <WorkbenchListDetailPanel
      // A new run-link focus replaces whatever row the reader picked before.
      key={focusedChangeId ?? 'cursor'}
      items={changes}
      getRowId={(change) => change.id}
      columns={columns}
      renderDetail={renderDetail}
      emptyText={emptyText}
      ariaLabel="Operation history"
      detailAriaLabel="Selected operation details"
      detailEmptyText="Select an operation to inspect it."
      tableWidthsStorageKey="frisket:bottom-dock-table-widths:history"
      detailWidthStorageKey="frisket:bottom-dock-detail-width"
      listTestId="history-list"
      rowTestId={(change) => `history-step-op-${change.id}`}
      rowClassName={(change) => `history-state-${change.status}`}
      defaultSelectedId={() => focusedChangeId ?? currentChangeId}
      activeRowId={currentChangeId}
      listHeader={listHeader}
      listFooter={listFooter}
    />
  );
}
