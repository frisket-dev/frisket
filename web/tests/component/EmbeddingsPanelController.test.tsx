// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { FormEvent } from 'react';

afterEach(cleanup);

import { ApiError, ConfirmationRequiredError, type EmbeddingIndexSummary } from '../../src/api/open';
import type { EmbeddingApiPort } from '../../src/api/ports';
import { useSelector } from '../../src/bind/useSelector';
import { useJobsHandle } from '../../src/bind/useJobsHandle';
import { CostGateModal } from '../../src/components/CostGateModal';
import { EmbeddingsPanel } from '../../src/components/EmbeddingsPanel';
import { useEmbeddingsPanelController } from '../../src/components/embeddings/useEmbeddingsPanelController';
import { sheetMeta } from '../support/actionFormFixtures';
import { createWorkspaceTestHarness, projectApiWith } from '../support/workspaceTestHarness';

function deferred<T>(): {
  promise: Promise<T>;
  resolve(value: T): void;
} {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

const sheet = sheetMeta([{ id: '11', name: 'bio', type: 'text' }], {
  id: '7',
  name: 'People',
  rowCount: 1,
});

function apiPort(overrides: Record<string, unknown> = {}): EmbeddingApiPort {
  return {
    embeddingIndexes: vi.fn().mockResolvedValue([]),
    embeddingProviderCatalog: vi.fn().mockResolvedValue([{
      providerId: 'local',
      providerKind: 'local',
      modelId: 'tiny',
      label: 'Tiny local model',
      modalities: ['text'],
      dimensions: [384],
      local: true,
      available: true,
      disabledReason: null,
      egress: 'local',
      recommended: true,
      sizeGb: null,
      maxInputTokens: null,
      modalityCompatible: true,
      dimensionDiscoveryRequired: false,
    }]),
    createEmbeddingIndex: vi.fn().mockResolvedValue('idx-new'),
    refreshEmbeddingIndex: vi.fn().mockResolvedValue({ jobId: null, status: 'completed' }),
    getActionJob: vi.fn().mockResolvedValue({ status: 'done' }),
    ...overrides,
  } as unknown as EmbeddingApiPort;
}

describe('useEmbeddingsPanelController project retirement', () => {
  it('does not reload or refresh an old index when create resolves after unmount', async () => {
    const create = deferred<string>();
    const port = apiPort({
      createEmbeddingIndex: vi.fn(() => create.promise),
    });
    const { result, unmount } = renderHook(() =>
      useEmbeddingsPanelController({ apiPort: port, sheet }),
    );

    await waitFor(() => expect(port.embeddingIndexes).toHaveBeenCalledTimes(1));
    await act(async () => {
      await result.current.openCreate();
    });
    // The dialog now owns the authoritative selector readiness signal. The
    // controller fixture supplies the matching selection it receives from it.
    act(() => result.current.selectModel('local', 'tiny'));
    expect(result.current.createReady).toBe(true);

    const event = { preventDefault: vi.fn() } as unknown as FormEvent<HTMLFormElement>;
    let submission!: Promise<void>;
    act(() => {
      submission = result.current.submitCreate(event);
    });
    await waitFor(() => expect(port.createEmbeddingIndex).toHaveBeenCalledTimes(1));

    unmount();
    create.resolve('idx-from-retired-project');
    await submission;

    expect(port.embeddingIndexes).toHaveBeenCalledTimes(1);
    expect(port.refreshEmbeddingIndex).not.toHaveBeenCalled();
    expect(port.getActionJob).not.toHaveBeenCalled();
  });

  it('does not poll or reload when refresh resolves after unmount', async () => {
    const refresh = deferred<{ jobId: number | null; status: string }>();
    const port = apiPort({
      refreshEmbeddingIndex: vi.fn(() => refresh.promise),
    });
    const { result, unmount } = renderHook(() =>
      useEmbeddingsPanelController({ apiPort: port, sheet }),
    );

    await waitFor(() => expect(port.embeddingIndexes).toHaveBeenCalledTimes(1));
    let refreshing!: Promise<void>;
    act(() => {
      refreshing = result.current.refresh('idx-from-retired-project', 'full');
    });
    await waitFor(() => expect(port.refreshEmbeddingIndex).toHaveBeenCalledTimes(1));

    unmount();
    refresh.resolve({ jobId: 42, status: 'queued' });
    await refreshing;

    expect(port.getActionJob).not.toHaveBeenCalled();
    expect(port.embeddingIndexes).toHaveBeenCalledTimes(1);
  });
});

describe('useEmbeddingsPanelController current scope', () => {
  it('still polls a queued refresh and reloads after it reaches terminal state', async () => {
    const port = apiPort({
      refreshEmbeddingIndex: vi.fn().mockResolvedValue({ jobId: 42, status: 'queued' }),
      getActionJob: vi.fn().mockResolvedValue({ status: 'done' }),
    });
    const { result } = renderHook(() =>
      useEmbeddingsPanelController({ apiPort: port, sheet }),
    );

    await waitFor(() => expect(port.embeddingIndexes).toHaveBeenCalledTimes(1));
    await act(async () => {
      await result.current.refresh('idx-current', 'incremental');
    });

    expect(port.getActionJob).toHaveBeenCalledWith(42);
    expect(port.embeddingIndexes).toHaveBeenCalledTimes(2);
    expect(result.current.busyId).toBeNull();
  });

  it('still closes, reloads, and starts a full refresh after create', async () => {
    const port = apiPort();
    const { result } = renderHook(() =>
      useEmbeddingsPanelController({ apiPort: port, sheet }),
    );

    await waitFor(() => expect(port.embeddingIndexes).toHaveBeenCalledTimes(1));
    await act(async () => {
      await result.current.openCreate();
    });
    act(() => result.current.selectModel('local', 'tiny'));
    const event = { preventDefault: vi.fn() } as unknown as FormEvent<HTMLFormElement>;
    await act(async () => {
      await result.current.submitCreate(event);
    });

    await waitFor(() => {
      expect(port.refreshEmbeddingIndex).toHaveBeenCalledWith('idx-new', 'full');
      expect(port.embeddingIndexes).toHaveBeenCalledTimes(3);
    });
    expect(result.current.adding).toBe(false);
    expect(result.current.creating).toBe(false);
  });

  it('prompts via requestCostConfirmation and echoes the token on confirm', async () => {
    const estimate = {
      cost: 0.25,
      billed_cost: 250_000,
      policy_id: 'frisket.pricing.identity.v1',
      rows: 200,
      promise_set_hash: 'b'.repeat(64),
    };
    const refreshEmbeddingIndex = vi.fn()
      .mockRejectedValueOnce(new ConfirmationRequiredError(estimate, 'Confirm remote embedding refresh.'))
      .mockResolvedValueOnce({ jobId: 77, status: 'queued' });
    const requestCostConfirmation = vi.fn().mockResolvedValue(true);
    const port = apiPort({
      refreshEmbeddingIndex,
      getActionJob: vi.fn().mockResolvedValue({ status: 'done' }),
    });
    const { result } = renderHook(() =>
      useEmbeddingsPanelController({ apiPort: port, sheet, requestCostConfirmation }),
    );

    await waitFor(() => expect(port.embeddingIndexes).toHaveBeenCalledTimes(1));
    await act(async () => {
      await result.current.refresh('idx-remote', 'incremental');
    });

    expect(requestCostConfirmation).toHaveBeenCalledWith(estimate, 'Confirm remote embedding refresh.');
    expect(refreshEmbeddingIndex).toHaveBeenNthCalledWith(1, 'idx-remote', 'incremental');
    expect(refreshEmbeddingIndex).toHaveBeenNthCalledWith(2, 'idx-remote', 'incremental', {
      confirmation: 'b'.repeat(64),
    });
    expect(port.getActionJob).toHaveBeenCalledWith(77);
    expect(result.current.error).toBeNull();
    expect(result.current.busyId).toBeNull();
  });

  it('aborts cleanly without error when the user cancels the cost confirmation modal', async () => {
    const estimate = {
      cost: null,
      billed_cost: null,
      policy_id: 'frisket.pricing.identity.v1',
      rows: 10,
      promise_set_hash: 'c'.repeat(64),
    };
    const refreshEmbeddingIndex = vi.fn()
      .mockRejectedValueOnce(new ConfirmationRequiredError(estimate, 'Estimated cost is unknown.'));
    const requestCostConfirmation = vi.fn().mockResolvedValue(false);
    const port = apiPort({ refreshEmbeddingIndex });
    const { result } = renderHook(() =>
      useEmbeddingsPanelController({ apiPort: port, sheet, requestCostConfirmation }),
    );

    await waitFor(() => expect(port.embeddingIndexes).toHaveBeenCalledTimes(1));
    await act(async () => {
      await result.current.refresh('idx-unknown', 'full');
    });

    expect(requestCostConfirmation).toHaveBeenCalledTimes(1);
    expect(refreshEmbeddingIndex).toHaveBeenCalledTimes(1);
    expect(port.getActionJob).not.toHaveBeenCalled();
    expect(result.current.error).toBeNull();
    expect(result.current.busyId).toBeNull();
  });

  it('does not confirm when unmounted while the cost gate modal is open', async () => {
    const gate = deferred<boolean>();
    const estimate = {
      cost: 0.5,
      billed_cost: 500_000,
      policy_id: 'frisket.pricing.identity.v1',
      rows: 100,
      promise_set_hash: 'd'.repeat(64),
    };
    const refreshEmbeddingIndex = vi.fn()
      .mockRejectedValueOnce(new ConfirmationRequiredError(estimate, 'Confirm refresh.'));
    const requestCostConfirmation = vi.fn(() => gate.promise);
    const port = apiPort({ refreshEmbeddingIndex });
    const { result, unmount } = renderHook(() =>
      useEmbeddingsPanelController({ apiPort: port, sheet, requestCostConfirmation }),
    );

    await waitFor(() => expect(port.embeddingIndexes).toHaveBeenCalledTimes(1));
    let pending!: Promise<void>;
    act(() => {
      pending = result.current.refresh('idx-remote', 'full');
    });
    await waitFor(() => expect(requestCostConfirmation).toHaveBeenCalledTimes(1));

    unmount();
    gate.resolve(true);
    await pending;

    expect(refreshEmbeddingIndex).toHaveBeenCalledTimes(1);
  });

  it('surfaces Cloud unpriceable_model refusal as an error without opening the modal', async () => {
    const refreshEmbeddingIndex = vi.fn().mockRejectedValueOnce(
      new ApiError(
        400,
        'Remote embedding refresh with an unknown price is not allowed on Cloud.',
        'unpriceable_model',
      ),
    );
    const requestCostConfirmation = vi.fn();
    const port = apiPort({ refreshEmbeddingIndex });
    const { result } = renderHook(() =>
      useEmbeddingsPanelController({ apiPort: port, sheet, requestCostConfirmation }),
    );

    await waitFor(() => expect(port.embeddingIndexes).toHaveBeenCalledTimes(1));
    await act(async () => {
      await result.current.refresh('idx-cloud-unknown', 'full');
    });

    expect(requestCostConfirmation).not.toHaveBeenCalled();
    expect(result.current.error).toBe(
      'Remote embedding refresh with an unknown price is not allowed on Cloud.',
    );
    expect(result.current.busyId).toBeNull();
  });
});

function remoteIndexSummary(overrides: Partial<EmbeddingIndexSummary> = {}): EmbeddingIndexSummary {
  return {
    indexId: 'idx-remote-1',
    name: 'Remote headlines',
    sheetId: 7,
    modality: 'text',
    providerId: 'openai',
    modelId: 'text-embedding-3-small',
    sourceColumns: ['bio'],
    status: 'ready',
    totalItems: 12,
    readyItems: 0,
    staleItems: 0,
    missingItems: 12,
    errorItems: 0,
    refreshNeeded: true,
    lastRefreshedAt: null,
    remote: true,
    allowRemote: true,
    allowRemoteAutomaticRefresh: false,
    providerKind: 'platform_api',
    maxCostUsdPerRefresh: null,
    maintenanceMode: 'manual',
    schedule: null,
    policyNeedsRepair: false,
    freshness: {
      reason: 'never_refreshed',
      current: 0,
      missing: 12,
      stale: 0,
      error: 0,
      total: 12,
      lastRefreshJobId: null,
      lastRefreshReceiptId: null,
      pendingRefreshJobId: null,
    },
    spaceId: 'emb_space_1',
    dimension: 1536,
    distanceMetric: 'cosine',
    ...overrides,
  };
}

function CostGateHost() {
  const jobs = useJobsHandle();
  const costGate = useSelector(jobs.store, (state) => state.costGate);
  if (!costGate) return null;
  return (
    <CostGateModal
      estimate={costGate.estimate}
      message={costGate.message}
      onConfirm={() => jobs.confirmCostGate()}
      onCancel={() => jobs.cancelCostGate()}
    />
  );
}

describe('EmbeddingsPanel manual remote refresh confirmation modal', () => {
  it('opens the shared CostGateModal with the estimate and runs on confirmation', async () => {
    const estimate = {
      cost: 0.12,
      billed_cost: 120_000,
      policy_id: 'frisket.pricing.identity.v1',
      rows: 12,
      avg_input_tokens: 400,
      promise_set_hash: 'e'.repeat(64),
    };
    const refreshEmbeddingIndex = vi.fn()
      .mockRejectedValueOnce(new ConfirmationRequiredError(
        estimate,
        'Confirm remote embedding refresh for openai/text-embedding-3-small before running.',
      ))
      .mockResolvedValueOnce({ jobId: 88, status: 'queued' });
    const port = apiPort({
      embeddingIndexes: vi.fn().mockResolvedValue([remoteIndexSummary()]),
      listViews: vi.fn().mockResolvedValue([]),
      refreshEmbeddingIndex,
      getActionJob: vi.fn().mockResolvedValue({ status: 'done' }),
    });
    const harness = createWorkspaceTestHarness({
      projectId: 'embed-modal-confirm',
      api: { projectApi: projectApiWith({}) },
    });
    harness.render(
      <>
        <EmbeddingsPanel
          apiPort={port}
          projectId="embed-modal-confirm"
          sheet={sheet}
          onSelectSheet={vi.fn()}
          onOpenView={vi.fn()}
          selectedRowIds={[]}
          onFindSimilar={vi.fn()}
        />
        <CostGateHost />
      </>,
    );

    const refreshButton = await screen.findByTestId('embedding-refresh-idx-remote-1');
    fireEvent.click(refreshButton);

    const modal = await screen.findByTestId('cost-gate-modal');
    expect(modal).toBeInTheDocument();
    expect(screen.getByTestId('cost-gate-estimate')).toHaveTextContent('$0.12');
    expect(screen.getByTestId('cost-gate-estimate')).toHaveTextContent('12 rows');
    expect(refreshEmbeddingIndex).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByTestId('cost-gate-confirm'));

    await waitFor(() => {
      expect(refreshEmbeddingIndex).toHaveBeenCalledWith('idx-remote-1', 'incremental', {
        confirmation: 'e'.repeat(64),
      });
    });
    await waitFor(() => expect(screen.queryByTestId('cost-gate-modal')).not.toBeInTheDocument());
    expect(screen.queryByTestId('embeddings-error')).not.toBeInTheDocument();
  });

  it('shows UNKNOWN in CostGateModal for an unpriced local model and aborts on cancel', async () => {
    const estimate = {
      cost: null,
      billed_cost: null,
      policy_id: 'frisket.pricing.identity.v1',
      rows: 12,
      promise_set_hash: 'f'.repeat(64),
    };
    const refreshEmbeddingIndex = vi.fn().mockRejectedValueOnce(
      new ConfirmationRequiredError(
        estimate,
        'Estimated cost is unknown for openrouter/custom — confirm to refresh this remote embedding index.',
      ),
    );
    const port = apiPort({
      embeddingIndexes: vi.fn().mockResolvedValue([
        remoteIndexSummary({ providerId: 'openrouter', modelId: 'custom' }),
      ]),
      listViews: vi.fn().mockResolvedValue([]),
      refreshEmbeddingIndex,
    });
    const harness = createWorkspaceTestHarness({
      projectId: 'embed-modal-cancel',
      api: { projectApi: projectApiWith({}) },
    });
    harness.render(
      <>
        <EmbeddingsPanel
          apiPort={port}
          projectId="embed-modal-cancel"
          sheet={sheet}
          onSelectSheet={vi.fn()}
          onOpenView={vi.fn()}
          selectedRowIds={[]}
          onFindSimilar={vi.fn()}
        />
        <CostGateHost />
      </>,
    );

    fireEvent.click(await screen.findByTestId('embedding-refresh-idx-remote-1'));

    expect(await screen.findByTestId('cost-gate-modal')).toBeInTheDocument();
    expect(screen.getByTestId('cost-gate-estimate')).toHaveTextContent('UNKNOWN');

    fireEvent.click(screen.getByTestId('cost-gate-cancel'));

    await waitFor(() => expect(screen.queryByTestId('cost-gate-modal')).not.toBeInTheDocument());
    expect(refreshEmbeddingIndex).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('embeddings-error')).not.toBeInTheDocument();
  });
});
