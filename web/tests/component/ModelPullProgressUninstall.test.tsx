// @vitest-environment jsdom
//
// P6: ModelPullProgress offers an Uninstall action for a completed ARTIFACT
// pull with a backend-declared remove capability and renders the distinct
// `uninstalled` terminal state. Daemon-managed Ollama pulls have no remove
// capability and show no uninstall button.

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    getModelPull: vi.fn(),
    cancelModelPull: vi.fn(),
    uninstallArtifact: vi.fn(),
  };
});

import { ModelPullProgress } from '../../src/components/ModelPullProgress';
import { uninstallArtifact } from '../../src/api/open';
import type { ModelPullDto } from '../../src/api/types';



function donePull(overrides: Partial<ModelPullDto>): ModelPullDto {
  return {
    schemaVersion: 'frisket.model_pull.v4',
    id: 5,
    display_name: 'OPUS-MT English to Spanish',
    operation_kind: 'artifact',
    capabilities: { cancel: false, retry: false, remove: (overrides.status ?? 'done') === 'done' },
    endpoint_id: null,
    endpoint_origin: null,
    initiated_by: null,
    model: 'opus-mt:en-es',
    status: 'done',
    phase: 'done',
    total_bytes: 100,
    completed_bytes: 100,
    error: null,
    resolved_digest: 'sha256:x',
    resolved_size: 100,
    created_at: '2026-07-17T00:00:00Z',
    started_at: '2026-07-17T00:00:01Z',
    finished_at: '2026-07-17T00:00:02Z',
    cancel_requested: false,
    artifact: { kind: 'ct2_pair', source_url: 'https://e', license: 'CC-BY-4.0', manifest_version: '2026.07.1' },
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('ModelPullProgress uninstall', () => {
  it('offers uninstall for a done artifact pull and flips to uninstalled', async () => {
    (uninstallArtifact as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
      donePull({ status: 'uninstalled' }),
    );
    render(<ModelPullProgress pull={donePull({})} />);
    const button = screen.getByTestId('model-pull-uninstall');
    await act(async () => {
      fireEvent.click(button);
    });
    expect(uninstallArtifact).toHaveBeenCalledWith('opus-mt:en-es');
    expect(screen.getByTestId('model-pull-uninstalled')).toBeInTheDocument();
  });

  it('shows no uninstall button for an ollama pull (artifact == null)', () => {
    render(
      <ModelPullProgress pull={donePull({ model: 'llama3:8b', display_name: 'Llama 3 8B', operation_kind: 'local_model', artifact: null, capabilities: { cancel: false, retry: false, remove: false } })} />,
    );
    expect(screen.queryByTestId('model-pull-uninstall')).toBeNull();
  });
});
