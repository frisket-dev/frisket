import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReviewRun } from '../../api/types';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';

const PAGE_SIZE = 50;

function mergeRuns(current: ReviewRun[], incoming: ReviewRun[]): ReviewRun[] {
  const byId = new Map(current.map((run) => [run.runId, run]));
  for (const run of incoming) byId.set(run.runId, run);
  return [...byId.values()];
}

/** The picker reads bounded run summaries, never the run's result rows. */
export function useReviewRuns(initialRunId?: string) {
  const { projectApi } = useWorkspaceStores();
  const versions = useRef(new Map<string, number>());
  const nextVersion = useCallback((id: string) => {
    const version = (versions.current.get(id) ?? 0) + 1;
    versions.current.set(id, version);
    return version;
  }, []);
  const [runs, setRuns] = useState<ReviewRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState(initialRunId ?? '');
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [statusBusy, setStatusBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    void Promise.all([
      projectApi.listReviewRuns(0, PAGE_SIZE),
      initialRunId ? projectApi.listReviewRuns(0, 1, { runId: initialRunId }) : null,
    ]).then(([page, pinned]) => {
      if (!active) return;
      const available = mergeRuns(page.runs, pinned?.runs ?? []);
      setRuns(available);
      setNextOffset(page.nextOffset);
      setSelectedRunId(pinned?.runs[0]?.runId
        ?? available.find((run) => run.reviewStatus === 'open')?.runId
        ?? available[0]?.runId ?? '');
    }).catch(() => {
      if (active) setError('Could not load runs for review. Close Review and try again.');
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [initialRunId, projectApi]);

  const loadMore = useCallback(async () => {
    if (nextOffset === null || loadingMore) return;
    setLoadingMore(true);
    setError(null);
    try {
      const page = await projectApi.listReviewRuns(nextOffset, PAGE_SIZE);
      setRuns((current) => mergeRuns(current, page.runs));
      setNextOffset(page.nextOffset);
    } catch {
      setError('Could not load more runs. Please try again.');
    } finally { setLoadingMore(false); }
  }, [loadingMore, nextOffset, projectApi]);

  const refreshRun = useCallback(async (runId: string) => {
    const version = nextVersion(runId);
    try {
      const page = await projectApi.listReviewRuns(0, 1, { runId });
      if (versions.current.get(runId) === version) setRuns((current) => mergeRuns(current, page.runs));
    } catch {
      if (versions.current.get(runId) !== version) return;
      setError('Your decision was saved, but the review counts could not be refreshed.');
    }
  }, [projectApi, nextVersion]);

  const setRunStatus = useCallback(async (status: 'open' | 'complete') => {
    if (!selectedRunId || statusBusy) return;
    nextVersion(selectedRunId);
    setStatusBusy(true);
    setError(null);
    try {
      const result = await projectApi.setReviewRunStatus(selectedRunId, status);
      nextVersion(selectedRunId);
      setRuns((current) => current.map((run) => run.runId === selectedRunId
        ? { ...run, reviewStatus: result.status, reviewCompletedAt: result.reviewCompletedAt }
        : run));
    } catch {
      setError('Could not change the review status. Please try again.');
    } finally { setStatusBusy(false); }
  }, [projectApi, selectedRunId, statusBusy, nextVersion]);

  return {
    runs, selectedRunId, setSelectedRunId, loading, loadingMore, statusBusy,
    error, loadMore, hasMore: nextOffset !== null, refreshRun, setRunStatus,
  };
}
