// @vitest-environment jsdom
//
// In-app model pull affordance inside the empty_native LocalServerGuidance
// state. Kept in a SEPARATE file from LocalServerGuidance.test.tsx so the existing
// spec — which pins the pull_enabled-absent behavior — never needs editing.
//
// Contract (frisket.model_pull.v3):
//   - pull_enabled absent/false: render exactly today's copyable command,
//     no Download button.
//   - pull_enabled true: a primary "Download qwen3:0.6b" button ABOVE the
//     copyable command; clicking opens an inline Confirm/Cancel with a
//     several-GB download warning; Confirm calls the generic artifact pull
//     and swaps the confirm for inline ModelPullProgress.
//   - a 409 pull_busy error carries the already-running pull as
//     `details.active` — show ITS progress instead of a bare error.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi, type Mock } from 'vitest';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    startArtifactPull: vi.fn(),
    getModelPull: vi.fn(),
    cancelModelPull: vi.fn(),
  };
});

import { LocalServerGuidance } from '../../src/components/LocalServerGuidance';
import { ApiError, startArtifactPull } from '../../src/api/open';
import type { LocalHttpEndpointEntry, ModelPullDto } from '../../src/api/types';



afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function entry(overrides: Partial<LocalHttpEndpointEntry>): LocalHttpEndpointEntry {
  return {
    endpoint_id: 'local-a1b2c3d4e5f6',
    label: 'Local server',
    kind: 'local_http',
    read_only: false,
    models: [],
    origin: 'http://localhost:11434',
    authority: 'instance',
    source: 'stored',
    reachable: true,
    detail: null,
    auth_status: 'ok',
    protocol: 'ollama_native',
    installed_models: [],
    token_configured: false,
    provisioning_token_configured: false,
    edge_auth: false,
    pull_enabled: false,
    ...overrides,
  };
}

function activePull(overrides: Partial<ModelPullDto> = {}): ModelPullDto {
  return {
    schemaVersion: 'frisket.model_pull.v3',
    id: 42,
    model: 'ollama/@local-a1b2c3d4e5f6/qwen3:8b',
    status: 'running',
    phase: 'downloading',
    total_bytes: 1000,
    completed_bytes: 400,
    error: null,
    resolved_digest: null,
    resolved_size: null,
    created_at: '2026-07-16T00:00:00Z',
    started_at: '2026-07-16T00:00:01Z',
    finished_at: null,
    cancel_requested: false,
    endpoint_id: 'local-a1b2c3d4e5f6',
    endpoint_origin: 'http://localhost:11434',
    initiated_by: null,
    artifact: null,
    ...overrides,
  };
}

describe('LocalServerGuidance download affordance', () => {
  it('no Download button when pull_enabled is absent', () => {
    render(
      <LocalServerGuidance entry={entry({})} variant="compact" onRecheck={vi.fn()} />,
    );
    expect(screen.queryByTestId('guidance-download')).not.toBeInTheDocument();
    expect(screen.getByTestId('guidance-command')).toHaveTextContent(/ollama pull/);
  });

  it('no Download button when pull_enabled is explicitly false', () => {
    render(
      <LocalServerGuidance
        entry={entry({ pull_enabled: false })}
        variant="compact"
        onRecheck={vi.fn()}
      />,
    );
    expect(screen.queryByTestId('guidance-download')).not.toBeInTheDocument();
  });

  it('pull_enabled true: Download button -> confirm -> start -> inline progress; onRecheck fires on completion', async () => {
    const onRecheck = vi.fn();
    const startedPull = activePull({ id: 7, status: 'pending', completed_bytes: null, total_bytes: null });
    (startArtifactPull as unknown as Mock).mockResolvedValue({
      pull: startedPull,
      deduplicated: false,
    });

    render(
      <LocalServerGuidance
        entry={entry({ pull_enabled: true })}
        variant="compact"
        onRecheck={onRecheck}
      />,
    );

    // The copyable command remains as the fallback path.
    expect(screen.getByTestId('guidance-command')).toHaveTextContent(/ollama pull/);

    const downloadBtn = screen.getByTestId('guidance-download');
    expect(downloadBtn).toHaveTextContent(/qwen3:0\.6b/);
    await userEvent.click(downloadBtn);

    const confirm = screen.getByTestId('guidance-download-confirm');
    expect(confirm.textContent).toMatch(/about 523 MB/i);
    expect(confirm.textContent).toMatch(/machine running your local server/i);

    await userEvent.click(screen.getByTestId('guidance-download-confirm-yes'));

    expect(startArtifactPull).toHaveBeenCalledWith(
      'ollama/@local-a1b2c3d4e5f6/qwen3:0.6b',
    );
    expect(screen.queryByTestId('guidance-download-confirm')).not.toBeInTheDocument();
    expect(screen.getByTestId('model-pull-progress')).toBeInTheDocument();
  });

  it('Cancel on the confirm step returns to the plain Download button', async () => {
    render(
      <LocalServerGuidance
        entry={entry({ pull_enabled: true })}
        variant="compact"
        onRecheck={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByTestId('guidance-download'));
    await userEvent.click(screen.getByTestId('guidance-download-cancel'));
    expect(screen.queryByTestId('guidance-download-confirm')).not.toBeInTheDocument();
    expect(screen.getByTestId('guidance-download')).toBeInTheDocument();
    expect(startArtifactPull).not.toHaveBeenCalled();
  });

  it('409 pull_busy: shows the ALREADY-RUNNING pull\'s progress instead of a bare error', async () => {
    const busy = activePull({ id: 99, phase: 'verifying', completed_bytes: 900, total_bytes: 1000 });
    (startArtifactPull as unknown as Mock).mockRejectedValue(
      new ApiError(409, 'a pull is already in progress', 'pull_busy', { active: busy }),
    );

    render(
      <LocalServerGuidance
        entry={entry({ pull_enabled: true })}
        variant="compact"
        onRecheck={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByTestId('guidance-download'));
    await userEvent.click(screen.getByTestId('guidance-download-confirm-yes'));

    const progress = await screen.findByTestId('model-pull-progress');
    expect(progress).toHaveTextContent('verifying');
    expect(screen.queryByTestId('guidance-download-confirm')).not.toBeInTheDocument();
  });
});
