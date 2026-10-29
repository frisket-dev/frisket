import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useSelector } from '../bind/useSelector';
import { ModelPullProgress } from './ModelPullProgress';
import { cancelScopedModelPull, readModelPull } from '../api/modelPullOperations';
import { modelPullActive, useModelPullPolling } from '../hooks/useModelPullPolling';
import { useState } from 'react';

export function RuntimeSetupNotice() {
  const { runtimeSetup } = useWorkspaceStores();
  const { pull, scope, canMutate } = useSelector(runtimeSetup.store, (state) => state);
  const [pollError, setPollError] = useState(false);
  useModelPullPolling({
    id: pull?.id ?? null, scopeKey: scope, active: modelPullActive(pull),
    fetchPull: (id, signal) => readModelPull(scope, id, { signal }),
    onProgress: (fresh) => { setPollError(false); runtimeSetup.update(fresh); },
    onError: () => setPollError(true),
    maxIntervalMs: 10_000,
  });
  if (!pull) return null;
  return <ModelPullProgress key={pull.id} pull={pull} poll={false} readOnly={!canMutate}
    externalPollError={pollError}
    onDismiss={modelPullActive(pull) ? undefined : runtimeSetup.dismiss}
    fetchPull={(id) => readModelPull(scope, id)}
    cancelPull={async (id) => { const fresh = await cancelScopedModelPull(scope, id); runtimeSetup.update(fresh); }} />;
}
