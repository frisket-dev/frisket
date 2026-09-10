// @vitest-environment jsdom
//
// ModelPullProgress's injectable fetchPull/cancelPull props let the org settings
// card use the same live-progress component pointed at
// /api/org/models/pulls/{id} instead of the workspace-tier
// /api/providers/models/pulls/{id} route. Kept as a SEPARATE file from
// ModelPullProgress.test.tsx (which pins the default-fetcher behavior) so
// this file only ever asserts the injection seam.

import '@testing-library/jest-dom/vitest';
import { act, cleanup, screen, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi, type Mock } from 'vitest';

import { ModelPullProgress } from '../../src/components/ModelPullProgress';
import { getModelPull, cancelModelPull } from '../../src/api/open';
import type { ModelPullDto } from '../../src/api/types';



vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    getModelPull: vi.fn(),
    cancelModelPull: vi.fn(),
  };
});

function pull(overrides: Partial<ModelPullDto>): ModelPullDto {
  return {
    id: 7,
    model: 'qwen3:8b',
    status: 'running',
    phase: 'downloading',
    total_bytes: null,
    completed_bytes: null,
    error: null,
    resolved_digest: null,
    resolved_size: null,
    created_at: '2026-07-16T00:00:00Z',
    started_at: '2026-07-16T00:00:01Z',
    finished_at: null,
    cancel_requested: false,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.useRealTimers();
});

describe('ModelPullProgress — injectable fetchPull/cancelPull', () => {
  it('polls the injected fetchPull instead of the default getModelPull', async () => {
    vi.useFakeTimers();
    const fetchPull = vi.fn().mockResolvedValue(
      pull({ status: 'done', total_bytes: 1000, completed_bytes: 1000 }),
    );
    const onDone = vi.fn();

    render(<ModelPullProgress pull={pull({})} onDone={onDone} fetchPull={fetchPull} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(fetchPull).toHaveBeenCalledWith(7);
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('model-pull-done')).toBeInTheDocument();
    // The default local-tier fetcher must never be reached when an injected
    // fetchPull is supplied.
    expect(getModelPull).not.toHaveBeenCalled();
  });

  it('ignores a typed ModelPullDto from injected cancelPull while completing cancellation', async () => {
    let resolveCancel!: (value: ModelPullDto) => void;
    const cancelPull = vi.fn(
      (): Promise<ModelPullDto> =>
        new Promise((resolve) => {
          resolveCancel = resolve;
        }),
    );
    render(<ModelPullProgress pull={pull({})} cancelPull={cancelPull} />);

    const cancelBtn = screen.getByTestId('model-pull-cancel');
    await userEvent.click(cancelBtn);

    expect(cancelPull).toHaveBeenCalledWith(7);
    expect(cancelModelPull).not.toHaveBeenCalled();
    expect(cancelBtn).toBeDisabled();

    await act(async () => {
      resolveCancel(
        pull({
          status: 'cancelled',
          finished_at: '2026-07-16T00:00:02Z',
          cancel_requested: true,
        }),
      );
    });

    // The shared component accepts truthful response-bearing cancel ports but
    // deliberately ignores their values: org cancellation still returns void,
    // and the next poll remains the status owner.
    expect(screen.queryByTestId('model-pull-cancelled')).not.toBeInTheDocument();
    expect(cancelBtn).toHaveTextContent('Cancelling…');
    expect(cancelBtn).toBeDisabled();
  });

  it('falls back to the default getModelPull/cancelModelPull when no fetchers are injected (backward compatibility)', async () => {
    vi.useFakeTimers();
    (getModelPull as unknown as Mock).mockResolvedValue(pull({ status: 'done' }));
    render(<ModelPullProgress pull={pull({})} />);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    expect(getModelPull).toHaveBeenCalledWith(7);
  });
});
