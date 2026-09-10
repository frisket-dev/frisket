// @vitest-environment jsdom

import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import type { FormEvent } from 'react';

import type { EmbeddingApiPort } from '../../src/api/ports';
import type { SheetMeta } from '../../src/api/types';
import { useEmbeddingsPanelController } from '../../src/components/embeddings/useEmbeddingsPanelController';

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

const sheet = {
  id: '7',
  name: 'People',
  rowCount: 1,
  columns: [{ id: '11', name: 'bio', type: 'text' }],
  citedColumnIds: [],
  annotatedTextColumnIds: [],
} as SheetMeta;

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
});
