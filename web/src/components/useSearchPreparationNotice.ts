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
  stage: 'download' | 'planning' | 'approval' | 'prepare' | 'index' | 'failed' | 'complete';
  tone: 'active' | 'warning' | 'error' | 'complete';
  title: string;
  detail: string;
  batch: SearchPreparationBatch | null;
  indexing: SearchIndexingProjection | null;
  pull: ModelPullDto | null;
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

function preparationProgress(batch: SearchPreparationBatch): number | null {
  if (batch.jobs.length === 0) return null;
  const complete = batch.jobs.filter((job) => job.status === 'done').length;
  return Math.round((complete / batch.jobs.length) * 100);
}

function indexingDetail(indexing: SearchIndexingProjection): string {
  switch (indexing.status) {
    case 'pending':
      return 'Preparing the search indexing job';
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
): SearchPreparationNotice | null {
  const activeBatch = snapshot.batches.find((batch) => ACTIVE_BATCH_STATUSES.has(batch.status)) ?? null;
  if (activeBatch?.status === 'needs_consent') {
    return {
      stage: 'approval',
      tone: 'warning',
      title: 'Search preparation needs approval',
      detail: `${remainingFileCount(activeBatch).toLocaleString()} files are waiting for approval`,
      batch: activeBatch,
      indexing: null,
      pull: null,
      progress: null,
      details: activeBatch.models.filter((model) => model.required && !model.ready).map((model) => model.display_name),
    };
  }

  const enginePulls = snapshot.pulls.filter(({ pull }) => pull.operation_kind === 'engine_setup');
  const activePull = latestPull(enginePulls.filter(({ pull }) => (
    pull.status === 'pending' || pull.status === 'running'
  )));
  if (activePull) {
    const { pull } = activePull;
    return {
      stage: 'download',
      tone: 'active',
      title: 'Downloading model',
      detail: pull.phase ? `Downloading ${pull.display_name} · ${pull.phase}` : `Downloading ${pull.display_name}`,
      batch: null,
      indexing: null,
      pull,
      progress: pullProgress(pull),
      details: [`${activePull.scope === 'organization' ? 'Organization' : 'Workspace'} model setup`],
    };
  }

  if (snapshot.indexing.status === 'failed') {
    return {
      stage: 'failed',
      tone: 'error',
      title: 'Search indexing needs attention',
      detail: indexingDetail(snapshot.indexing),
      batch: null,
      indexing: snapshot.indexing,
      pull: null,
      progress: null,
      details: snapshot.indexing.job_id == null
        ? []
        : [`Index job ${snapshot.indexing.job_id.toLocaleString()}`],
    };
  }

  if (snapshot.indexing.status !== 'idle') {
    return {
      stage: 'index',
      tone: 'active',
      title: 'Preparing search',
      detail: indexingDetail(snapshot.indexing),
      batch: null,
      indexing: snapshot.indexing,
      pull: null,
      progress: null,
      details: snapshot.indexing.job_id == null
        ? ['The project has durable indexing work waiting to be queued.']
        : [`Job ${snapshot.indexing.job_id.toLocaleString()}`],
    };
  }

  if (activeBatch) {
    if (activeBatch.status === 'planning') {
      return {
        stage: 'planning',
        tone: 'active',
        title: 'Preparing search',
        detail: 'Checking imported files for search',
        batch: activeBatch,
        indexing: null,
        pull: null,
        progress: null,
        details: [`Preparation ${activeBatch.id}`],
      };
    }
    return {
      stage: 'prepare',
      tone: 'active',
      title: 'Preparing search',
      detail: preparationDetail(activeBatch),
      batch: activeBatch,
      indexing: null,
      pull: null,
      progress: preparationProgress(activeBatch),
      details: activeBatch.jobs.map((job) => `Job ${job.job_id.toLocaleString()} · ${job.status}`),
    };
  }

  const newestEnginePull = latestPull(enginePulls);
  if (newestEnginePull && (newestEnginePull.pull.status === 'failed' || newestEnginePull.pull.status === 'cancelled')) {
    const { pull } = newestEnginePull;
    const errorMessage = pull.error?.message || (pull.status === 'cancelled' ? 'Model download was cancelled.' : 'Model setup failed.');
    return {
      stage: 'failed',
      tone: 'error',
      title: 'Model setup needs attention',
      detail: errorMessage,
      batch: null,
      indexing: null,
      pull,
      progress: null,
      details: [pull.error?.code, pull.display_name].filter((item): item is string => Boolean(item)),
    };
  }

  const failedBatch = snapshot.batches.find((batch) => batch.status === 'failed') ?? null;
  if (failedBatch) {
    return {
      stage: 'failed',
      tone: 'error',
      title: 'Search preparation needs attention',
      detail: failedBatch.errors[0] || 'Search preparation failed.',
      batch: failedBatch,
      indexing: null,
      pull: null,
      progress: null,
      details: failedBatch.errors.length > 0 ? failedBatch.errors : [`Preparation ${failedBatch.id}`],
    };
  }

  if (completion) {
    return {
      stage: 'complete',
      tone: 'complete',
      title: completion,
      detail: 'Search indexing finished.',
      batch: null,
      indexing: null,
      pull: null,
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
  const requests: Promise<{ scope: ScopedPull['scope']; pulls: ModelPullDto[] }>[] = [
    listModelPulls().then(({ pulls }) => ({ scope: 'workspace', pulls })),
  ];
  if (team) {
    requests.push(orgListModelPulls().then(({ pulls }) => ({ scope: 'organization', pulls })));
  }
  const settled = await Promise.allSettled(requests);
  return settled.flatMap((result, index) => {
    if (result.status === 'fulfilled') {
      return result.value.pulls.map((pull) => ({ pull, scope: result.value.scope }));
    }
    const scope: ScopedPull['scope'] = index === 0 ? 'workspace' : 'organization';
    return previous.filter((item) => item.scope === scope);
  });
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
): SearchPreparationBatch[] {
  const active = batches.filter((batch) => ACTIVE_BATCH_STATUSES.has(batch.status));
  if (active.length > 0) {
    observedActiveBatchIds.clear();
    for (const batch of active) observedActiveBatchIds.add(batch.id);
    return active;
  }
  return batches.filter((batch) => (
    batch.status === 'failed' && observedActiveBatchIds.has(batch.id)
  )).slice(0, 1);
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
    snapshotRef.current = { scopeKey, snapshot: next };
    setSnapshotState({ scopeKey, snapshot: next });
    if (completedSince(previous, next)) {
      setCompletionState({ scopeKey, message: 'Search index ready' });
      if (completionTimer.current !== null) clearTimeout(completionTimer.current);
      completionTimer.current = setTimeout(() => {
        setCompletionState((current) => current.scopeKey === scopeKey
          ? { ...current, message: null }
          : current);
      }, 8_000);
    }
  }, [projectId, scopeKey, team]);

  useEffect(() => {
    requestGeneration.current += 1;
    snapshotRef.current = null;
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
  const notice = deriveSearchPreparationNotice(snapshot, completion);
  usePoll(refresh, {
    active: true,
    intervalMs: notice?.tone === 'active' ? 2_000 : 5_000,
    guardOverlap: true,
  });
  return notice;
}
