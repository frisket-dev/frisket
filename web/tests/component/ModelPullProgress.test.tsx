// @vitest-environment jsdom
//
// ModelPullProgress is the live-progress view for a single in-app model pull.
// It renders the model name,
// phase, a byte progress bar (indeterminate while total_bytes is unknown —
// Ollama doesn't know a layer's size until it starts streaming it), a Cancel
// button while pending/running, and terminal error/done states. Polls
// GET /providers/models/pulls/{id} every ~1s via the shared usePoll hook
// while the pull is still active, and calls onDone once when the poll
// observes status flip to 'done' so the parent (LocalServerGuidance /
// Settings) can refresh its catalog.

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi, type Mock } from 'vitest';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    getModelPull: vi.fn(),
    cancelModelPull: vi.fn(),
  };
});

import { ModelPullProgress } from '../../src/components/ModelPullProgress';
import { cancelModelPull, getModelPull } from '../../src/api/open';
import type { ModelPullDto } from '../../src/api/types';



function pull(overrides: Partial<ModelPullDto>): ModelPullDto {
  return {
    id: 1,
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

describe('ModelPullProgress', () => {
  it('renders model, phase, and a determinate bar from completed/total bytes', () => {
    render(
      <ModelPullProgress
        pull={pull({ total_bytes: 1000, completed_bytes: 250 })}
      />,
    );
    expect(screen.getByText('qwen3:8b')).toBeInTheDocument();
    expect(screen.getByText('downloading')).toBeInTheDocument();
    const bar = screen.getByRole('progressbar');
    expect(bar).toHaveAttribute('aria-valuenow', '25');
  });

  it('renders an indeterminate bar when total_bytes is null', () => {
    render(<ModelPullProgress pull={pull({ total_bytes: null, completed_bytes: null })} />);
    const bar = screen.getByRole('progressbar');
    expect(bar).not.toHaveAttribute('aria-valuenow');
    expect(bar.className).toMatch(/indeterminate/);
  });

  it('Cancel button calls cancelModelPull and then disables itself once cancel_requested', async () => {
    (cancelModelPull as unknown as Mock).mockResolvedValue(undefined);
    render(<ModelPullProgress pull={pull({})} />);
    const cancelBtn = screen.getByTestId('model-pull-cancel');
    expect(cancelBtn).toBeEnabled();
    await userEvent.click(cancelBtn);
    expect(cancelModelPull).toHaveBeenCalledWith(1);
    expect(cancelBtn).toBeDisabled();
  });

  it('does not show Cancel once cancel_requested is already true', () => {
    render(<ModelPullProgress pull={pull({ cancel_requested: true })} />);
    expect(screen.getByTestId('model-pull-cancel')).toBeDisabled();
  });

  it('renders the error state with code + message, no Cancel button', () => {
    render(
      <ModelPullProgress
        pull={pull({
          status: 'failed',
          error: { code: 'pull_failed', message: 'connection reset' },
        })}
      />,
    );
    const err = screen.getByTestId('model-pull-error');
    expect(err.textContent).toMatch(/pull_failed/);
    expect(err.textContent).toMatch(/connection reset/);
    expect(screen.queryByTestId('model-pull-cancel')).not.toBeInTheDocument();
  });

  it('renders the done state', () => {
    render(
      <ModelPullProgress
        pull={pull({ status: 'done', resolved_size: 4_000_000_000 })}
      />,
    );
    expect(screen.getByTestId('model-pull-done')).toBeInTheDocument();
    expect(screen.queryByTestId('model-pull-cancel')).not.toBeInTheDocument();
  });

  it('polls getModelPull ~1s while active and calls onDone when status flips to done', async () => {
    vi.useFakeTimers();
    (getModelPull as unknown as Mock).mockResolvedValue(
      pull({ status: 'done', total_bytes: 1000, completed_bytes: 1000 }),
    );
    const onDone = vi.fn();
    render(<ModelPullProgress pull={pull({})} onDone={onDone} />);

    expect(getModelPull).not.toHaveBeenCalled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(getModelPull).toHaveBeenCalledWith(1);
    expect(onDone).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('model-pull-done')).toBeInTheDocument();

    // Once done, the loop must not keep polling.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(getModelPull).toHaveBeenCalledTimes(1);
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it('does not poll once the pull is already terminal on mount', async () => {
    vi.useFakeTimers();
    render(<ModelPullProgress pull={pull({ status: 'cancelled' })} />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(getModelPull).not.toHaveBeenCalled();
  });

  it('does not latch in "Cancelling…"/spinner once a poll reports the backend-confirmed cancellation, even for a pull cancelled while still queued', async () => {
    // Backend agents now flip a cancel-while-QUEUED pull straight to
    // 'cancelled' (no more eternal 'Cancelling…' from a pull stuck in
    // 'pending'). Drive the same sequence the UI actually sees: Cancel is
    // clicked while still pending/active, then the next poll observes the
    // terminal 'cancelled' status.
    vi.useFakeTimers();
    (cancelModelPull as unknown as Mock).mockResolvedValue(undefined);
    (getModelPull as unknown as Mock).mockResolvedValue(
      pull({ status: 'cancelled', cancel_requested: true }),
    );
    render(<ModelPullProgress pull={pull({ status: 'pending' })} />);

    const cancelBtn = screen.getByTestId('model-pull-cancel');
    await act(async () => {
      fireEvent.click(cancelBtn);
      // Let the mocked cancelModelPull() promise resolve.
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByTestId('model-pull-cancel')).toHaveTextContent('Cancelling…');
    expect(screen.getByRole('progressbar')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });

    // Terminal state won: no more spinner/progress bar, no latched
    // 'Cancelling…' button, and polling has stopped.
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
    expect(screen.queryByTestId('model-pull-cancel')).not.toBeInTheDocument();
    expect(screen.getByTestId('model-pull-cancelled')).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(getModelPull).toHaveBeenCalledTimes(1);
  });

  it('renders a canonical backend error code (endpoint_changed) verbatim', () => {
    render(
      <ModelPullProgress
        pull={pull({
          status: 'failed',
          error: {
            code: 'endpoint_changed',
            message: 'The local endpoint changed mid-pull (correlation id corr-abc123); please retry.',
          },
        })}
      />,
    );
    const err = screen.getByTestId('model-pull-error');
    expect(err.textContent).toMatch(/endpoint_changed/);
    expect(err.textContent).toMatch(
      /The local endpoint changed mid-pull \(correlation id corr-abc123\); please retry\./,
    );
  });
});
