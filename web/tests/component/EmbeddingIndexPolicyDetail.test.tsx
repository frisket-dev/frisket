// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { EmbeddingIndexSummary } from '../../src/api/open';
import type { EmbeddingApiPort } from '../../src/api/ports';
import { IndexPolicyDetail } from '../../src/components/embeddings/IndexPolicyDetail';



afterEach(cleanup);

function index(): EmbeddingIndexSummary {
  return {
    indexId: 'idx_1', name: 'Index', sheetId: 3, modality: 'text', providerId: 'openai',
    modelId: 'text-embedding', sourceColumns: ['headline'], status: 'ready', totalItems: 1,
    readyItems: 1, staleItems: 0, missingItems: 0, errorItems: 0, refreshNeeded: false,
    lastRefreshedAt: null, remote: true, providerKind: 'platform_api', allowRemote: true,
    allowRemoteAutomaticRefresh: true, maxCostUsdPerRefresh: null, maintenanceMode: 'manual',
    schedule: null, policyNeedsRepair: false, spaceId: 'space_1', dimension: 1536,
    distanceMetric: 'cosine',
    freshness: {
      reason: 'fresh', current: 1, missing: 0, stale: 0, error: 0, total: 1,
      lastRefreshJobId: null, lastRefreshReceiptId: null, pendingRefreshJobId: null,
    },
  };
}

describe('EmbeddingIndexPolicyDetail cost cap', () => {
  it.each(['0', '-1', 'Infinity'])('does not submit invalid cap %s', async (value) => {
    const updateEmbeddingIndexPolicy = vi.fn().mockResolvedValue(undefined);
    render(
      <IndexPolicyDetail
        apiPort={{ updateEmbeddingIndexPolicy } as unknown as EmbeddingApiPort}
        index={index()}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByTestId('embedding-policy-max-cost-idx_1'), {
      target: { value },
    });
    fireEvent.click(screen.getByTestId('embedding-policy-save-idx_1'));

    expect(await screen.findByText('Max $/refresh must be a positive finite number or blank.'))
      .toBeInTheDocument();
    expect(updateEmbeddingIndexPolicy).not.toHaveBeenCalled();
  });

  it('preserves blank cost cap as null', async () => {
    const updateEmbeddingIndexPolicy = vi.fn().mockResolvedValue(undefined);
    render(
      <IndexPolicyDetail
        apiPort={{ updateEmbeddingIndexPolicy } as unknown as EmbeddingApiPort}
        index={index()}
        onChanged={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId('embedding-policy-save-idx_1'));

    expect(updateEmbeddingIndexPolicy).toHaveBeenCalledWith(expect.objectContaining({
      providerPolicy: expect.objectContaining({ maxCostUsdPerRefresh: null }),
    }));
  });
});
