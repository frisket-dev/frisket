// @vitest-environment jsdom
//
// Settings additions for in-app model pull, kept in a SEPARATE file from
// WorkspaceAiProvidersSettings.test.tsx. Covers the
// endpoint-scoped download opt-in (environment endpoints are read-only) and
// the active/recent pulls strip.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi, type Mock } from 'vitest';

import { WorkspaceAiProvidersSettings } from '../../src/settings/SettingsSections';
import type { LocalHttpEndpointEntry, LocalProviderCatalog, ModelPullDto } from '../../src/api/types';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    listProviders: vi.fn(),
    updateLocalEndpoint: vi.fn(),
    listModelPulls: vi.fn(),
  };
});

import { listModelPulls, listProviders, updateLocalEndpoint } from '../../src/api/open';



afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function localEndpoint(overrides: Partial<LocalHttpEndpointEntry> = {}): LocalHttpEndpointEntry {
  return {
    endpoint_id: 'local-a1b2c3d4e5f6',
    label: 'Desk Ollama',
    kind: 'local_http',
    read_only: false,
    models: [],
    reachable: true,
    origin: 'http://localhost:11434',
    authority: 'instance',
    source: 'stored',
    detail: null,
    installed_models: [],
    protocol: 'ollama_native',
    auth_status: 'ok',
    token_configured: false,
    provisioning_token_configured: false,
    edge_auth: false,
    pull_enabled: true,
    ...overrides,
  };
}

function catalog(providers: LocalProviderEntry[]): LocalProviderCatalog {
  return { schemaVersion: 'frisket.providers.v1', tier: 'local', providers };
}

function pull(overrides: Partial<ModelPullDto>): ModelPullDto {
  return {
    schemaVersion: 'frisket.model_pull.v3',
    id: 1,
    model: 'ollama/@local-a1b2c3d4e5f6/qwen3:8b',
    status: 'running',
    phase: 'downloading',
    total_bytes: 1000,
    completed_bytes: 500,
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

const OLLAMA_ENABLED = localEndpoint();

describe('WorkspaceAiProvidersSettings — model pull', () => {
  it('toggle reflects pull_enabled and patches the selected endpoint + reloads', async () => {
    (listProviders as unknown as Mock).mockResolvedValue(catalog([OLLAMA_ENABLED]));
    (listModelPulls as unknown as Mock).mockResolvedValue({ pulls: [] });
    (updateLocalEndpoint as unknown as Mock).mockResolvedValue(undefined);

    render(<WorkspaceAiProvidersSettings />);

    const toggle = (await screen.findByRole('checkbox', { name: /allow model downloads/i })) as HTMLInputElement;
    expect(toggle.checked).toBe(true);
    expect(toggle).toBeEnabled();

    (listProviders as unknown as Mock).mockResolvedValue(
      catalog([{ ...OLLAMA_ENABLED, pull_enabled: false }]),
    );
    await userEvent.click(toggle);
    await waitFor(() => expect(updateLocalEndpoint).toHaveBeenCalledWith(
      'local-a1b2c3d4e5f6',
      { pull_enabled: false },
    ));
    await waitFor(() => expect(listProviders).toHaveBeenCalledTimes(2));
  });

  it('env-owned pull setting is read-only and says so', async () => {
    (listProviders as unknown as Mock).mockResolvedValue(
      catalog([
        { ...OLLAMA_ENABLED, pull_enabled: true, source: 'environment', read_only: true },
      ]),
    );
    (listModelPulls as unknown as Mock).mockResolvedValue({ pulls: [] });

    render(<WorkspaceAiProvidersSettings />);

    expect(await screen.findByTestId('local-endpoint-local-a1b2c3d4e5f6')).toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: /allow model downloads/i })).not.toBeInTheDocument();
    expect(updateLocalEndpoint).not.toHaveBeenCalled();
  });

  it('the pull list strip fetches on mount and renders active + recent pulls', async () => {
    (listProviders as unknown as Mock).mockResolvedValue(catalog([OLLAMA_ENABLED]));
    (listModelPulls as unknown as Mock).mockResolvedValue({
      pulls: [
        pull({ id: 5, status: 'running', phase: 'downloading' }),
        pull({ id: 4, status: 'done', resolved_size: 3_000_000_000, finished_at: '2026-07-16T00:05:00Z' }),
        pull({ id: 3, status: 'failed', error: { code: 'pull_failed', message: 'reset' } }),
      ],
    });

    render(<WorkspaceAiProvidersSettings />);

    const list = await screen.findByTestId('model-pull-list');
    expect(list).toBeInTheDocument();
    await waitFor(() => expect(listModelPulls).toHaveBeenCalled());
    // Active pull renders as live progress.
    expect(await screen.findByTestId('model-pull-progress')).toBeInTheDocument();
    // Finished pulls render compactly with model + status + size.
    expect(list.textContent).toMatch(/qwen3:8b/);
    expect(list.textContent).toMatch(/done/);
    expect(list.textContent).toMatch(/failed/);
  });
});
