import { useCallback, useEffect, useRef, useState } from 'react';
import {
  listModelPulls,
  orgListModelPulls,
  type ModelPullDto,
} from '../api/open';
import {
  listSearchPreparations,
  type SearchPreparationBatch,
} from '../api/searchPreparation';
import { usePoll } from '../hooks/usePoll';

interface LegacySearchIndexingProjection {
  status: 'idle' | 'pending' | 'queued' | 'running' | 'failed';
  job_id: number | null;
  error: string | null;
}

type SearchPreparationListResult = Awaited<ReturnType<typeof listSearchPreparations>>;
type GeneratedSearchIndexingProjection = SearchPreparationListResult extends { indexing: infer Indexing }
  ? Indexing
  : never;
export type SearchIndexingProjection = [GeneratedSearchIndexingProjection] extends [never]
  ? LegacySearchIndexingProjection
  : GeneratedSearchIndexingProjection;

interface ScopedPull {
  pull: ModelPullDto;
  scope: 'workspace' | 'organization';
}

export interface SearchPreparationSnapshot {
  batches: SearchPreparationBatch[];
  indexing: SearchIndexingProjection;
  pulls: ScopedPull[];
}

export type SearchPreparationNotice = {
  activityKey: string;
  reviewBatchId: string | null;
  stage: 'download' | 'planning' | 'approval' | 'prepare' | 'index' | 'failed' | 'complete';
  tone: 'active' | 'warning' | 'error' | 'complete';
  title: string;
  detail: string;
  progress: number | null;
  details: string[];
};

const IDLE_INDEXING: SearchIndexingProjection = {
  status: 'idle',
  job_id: null,
  error: null,
};

const EMPTY_SNAPSHOT: SearchPreparationSnapshot = {
  batches: [],
  indexing: IDLE_INDEXING,
  pulls: [],
};

const ACTIVE_BATCH_STATUSES = new Set<SearchPreparationBatch['status']>([
  'planning',
  'needs_consent',
  'preparing',
]);

function latestPull(pulls: ScopedPull[]): ScopedPull | null {
  return [...pulls].sort((left, right) => (
    Date.parse(right.pull.created_at) - Date.parse(left.pull.created_at)
      || right.pull.id - left.pull.id
  ))[0] ?? null;
}

function pullProgress(pull: ModelPullDto): number | null {
  if (pull.total_bytes == null || pull.total_bytes <= 0 || pull.completed_bytes == null) return null;
  return Math.max(0, Math.min(100, Math.round((pull.completed_bytes / pull.total_bytes) * 100)));
}

function remainingFileCount(batch: SearchPreparationBatch): number {
  return batch.groups
    .filter((group) => group.status !== 'complete' && group.status !== 'not_prepared')
    .reduce((sum, group) => sum + group.count, 0);
}

function preparationDetail(batch: SearchPreparationBatch): string {
  const group = batch.groups.find((candidate) => (
    candidate.status === 'running'
      || candidate.status === 'queued'
      || candidate.status === 'waiting_for_download'
  ));
  const count = remainingFileCount(batch);
  const countLabel = `${count.toLocaleString()} ${count === 1 ? 'file' : 'files'}`;
  if (!group) return `Preparing ${countLabel}`;
  if (group.status === 'waiting_for_download') return `Waiting for a required model · ${countLabel}`;
  switch (group.kind) {
    case 'native_pdf':
      return `Reading PDF text · ${countLabel}`;
    case 'ocr':
      return `Reading scanned PDFs · ${countLabel}`;
    case 'transcribe':
      return `Transcribing media · ${countLabel}`;
  }
}

function progressFailure(batch: SearchPreparationBatch): {
  completed: number;
  details: string[];
  failed: number;
  running: boolean;
  total: number;
} | null {
  const progress = batch.jobs
    .map((job) => job.progress)
    .filter((item) => item !== null && item !== undefined);
  if (progress.length === 0) return null;
  const summary = progress.reduce((current, item) => ({
    completed: current.completed + item.completed,
    failed: current.failed + item.failed,
    total: current.total + item.total,
    running: current.running || ['queued', 'running', 'stalled', 'orphaned'].includes(item.status),
    details: [
      ...current.details,
      ...(item.row_errors?.groups.map((group) => (
        group.count > 1 ? `${group.message} (${group.count.toLocaleString()} files)` : group.message
      )) ?? []),
    ],
  }), { completed: 0, details: [] as string[], failed: 0, running: false, total: 0 });
  return summary.failed > 0 ? summary : null;
}

function indexingDetail(indexing: SearchIndexingProjection): string {
  switch (indexing.status) {
    case 'pending':
      return 'Waiting to index';
    case 'queued':
      return 'Search indexing queued';
    case 'running':
      return 'Indexing search content';
    case 'failed':
      return indexing.error || 'Search indexing failed.';
    case 'idle':
      return 'Search index is up to date';
  }
}

export function deriveSearchPreparationNotice(
  snapshot: SearchPreparationSnapshot,
  completion: string | null = null,
  activityIdentity: string | null = null,
): SearchPreparationNotice | null {
  const activeBatch = snapshot.batches.find((batch) => ACTIVE_BATCH_STATUSES.has(batch.status)) ?? null;
  const activePreparationBatch = activeBatch && (
    activeBatch.status === 'planning'
    || activeBatch.groups.some((group) => (
      group.status !== 'complete' && group.status !== 'not_prepared'
    ))
  ) ? activeBatch : null;
  const failedBatch = snapshot.batches.find((batch) => batch.status === 'failed') ?? null;
  const progressFailureBatch = snapshot.batches
    .map((batch) => ({ batch, failure: progressFailure(batch) }))
    .find((item) => item.failure !== null) ?? null;
  if (progressFailureBatch?.failure) {
    const { batch, failure } = progressFailureBatch;
    const fileNoun = failure.total === 1 ? 'file' : 'files';
    const details = [...failure.details];
    if (failure.running) details.push(preparationDetail(batch));
    if (snapshot.indexing.status !== 'idle') details.push(indexingDetail(snapshot.indexing));
    return {
      activityKey: activityIdentity?.startsWith(`preparation:${batch.id}`)
        ? activityIdentity
        : `preparation:${batch.id}:failed`,
      reviewBatchId: batch.id,
      stage: 'failed',
      tone: 'error',
      title: 'Search preparation needs attention',
      detail: `${failure.failed.toLocaleString()} of ${failure.total.toLocaleString()} ${fileNoun} failed · ${failure.completed.toLocaleString()}/${failure.total.toLocaleString()} processed`,
      progress: failure.total > 0
        ? Math.round((failure.completed / failure.total) * 100)
        : null,
      details,
    };
  }
  if (failedBatch) {
    const unavailableModels = failedBatch.models.filter((model) => (
      model.required && !model.ready && !model.can_setup
    ));
    const remedies = unavailableModels.map((model) => (
      `${model.display_name}: ${model.remediation || 'Install its local runtime, then prepare these files again.'}`
    ));
    const failureDetails = failedBatch.errors.length > 0
      ? failedBatch.errors
      : [`Preparation ${failedBatch.id}`];
    return {
      activityKey: activityIdentity?.startsWith(`preparation:${failedBatch.id}`)
        ? activityIdentity
        : `preparation:${failedBatch.id}:failed`,
      reviewBatchId: failedBatch.id,
      stage: 'failed',
      tone: 'error',
      title: unavailableModels.length > 0 ? 'Some files need local setup' : 'Search preparation needs attention',
      detail: failedBatch.errors[0] || remedies.join(' ') || 'Search preparation failed.',
      progress: null,
      details: snapshot.indexing.status === 'idle'
        ? failureDetails
        : [...failureDetails, indexingDetail(snapshot.indexing)],
    };
  }
  if (activeBatch?.status === 'needs_consent') {
    return {
      activityKey: `preparation:${activeBatch.id}`,
      reviewBatchId: activeBatch.id,
      stage: 'approval',
      tone: 'warning',
      title: 'Search preparation needs approval',
      detail: `${remainingFileCount(activeBatch).toLocaleString()} files are waiting for approval`,
      progress: null,
      details: activeBatch.models.filter((model) => model.required && !model.ready).map((model) => model.display_name),
    };
  }

  const enginePulls = snapshot.pulls.filter(({ pull }) => pull.operation_kind === 'engine_setup');
  const activePull = latestPull(enginePulls.filter(({ pull }) => (
    pull.status === 'pending' || pull.status === 'running'
  )));
  const newestEnginePull = latestPull(enginePulls);
  if (newestEnginePull && (newestEnginePull.pull.status === 'failed' || newestEnginePull.pull.status === 'cancelled')) {
    const { pull } = newestEnginePull;
    const errorMessage = pull.error?.message || (pull.status === 'cancelled' ? 'Model download was cancelled.' : 'Model setup failed.');
    return {
      activityKey: activeBatch
        ? activityIdentity?.startsWith(`preparation:${activeBatch.id}`)
          ? activityIdentity
          : `preparation:${activeBatch.id}:failed`
        : `model-pull:${pull.id}:failed`,
      reviewBatchId: activeBatch?.id ?? null,
      stage: 'failed',
      tone: 'error',
      title: 'Model setup needs attention',
      detail: errorMessage,
      progress: null,
      details: [pull.error?.code, pull.display_name].filter((item): item is string => Boolean(item)),
    };
  }

  if (snapshot.indexing.status === 'failed') {
    return {
      activityKey: activeBatch
        ? activityIdentity?.startsWith(`preparation:${activeBatch.id}`)
          ? activityIdentity
          : `preparation:${activeBatch.id}:failed`
        : activityIdentity?.endsWith(':failed')
          ? activityIdentity
          : `index:${snapshot.indexing.job_id ?? 'pending'}:failed`,
      reviewBatchId: activeBatch?.id ?? null,
      stage: 'failed',
      tone: 'error',
      title: 'Search indexing needs attention',
      detail: indexingDetail(snapshot.indexing),
      progress: null,
      details: snapshot.indexing.job_id == null
        ? []
        : [`Index job ${snapshot.indexing.job_id.toLocaleString()}`],
    };
  }

  if (activePull) {
    const { pull } = activePull;
    return {
      activityKey: activeBatch
        ? `preparation:${activeBatch.id}`
        : `model-pull:${pull.id}`,
      reviewBatchId: activeBatch?.id ?? null,
      stage: 'download',
      tone: 'active',
      title: 'Downloading model',
      detail: pull.phase ? `Downloading ${pull.display_name} · ${pull.phase}` : `Downloading ${pull.display_name}`,
      progress: pullProgress(pull),
      details: [`${activePull.scope === 'organization' ? 'Organization' : 'Workspace'} model setup`],
    };
  }

  if (activePreparationBatch) {
    if (activePreparationBatch.status === 'planning') {
      return {
        activityKey: `preparation:${activePreparationBatch.id}`,
        reviewBatchId: activePreparationBatch.id,
        stage: 'planning',
        tone: 'active',
        title: 'Preparing search',
        detail: 'Checking imported files for search',
        progress: null,
        details: [`Preparation ${activePreparationBatch.id}`],
      };
    }
    return {
      activityKey: `preparation:${activePreparationBatch.id}`,
      reviewBatchId: activePreparationBatch.id,
      stage: 'prepare',
      tone: 'active',
      title: 'Preparing search',
      detail: preparationDetail(activePreparationBatch),
      progress: null,
      details: activePreparationBatch.jobs.map((job) => `Job ${job.job_id.toLocaleString()} · ${job.status}`),
    };
  }

  if (snapshot.indexing.status !== 'idle') {
    return {
      activityKey: activeBatch
        ? `preparation:${activeBatch.id}`
        : activityIdentity ?? `index:${snapshot.indexing.job_id ?? 'pending'}`,
      reviewBatchId: activeBatch?.id ?? null,
      stage: 'index',
      tone: 'active',
      title: 'Preparing search',
      detail: indexingDetail(snapshot.indexing),
      progress: null,
      details: snapshot.indexing.job_id == null
        ? []
        : [`Job ${snapshot.indexing.job_id.toLocaleString()}`],
    };
  }

  if (completion) {
    return {
      activityKey: activityIdentity ?? 'search-completion',
      reviewBatchId: null,
      stage: 'complete',
      tone: 'complete',
      title: completion,
      detail: 'Search indexing finished.',
      progress: 100,
      details: [],
    };
  }
  return null;
}

function completedSince(
  previous: SearchPreparationSnapshot | null,
  next: SearchPreparationSnapshot,
): boolean {
  if (!previous) return false;
  if (next.batches.some((batch) => ACTIVE_BATCH_STATUSES.has(batch.status) || batch.status === 'failed')) {
    return false;
  }
  if (
    (previous.indexing.status === 'pending'
      || previous.indexing.status === 'queued'
      || previous.indexing.status === 'running')
    && next.indexing.status === 'idle'
  ) return true;
  return false;
}

type PreparationListResult = SearchPreparationListResult & {
  indexing?: SearchIndexingProjection;
};

async function loadPulls(team: boolean, previous: ScopedPull[]): Promise<ScopedPull[]> {
  const scope: ScopedPull['scope'] = team ? 'organization' : 'workspace';
  try {
    const { pulls } = team ? await orgListModelPulls() : await listModelPulls();
    return pulls.map((pull) => ({ pull, scope }));
  } catch {
    return previous.filter((item) => item.scope === scope);
  }
}

function relevantPulls(
  pulls: ScopedPull[],
  batches: SearchPreparationBatch[],
  observedActivePullIds: Set<number>,
): ScopedPull[] {
  const batchPullIds = new Set(batches.flatMap((batch) => (
    batch.models.flatMap((model) => model.pull_id == null ? [] : [model.pull_id])
  )));
  const activePulls = pulls.filter(({ pull }) => (
    pull.operation_kind === 'engine_setup'
      && (pull.status === 'pending' || pull.status === 'running')
  ));
  if (activePulls.length > 0) {
    observedActivePullIds.clear();
    for (const { pull } of activePulls) observedActivePullIds.add(pull.id);
  }
  return pulls.filter(({ pull }) => {
    if (pull.operation_kind !== 'engine_setup') return false;
    if (pull.status === 'pending' || pull.status === 'running') return true;
    return observedActivePullIds.has(pull.id)
      || batchPullIds.has(pull.id);
  });
}

function relevantBatches(
  batches: SearchPreparationBatch[],
  observedActiveBatchIds: Set<string>,
  indexing: SearchIndexingProjection,
): SearchPreparationBatch[] {
  const active = batches.filter((batch) => ACTIVE_BATCH_STATUSES.has(batch.status));
  const previouslyObserved = new Set(observedActiveBatchIds);
  if (active.length > 0) {
    observedActiveBatchIds.clear();
    for (const batch of active) observedActiveBatchIds.add(batch.id);
  }
  const failed = batches.filter((batch, index) => (
    batch.status === 'failed' && (
      previouslyObserved.has(batch.id)
      || batch.models.some((model) => model.required && !model.ready && !model.can_setup)
      || (index === 0 && indexing.status !== 'idle')
    )
  )).slice(0, 1);
  return [...failed, ...active];
}

export function useSearchPreparationNotice(projectId: string, team: boolean): SearchPreparationNotice | null {
  const scopeKey = `${team ? 'team' : 'local'}:${projectId}`;
  const [snapshotState, setSnapshotState] = useState<{
    scopeKey: string;
    snapshot: SearchPreparationSnapshot;
  }>({ scopeKey, snapshot: EMPTY_SNAPSHOT });
  const [completionState, setCompletionState] = useState<{
    scopeKey: string;
    message: string | null;
  }>({ scopeKey, message: null });
  const snapshotRef = useRef<{ scopeKey: string; snapshot: SearchPreparationSnapshot } | null>(null);
  const activityIdentityRef = useRef<{ scopeKey: string; key: string } | null>(null);
  const observedPullsRef = useRef<{ scopeKey: string; ids: Set<number> }>({
    scopeKey: '',
    ids: new Set(),
  });
  const observedBatchesRef = useRef<{ scopeKey: string; ids: Set<string> }>({
    scopeKey: '',
    ids: new Set(),
  });
  const requestGeneration = useRef(0);
  const completionTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    const generation = requestGeneration.current;
    const previous = snapshotRef.current?.scopeKey === scopeKey
      ? snapshotRef.current.snapshot
      : null;
    const [preparationsResult, pullsResult] = await Promise.allSettled([
      listSearchPreparations(projectId),
      loadPulls(team, previous?.pulls ?? []),
    ]);
    if (generation !== requestGeneration.current) return;
    const preparationPayload = preparationsResult.status === 'fulfilled'
      ? preparationsResult.value as PreparationListResult
      : null;
    const batches = relevantBatches(
      preparationPayload?.batches ?? previous?.batches ?? [],
      observedBatchesRef.current.ids,
      preparationPayload?.indexing ?? previous?.indexing ?? IDLE_INDEXING,
    );
    const pulls = pullsResult.status === 'fulfilled' ? pullsResult.value : previous?.pulls ?? [];
    const next: SearchPreparationSnapshot = {
      batches,
      indexing: preparationPayload?.indexing ?? previous?.indexing ?? IDLE_INDEXING,
      pulls: relevantPulls(
        pulls,
        batches,
        observedPullsRef.current.ids,
      ),
    };
    const progressFailedBatch = next.batches.find((batch) => progressFailure(batch) !== null) ?? null;
    const workflowBatch = progressFailedBatch
      ?? next.batches.find((batch) => batch.status === 'failed')
      ?? next.batches.find((batch) => ACTIVE_BATCH_STATUSES.has(batch.status))
      ?? null;
    if (workflowBatch) {
      const baseKey = `preparation:${workflowBatch.id}`;
      const existingKey = activityIdentityRef.current?.scopeKey === scopeKey
        ? activityIdentityRef.current.key
        : null;
      const failed = progressFailedBatch?.id === workflowBatch.id
        || workflowBatch.status === 'failed'
        || next.indexing.status === 'failed'
        || next.pulls.some(({ pull }) => (
          pull.status === 'failed' || pull.status === 'cancelled'
        ));
      activityIdentityRef.current = {
        scopeKey,
        key: failed || existingKey === `${baseKey}:failed`
          ? `${baseKey}:failed`
          : baseKey,
      };
    } else if (next.indexing.status !== 'idle') {
      const currentIdentity = activityIdentityRef.current?.scopeKey === scopeKey
        ? activityIdentityRef.current.key
        : null;
      if (next.indexing.status === 'failed') {
        const baseKey = currentIdentity ?? `index:${next.indexing.job_id ?? 'pending'}`;
        activityIdentityRef.current = {
          scopeKey,
          key: baseKey.endsWith(':failed') ? baseKey : `${baseKey}:failed`,
        };
      } else if (currentIdentity === null || (
        previous?.indexing.status === 'idle'
        && !previous.batches.some((batch) => ACTIVE_BATCH_STATUSES.has(batch.status))
      )) {
        activityIdentityRef.current = {
          scopeKey,
          key: `index:${next.indexing.job_id ?? 'pending'}`,
        };
      }
    }
    snapshotRef.current = { scopeKey, snapshot: next };
    setSnapshotState({ scopeKey, snapshot: next });
    if (completedSince(previous, next)) {
      const completedActivityKey = activityIdentityRef.current?.scopeKey === scopeKey
        ? activityIdentityRef.current.key
        : 'search-completion';
      setCompletionState({
        scopeKey,
        message: 'Search index ready',
      });
      if (completionTimer.current !== null) clearTimeout(completionTimer.current);
      completionTimer.current = setTimeout(() => {
        setCompletionState((current) => current.scopeKey === scopeKey
          ? { ...current, message: null }
          : current);
        const currentSnapshot = snapshotRef.current;
        if (
          activityIdentityRef.current?.scopeKey === scopeKey
          && activityIdentityRef.current.key === completedActivityKey
          && currentSnapshot?.scopeKey === scopeKey
          && currentSnapshot.snapshot.indexing.status === 'idle'
          && !currentSnapshot.snapshot.batches.some((batch) => (
            batch.status === 'failed' || ACTIVE_BATCH_STATUSES.has(batch.status)
          ))
        ) {
          activityIdentityRef.current = null;
        }
      }, 8_000);
    }
  }, [projectId, scopeKey, team]);

  useEffect(() => {
    requestGeneration.current += 1;
    snapshotRef.current = null;
    activityIdentityRef.current = null;
    observedPullsRef.current = { scopeKey, ids: new Set() };
    observedBatchesRef.current = { scopeKey, ids: new Set() };
    void refresh();
    return () => {
      requestGeneration.current += 1;
      if (completionTimer.current !== null) clearTimeout(completionTimer.current);
    };
  }, [refresh, scopeKey]);

  const snapshot = snapshotState.scopeKey === scopeKey ? snapshotState.snapshot : EMPTY_SNAPSHOT;
  const completion = completionState.scopeKey === scopeKey ? completionState.message : null;
  const activityIdentity = activityIdentityRef.current?.scopeKey === scopeKey
    ? activityIdentityRef.current.key
    : null;
  const notice = deriveSearchPreparationNotice(snapshot, completion, activityIdentity);
  usePoll(refresh, {
    active: true,
    intervalMs: notice?.tone === 'active' ? 2_000 : 5_000,
    guardOverlap: true,
  });
  return notice;
}
