import { useState, type ReactNode } from 'react';
import { PanelEmpty } from './PanelPrimitives';
import {
  AlertTriangle, ArrowDownToLine, Check, ChevronDown, ChevronUp, Copy, FileInput,
  GitBranch, ListChecks, Wand2,
} from 'lucide-react';
import type { HistoryOp, HistoryOpRun, HistoryState } from '../api/open';
import { formatUsd } from '../actions/model';
import { routePath, useRoute } from '../routes';
import {
  ListDetailGrid,
  ListDetailHeader,
  WorkbenchListDetailPanel,
  type ListDetailColumn,
} from '../workbench/WorkbenchListDetailPanel';
import { RowErrorGroupList } from '../workbench/RowErrorGroupList';

const KIND_ICONS: Record<HistoryOp['kind'], typeof Wand2> = {
  import: FileInput,
  map: Wand2,
  derive: GitBranch,
  edit: ArrowDownToLine,
  'review-batch': ListChecks,
  sort: ArrowDownToLine,
};
const EMPTY_HISTORY_OPS: HistoryOp[] = [];

export interface HistoryPanelProps {
  history: HistoryState | null;
  onStepTo(opIndex: number): void;
  onLoadPage(offset: number, limit: number): Promise<void>;
}

function opState(op: HistoryOp, pointer: number): 'current' | 'applied' | 'undone' {
  return op.index === pointer ? 'current' : op.index < pointer ? 'applied' : 'undone';
}

function opStateLabel(state: ReturnType<typeof opState>): string {
  return state === 'current' ? 'Current' : state === 'applied' ? 'Applied' : 'Undone';
}

function formatOpTime(at: string): string {
  return new Date(at).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  });
}

function opIcon(op: HistoryOp) {
  return op.barrier ? AlertTriangle : KIND_ICONS[op.kind];
}

/** Copy-to-clipboard, mirroring RowDrawer.tsx's
 * local CopyButton (not exported from there, so re-implemented here rather
 * than reaching across component boundaries for a five-line helper). */
function CopyButton({ text, testId }: { text: string; testId: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="mini-btn"
      data-testid={testId}
      title="Copy to clipboard"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        });
      }}
    >
      {copied ? <Check size={11} /> : <Copy size={11} />} {copied ? 'copied' : 'copy'}
    </button>
  );
}

function rowOutcomeSummary(run: HistoryOpRun): string {
  if (run.totalRows <= 0) return '—';
  if (run.failedRows > 0) {
    return `${run.failedRows.toLocaleString()} of ${run.totalRows.toLocaleString()} rows failed`;
  }
  return `${run.completedRows.toLocaleString()} of ${run.totalRows.toLocaleString()} rows succeeded`;
}

function outputColumnsSummary(run: HistoryOpRun): string {
  return run.outputColumns.length > 0
    ? run.outputColumns.map((c) => c.name).join(', ')
    : '—';
}

/**
 * The OpenRefine-style op log, rendered in the bottom drawer as a list + a
 * right-hand detail pane (the same list/detail template as Jobs). Select any
 * step to inspect it on the right, then explicitly Restore from the detail area.
 * Older/Newer paging surfaces above/below the list.
 */
export function HistoryPanel({ history, onStepTo, onLoadPage }: HistoryPanelProps) {
  const [loadingPage, setLoadingPage] = useState<'older' | 'newer' | null>(null);
  const loadPage = (offset: number, limit: number, direction: 'older' | 'newer') => {
    if (loadingPage !== null) return;
    setLoadingPage(direction);
    void onLoadPage(offset, limit).catch(() => undefined).finally(() => setLoadingPage(null));
  };

  // The deep link reuses the SAME route helpers the app navigates with
  // (routes.ts) rather than hand-building a path — project/sheet form plus a
  // ?run= query param, since the Route type has no dedicated run panel kind.
  const route = useRoute();
  const deepLinkFor = (runId: string): string | null => {
    if (route.kind !== 'project') return null;
    const path = routePath({ kind: 'project', projectId: route.projectId, sheetId: route.sheetId });
    const origin = typeof window !== 'undefined' ? window.location.origin : '';
    return `${origin}${path}?run=${encodeURIComponent(runId)}`;
  };

  const ops = history?.ops ?? EMPTY_HISTORY_OPS;
  const pointer = history?.cursorIndex ?? -1;
  const currentOpId = ops.find((op) => op.index === pointer)?.id ?? null;

  const columns: Array<ListDetailColumn<HistoryOp>> = [
    {
      key: 'op',
      label: 'Operation',
      defaultWidth: 240,
      render: (op) => {
        const Icon = opIcon(op);
        return (
          <>
            <span className="history-icon"><Icon size={13} /></span>
            <span className="history-label">{op.label}</span>
            {op.index === pointer && <span className="history-pointer">now</span>}
          </>
        );
      },
    },
    {
      key: 'rows',
      label: 'Rows',
      defaultWidth: 90,
      render: (op) => (op.rowsAffected > 0 ? op.rowsAffected.toLocaleString() : '—'),
    },
    {
      key: 'cost',
      label: 'Cost',
      defaultWidth: 90,
      render: (op) => (op.cost != null ? formatUsd(op.cost) : '—'),
    },
    {
      key: 'when',
      label: 'When',
      defaultWidth: 150,
      render: (op) => formatOpTime(op.at),
    },
    {
      key: 'state',
      label: 'State',
      defaultWidth: 96,
      render: (op) => opStateLabel(opState(op, pointer)),
    },
  ];

  const renderDetail = (op: HistoryOp): ReactNode => {
    const state = opState(op, pointer);
    const stateLabel = opStateLabel(state);
    const time = formatOpTime(op.at);
    const Icon = opIcon(op);
    const run = op.run;
    const deepLink = run ? deepLinkFor(run.runId) : null;
    const paramsJson = run ? JSON.stringify(run.params, null, 2) : null;
    return (
      <div data-testid={`history-step-detail-op-${op.id}`}>
        <ListDetailHeader
          title={
            <>
              <span className="history-icon"><Icon size={13} /></span>
              {op.label}
            </>
          }
          status={stateLabel}
        />
        <ListDetailGrid
          rows={[
            ['Step', String(op.index + 1)],
            ['State', stateLabel],
            ['When', time],
            ['Rows', op.rowsAffected > 0 ? op.rowsAffected.toLocaleString() : '—'],
            ['Cost', op.cost != null ? formatUsd(op.cost) : '—'],
            ...(run
              ? ([
                  [
                    'Run ID',
                    <span key="run-id" className="history-run-id-row">
                      <code data-testid={`history-run-id-${op.id}`}>{run.runId}</code>
                      <CopyButton text={run.runId} testId={`history-copy-run-id-${op.id}`} />
                    </span>,
                  ],
                  [
                    'Deep link',
                    deepLink
                      ? (
                        <span key="deep-link" className="history-deep-link-row">
                          <code data-testid={`history-deep-link-${op.id}`}>{deepLink}</code>
                          <CopyButton
                            text={deepLink}
                            testId={`history-copy-deep-link-${op.id}`}
                          />
                        </span>
                      )
                      : '—',
                  ],
                  [
                    'Worker version',
                    <code key="worker-version" data-testid={`history-worker-version-${op.id}`}>
                      {run.workerVersion ?? 'unknown'}
                    </code>,
                  ],
                  [
                    'Output columns',
                    <span key="output-columns" data-testid={`history-output-columns-${op.id}`}>
                      {outputColumnsSummary(run)}
                    </span>,
                  ],
                  [
                    'Row outcomes',
                    <span
                      key="row-outcomes"
                      data-testid={`history-row-failure-summary-${op.id}`}
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
            data-testid={`history-row-errors-${op.id}`}
          >
            <h4>Row errors ({run.rowErrors.totalFailedRows.toLocaleString()})</h4>
            <RowErrorGroupList groups={run.rowErrors.groups} />
          </section>
        )}
        {run && paramsJson && (
          <section className="bottom-dock-detail-section">
            <div className="history-params-header">
              <strong>Params</strong>
              <CopyButton text={paramsJson} testId={`history-copy-params-${op.id}`} />
            </div>
            <pre
              className="history-params-json"
              data-testid={`history-run-params-${op.id}`}
            >
              {paramsJson}
            </pre>
          </section>
        )}
        <section className="bottom-dock-detail-section">
          <button
            type="button"
            className="history-page-button"
            data-testid={`history-restore-op-${op.id}`}
            disabled={op.index === pointer}
            aria-label={`Restore history to ${op.label}`}
            onClick={() => onStepTo(op.index)}
          >
            Restore
          </button>
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
      items={ops}
      getRowId={(op) => op.id}
      columns={columns}
      renderDetail={renderDetail}
      emptyText={emptyText}
      ariaLabel="Operation history"
      detailAriaLabel="Selected operation details"
      detailEmptyText="Select an operation to inspect it."
      tableWidthsStorageKey="frisket:bottom-dock-table-widths:history"
      detailWidthStorageKey="frisket:bottom-dock-detail-width"
      listTestId="history-list"
      rowTestId={(op) => `history-step-op-${op.id}`}
      rowClassName={(op) => `history-state-${opState(op, pointer)}`}
      defaultSelectedId={() => currentOpId}
      activeRowId={currentOpId}
      listHeader={listHeader}
      listFooter={listFooter}
    />
  );
}
