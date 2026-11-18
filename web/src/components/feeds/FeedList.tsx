import { ChevronDown, ChevronRight, Eye, Pencil, RefreshCw, Trash2, X } from 'lucide-react';
import type { FeedInfo, FeedCheck, FeedCheckPage } from '../../api/open';
import { Button } from '../Button';
import { IconButton } from '../IconButton';
import { PanelEmpty } from '../PanelPrimitives';
import { PanelSelect } from '../PanelSelect';
import { INTERVALS, redactedUrl, feedConfigSummary, feedKindLabel } from './feedConfig';
import { FeedStatus, FeedKindIcon } from './feedPresentation';
import { formatWhen, runDeltaText } from './feedFormatting';
import type { BusyFeed } from './feedState';
import type { FeedPanelActions } from './useFeedsPanelState';
import '../FeedsPanel.css';

interface FeedListProps {
  actions: FeedPanelActions;
  armedDelete: number | null;
  busy: BusyFeed;
  expanded: Record<number, boolean>;
  items: FeedInfo[] | null;
  runPages: Record<number, FeedCheckPage>;
  runs: Record<number, FeedCheck[]>;
}

export function FeedList({ actions, armedDelete, busy, expanded, items, runPages, runs }: FeedListProps) {
  if (items === null) return <PanelEmpty>loading...</PanelEmpty>;
  if (items.length === 0) {
    return (
      <PanelEmpty testId="sources-empty">No feeds yet.</PanelEmpty>
    );
  }

  return (
    <ul className="panel-list" data-testid="source-list">
      {items.map((s) => {
        const status = s.lastStatus ?? 'never';
        const isOpen = !!expanded[s.id];
        const page = runPages[s.id];
        const RunChevron = isOpen ? ChevronDown : ChevronRight;
        return (
          <li
            key={s.id}
            className={`panel-card source-item${s.enabled ? '' : ' disabled'}`}
            data-testid={`source-item-${s.id}`}
          >
            <div className="control-row">
              <span className="control-row panel-item-meta"><FeedKindIcon kind={s.kind} /></span>
              <span className="panel-item-title panel-item-main" title={s.name}>{s.name}</span>
              <FeedStatus value={status} testId={`source-status-${s.id}`} />
            </div>
            <div className="control-row panel-item-meta">
              <strong data-testid={`source-kind-${s.id}`}>{feedKindLabel(s.kind)}</strong>
              <span>{feedConfigSummary(s)}</span>
            </div>
            {s.url && (
              <div className="panel-item-meta" title={redactedUrl(s.url) ?? ''}>
                {redactedUrl(s.url)}
              </div>
            )}

            <div className="control-row source-controls">
              <label className="control-row" title="Enable scheduled polling">
                <input
                  type="checkbox"
                  checked={s.enabled}
                  aria-label={`Enable ${s.name}`}
                  onChange={(e) => { void actions.toggleEnabled(s.id, e.target.checked); }}
                />
              </label>
              <PanelSelect
                aria-label={`Cadence for ${s.name}`}
                data-testid={`source-sched-${s.id}`}
                value={s.schedule ?? ''}
                onChange={(e) => { void actions.changeSchedule(s.id, e.target.value); }}
              >
                {INTERVALS.map((i) => (
                  <option key={i.value} value={i.value}>{i.label}</option>
                ))}
                {s.schedule && !INTERVALS.some((i) => i.value === s.schedule) && (
                  <option value={s.schedule}>{s.schedule}</option>
                )}
              </PanelSelect>
              <button
                type="button"
                className="icon-btn"
                title="Poll now"
                aria-label={`Poll ${s.name}`}
                data-testid={`source-fetch-${s.id}`}
                disabled={busy === s.id}
                onClick={() => { void actions.fetchNow(s.id); }}
              >
                <RefreshCw size={13} className={busy === s.id ? 'spin' : undefined} />
              </button>
              <IconButton
                icon={Pencil}
                title="Edit feed"
                label={`Edit ${s.name}`}
                data-testid={`source-edit-${s.id}`}
                onClick={() => actions.startEdit(s)}
              />
              <IconButton
                icon={Eye}
                title="Feed details"
                label={`Feed details for ${s.name}`}
                data-testid={`source-health-${s.id}`}
                onClick={() => { void actions.openHealth(s.id); }}
              />
              <button
                type="button"
                className={`icon-btn${armedDelete === s.id ? ' danger' : ''}`}
                title={armedDelete === s.id ? 'Click again to confirm' : 'Delete feed'}
                aria-label={
                  armedDelete === s.id
                    ? `Confirm delete ${s.name}`
                    : `Delete ${s.name}`
                }
                data-testid={`source-delete-${s.id}`}
                onClick={() => { void actions.remove(s.id); }}
              >
                {armedDelete === s.id ? <X size={13} /> : <Trash2 size={13} />}
              </button>
            </div>

            <div className="control-row panel-item-meta">
              {s.newRowsTotal > 0 && <>{s.newRowsTotal.toLocaleString()} rows / </>}
              <Button
                type="button"
                variant="text"
                aria-expanded={isOpen}
                aria-controls={`source-poll-history-${s.id}`}
                onClick={() => { void actions.toggleRuns(s.id); }}
              >
                <RunChevron size={14} aria-hidden /> Poll history
              </Button>
            </div>

            {isOpen && (
              <ul id={`source-poll-history-${s.id}`} className="panel-list" data-testid={`source-runs-${s.id}`}>
                {runs[s.id] === undefined ? (
                  <li className="panel-empty">loading...</li>
                ) : runs[s.id].length === 0 ? (
                  <li className="panel-empty">No polls yet.</li>
                ) : (
                  <>
                    {runs[s.id].map((r) => (
                      <li key={r.id} className="panel-list">
                        <div className="control-row panel-item-meta">
                          <FeedStatus value={r.status} />
                          <span>{runDeltaText(r)}</span>
                          <span>{formatWhen(r.startedAt)}</span>
                        </div>
                        {r.warningCount > 0 && (
                          <div className="panel-notice" data-tone="warning">
                            {r.warningCount} warning{r.warningCount === 1 ? '' : 's'}
                          </div>
                        )}
                        {r.error && <div className="panel-item-meta source-run-error">{r.error}</div>}
                      </li>
                    ))}
                    {page?.hasMore && (
                      <li className="panel-empty" data-testid={`source-runs-page-note-${s.id}`}>
                        {page.total.toLocaleString()} total polls / older polls not loaded
                      </li>
                    )}
                  </>
                )}
              </ul>
            )}
          </li>
        );
      })}
    </ul>
  );
}
