// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import type { EmbeddingApiPort } from '../../src/api/ports';
import type { EmbeddingIndexAnalysisInput, EmbeddingIndexSummary } from '../../src/api/types';
import { EmbeddingIndexCard } from '../../src/components/embeddings/EmbeddingIndexCard';
import { OpenLensContext } from '../../src/components/embeddings/openLensContext';

afterEach(cleanup);

const index: EmbeddingIndexSummary = {
  indexId: 'idx_1', name: 'People', sheetId: 3, modality: 'text', providerId: 'local',
  modelId: 'test-model', sourceColumns: ['bio'], status: 'ready', totalItems: 10,
  readyItems: 10, staleItems: 0, missingItems: 0, errorItems: 0, refreshNeeded: false,
  lastRefreshedAt: null, remote: false, providerKind: 'local', allowRemote: false,
  allowRemoteAutomaticRefresh: false, maxCostUsdPerRefresh: null, maintenanceMode: 'manual',
  schedule: null, policyNeedsRepair: false, spaceId: 'space_1', dimension: 384,
  distanceMetric: 'cosine',
  freshness: { reason: 'fresh', current: 10, missing: 0, stale: 0, error: 0, total: 10,
    lastRefreshJobId: null, lastRefreshReceiptId: null, pendingRefreshJobId: null },
};

function mountCard() {
  const destinations = new Set<string>();
  const runAnalysis = vi.fn(async (request: EmbeddingIndexAnalysisInput) => {
    if (destinations.has(request.sheet_name)) throw new Error(`A sheet named '${request.sheet_name}' already exists`);
    destinations.add(request.sheet_name);
    return { sheetId: destinations.size + 10 };
  });
  const onSelectSheet = vi.fn();
  const apiPort = {
    runEmbeddingIndexAnalysis: runAnalysis, listLenses: vi.fn().mockResolvedValue([]),
  } as unknown as EmbeddingApiPort;
  render(<OpenLensContext.Provider value={vi.fn()}>
    <EmbeddingIndexCard apiPort={apiPort} index={index} busy={false}
      onRefresh={vi.fn()} onChanged={vi.fn()} onSelectSheet={onSelectSheet} />
  </OpenLensContext.Provider>);
  return { runAnalysis, onSelectSheet };
}

const cases = [
  { kind: 'pca', label: 'PCA sheet name', name: 'PCA of People', actionId: 'embedding.index_project',
    params: { index_id: 'idx_1', method: 'pca', dimensions: 2 } },
  { kind: 'cluster', label: 'Clustering sheet name', name: 'Clusters of People', actionId: 'embedding.index_cluster',
    params: { index_id: 'idx_1', method: 'kmeans', k: 5, seed: 0 } },
];

it.each(cases)('lets $kind run again with an explicit destination after a name collision', async ({ kind, label, name, actionId, params }) => {
  const { runAnalysis, onSelectSheet } = mountCard();
  const destination = screen.getByLabelText(label);
  const run = screen.getByTestId(`embedding-run-${kind}-idx_1`);
  expect(destination).toHaveValue(name);
  fireEvent.click(run);
  await waitFor(() => expect(onSelectSheet).toHaveBeenCalledWith('11'));
  expect(runAnalysis).toHaveBeenLastCalledWith({ action_id: actionId, sheet_name: name, params });

  // Changing k alone must not silently invent a new destination name.
  if (kind === 'cluster') fireEvent.change(screen.getByTestId('embedding-cluster-k-idx_1'), { target: { value: '3' } });
  await waitFor(() => expect(run).toBeEnabled());
  fireEvent.click(run);
  expect(await screen.findByTestId('embedding-analysis-error-idx_1')).toHaveTextContent('already exists');
  expect(destination).toHaveValue(name);
  expect(onSelectSheet).toHaveBeenCalledTimes(1);

  fireEvent.change(destination, { target: { value: `  ${name} revised  ` } });
  fireEvent.click(run);
  await waitFor(() => expect(onSelectSheet).toHaveBeenLastCalledWith('12'));
  expect(runAnalysis).toHaveBeenLastCalledWith({ action_id: actionId, sheet_name: `${name} revised`,
    params: kind === 'cluster' ? { ...params, k: 3 } : params });
  expect(screen.queryByTestId('embedding-analysis-error-idx_1')).not.toBeInTheDocument();
});

it.each(cases)('blocks $kind with a blank destination without disabling the other analysis', ({ kind, label }) => {
  const { runAnalysis } = mountCard();
  fireEvent.change(screen.getByLabelText(label), { target: { value: '   ' } });
  const run = screen.getByTestId(`embedding-run-${kind}-idx_1`);
  expect(run).toBeDisabled();
  expect(screen.getByTestId(`embedding-run-${kind === 'pca' ? 'cluster' : 'pca'}-idx_1`)).toBeEnabled();
  fireEvent.click(run);
  expect(runAnalysis).not.toHaveBeenCalled();
});
