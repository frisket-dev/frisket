import { useState } from 'react';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useSelector } from '../bind/useSelector';
import { ActivityProgressCard, ProgressRow } from './ActivityProgress';

/** Uses the ordinary jobs polling owner; no separate timer or progress store. */
export function FeedProgressNotice() {
  const { job, chromePreferences: { projectId } } = useWorkspaceStores();
  const feed = useSelector(job.store, (state) => state.actionJobs.jobs.find((candidate) => candidate.kind === 'source.poll'));
  const [dismissed, setDismissed] = useState<string | null>(null);
  const active = feed?.status === 'queued' || feed?.status === 'running';
  const identity = feed ? `${projectId}:${feed.jobId}` : null;
  const [observed, setObserved] = useState<string | null>(null);
  if (active && observed !== identity) setObserved(identity);
  if (!feed || dismissed === identity || (!active && observed !== identity)) return null;
  const failed = feed.status === 'failed';
  const state = active ? 'active' : failed ? 'error' : feed.status === 'cancelled' ? 'warning' : 'complete';
  const facts = feed.resultSummary ?? {};
  const completed = typeof facts.completed_items === 'number' ? facts.completed_items : null;
  const total = typeof facts.total_items === 'number' ? facts.total_items : null;
  const status = feed.status === 'queued' ? 'Waiting to start'
    : active ? completed == null ? 'Reading feed…' : `${completed.toLocaleString()}${total == null ? '' : ` of ${total.toLocaleString()}`} items found`
    : failed ? feed.error ?? 'Feed import failed' : feed.status === 'cancelled' ? 'Import cancelled' : 'Feed imported';
  return <ActivityProgressCard state={state} title={active ? 'Importing feed' : 'Feed import'}
    detail={typeof facts.source_name === 'string' ? facts.source_name : undefined}
    onDismiss={() => setDismissed(`${projectId}:${feed.jobId}`)} testId="feed-progress-notice">
    <ProgressRow label="Feed items" completed={completed} total={total} state={state}
      status={status} ariaLabel="Feed import progress" />
  </ActivityProgressCard>;
}
