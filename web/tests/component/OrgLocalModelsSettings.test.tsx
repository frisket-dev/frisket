// @vitest-environment jsdom
//
// The org (team-tier) local model server card renders inside
// OrganizationAiProvidersSettings, backed by GET /api/org/local-endpoints.
// Covers: an empty endpoint collection hides the card, installed models,
// the unenforced auth
// state renders a warning-tone chip with the "check your front door" copy,
// the owner download flow (free-text model input -> confirm -> progress,
// including the pull_busy-shows-active-pull disposition), and the disabled
// note when pull_enabled is false.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi, type Mock } from 'vitest';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    providerCatalog: vi.fn(),
    listOrgKeys: vi.fn(),
    listOrgLocalEndpoints: vi.fn(),
    orgStartArtifactPull: vi.fn(),
    orgListModelPulls: vi.fn(),
    orgGetModelPull: vi.fn(),
    orgCancelModelPull: vi.fn(),
  };
});

import { OrganizationAiProvidersSettings } from '../../src/settings/SettingsSections';
import { ApiError, listOrgLocalEndpoints, listOrgKeys, orgListModelPulls, orgStartArtifactPull, providerCatalog } from '../../src/api/open';
import type { LocalEndpointCatalog, LocalHttpEndpointEntry, ModelPullDto } from '../../src/api/types';



function endpoint(overrides: Partial<LocalHttpEndpointEntry> = {}): LocalHttpEndpointEntry {
  return {
    endpoint_id: 'local-a1b2c3d4e5f6',
    label: 'Organization Ollama',
    kind: 'local_http',
    read_only: true,
    models: [],
    reachable: true,
    origin: 'https://llm.example.internal',
    authority: 'organization',
    source: 'environment',
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

function catalog(endpoints: LocalHttpEndpointEntry[]): LocalEndpointCatalog {
  return { schemaVersion: 'frisket.local_endpoints.v1', endpoints };
}

function models(...names: string[]): LocalHttpEndpointEntry['models'] {
  return names.map((name) => ({
    id: `ollama/@local-a1b2c3d4e5f6/${name}`,
    label: name,
    price: null,
    local: true,
  }));
}

function pull(overrides: Partial<ModelPullDto>): ModelPullDto {
  return {
    schemaVersion: 'frisket.model_pull.v3',
    id: 9,
    model: 'ollama/@local-a1b2c3d4e5f6/qwen3:8b',
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
    endpoint_id: 'local-a1b2c3d4e5f6',
    endpoint_origin: 'https://llm.example.internal',
    initiated_by: '1',
    artifact: null,
    ...overrides,
  };
}

async function renderOrgAiProviders() {
  (providerCatalog as unknown as Mock).mockResolvedValue({ schemaVersion: 'frisket.providers.v1', tier: 'organization', providers: [] });
  (listOrgKeys as unknown as Mock).mockResolvedValue([]);
  (orgListModelPulls as unknown as Mock).mockResolvedValue({ pulls: [] });
  render(<OrganizationAiProvidersSettings />);
  return screen.findByTestId('organization-ai-providers-settings');
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('OrgLocalModelsSettings (organization AI-providers section)', () => {
  it('renders nothing when the operator has not configured a local endpoint', async () => {
    (listOrgLocalEndpoints as unknown as Mock).mockResolvedValue(catalog([]));
    await renderOrgAiProviders();

    await waitFor(() => expect(listOrgLocalEndpoints).toHaveBeenCalled());
    expect(screen.queryByTestId('org-local-models-settings')).not.toBeInTheDocument();
  });

  it('shows origin, status chip, and installed models when configured + reachable', async () => {
    (listOrgLocalEndpoints as unknown as Mock).mockResolvedValue(catalog([endpoint({
      models: models('qwen3:8b', 'llama3:8b'),
      installed_models: ['qwen3:8b', 'llama3:8b'],
    })]));
    await renderOrgAiProviders();

    const card = await screen.findByTestId('org-local-models-settings');
    expect(card).toHaveTextContent('llm.example.internal');
    const chip = screen.getByTestId('org-local-models-status');
    expect(chip).toHaveTextContent(/reachable/i);
    const list = screen.getByTestId('org-local-models-list');
    expect(list).toHaveTextContent('qwen3:8b');
    expect(list).toHaveTextContent('llama3:8b');
  });

  it('renders a warning-tone chip with the front-door copy when auth is unenforced', async () => {
    (listOrgLocalEndpoints as unknown as Mock).mockResolvedValue(catalog([endpoint({
      auth_status: 'unenforced',
      models: [],
      pull_enabled: false,
    })]));
    await renderOrgAiProviders();

    const chip = await screen.findByTestId('org-local-models-status');
    expect(chip).toHaveAttribute('data-tone', 'warning');
    expect(chip).toHaveTextContent(/not enforced/i);
    expect(chip).toHaveTextContent(/front door/i);
  });

  it('shows the quiet disabled note (no Download button) when pull_enabled is false', async () => {
    (listOrgLocalEndpoints as unknown as Mock).mockResolvedValue(catalog([endpoint({
      models: models('qwen3:8b'),
      pull_enabled: false,
    })]));
    await renderOrgAiProviders();

    await screen.findByTestId('org-local-models-settings');
    expect(screen.getByTestId('org-local-models-pull-disabled')).toHaveTextContent(
      /FRISKET_ENABLE_MODEL_PULL/,
    );
    expect(screen.queryByTestId('org-model-pull-start')).not.toBeInTheDocument();
  });

  describe('owner download flow', () => {
    function reachableWithPull(): LocalEndpointCatalog {
      return catalog([endpoint({ models: [], pull_enabled: true })]);
    }

    it('lets the owner type a model name, confirm, and see live progress', async () => {
      (listOrgLocalEndpoints as unknown as Mock).mockResolvedValue(reachableWithPull());
      (orgStartArtifactPull as unknown as Mock).mockResolvedValue({
        pull: pull({}),
        deduplicated: false,
      });
      await renderOrgAiProviders();

      await screen.findByTestId('org-local-models-settings');
      const input = screen.getByTestId('org-model-pull-input');
      const startBtn = screen.getByTestId('org-model-pull-start');
      expect(startBtn).toBeDisabled();

      await userEvent.type(input, 'qwen3:8b');
      expect(startBtn).toBeEnabled();
      await userEvent.click(startBtn);

      // Confirmation gate before the request fires.
      expect(orgStartArtifactPull).not.toHaveBeenCalled();
      await userEvent.click(screen.getByTestId('org-model-pull-confirm'));

      await waitFor(() =>
        expect(orgStartArtifactPull).toHaveBeenCalledWith(
          'ollama/@local-a1b2c3d4e5f6/qwen3:8b',
        ),
      );
      expect(await screen.findByTestId('model-pull-progress')).toBeInTheDocument();
    });

    it('surfaces a 409 pull_busy error by showing the already-active pull instead of a bare error', async () => {
      (listOrgLocalEndpoints as unknown as Mock).mockResolvedValue(reachableWithPull());
      const active = pull({ id: 42, model: 'ollama/@local-a1b2c3d4e5f6/llama3:70b' });
      (orgStartArtifactPull as unknown as Mock).mockRejectedValue(
        new ApiError(409, 'a pull is already in progress', 'pull_busy', { active }),
      );
      await renderOrgAiProviders();

      await screen.findByTestId('org-local-models-settings');
      await userEvent.type(screen.getByTestId('org-model-pull-input'), 'qwen3:8b');
      await userEvent.click(screen.getByTestId('org-model-pull-start'));
      await userEvent.click(screen.getByTestId('org-model-pull-confirm'));

      const progress = await screen.findByTestId('model-pull-progress');
      expect(progress).toHaveTextContent('llama3:70b');
      expect(screen.queryByTestId('org-model-pull-error')).not.toBeInTheDocument();
    });

    it('surfaces a member 403 as the download flow error message rather than hiding the button', async () => {
      (listOrgLocalEndpoints as unknown as Mock).mockResolvedValue(reachableWithPull());
      (orgStartArtifactPull as unknown as Mock).mockRejectedValue(
        new ApiError(403, 'owner permission required', 'forbidden'),
      );
      await renderOrgAiProviders();

      await screen.findByTestId('org-local-models-settings');
      await userEvent.type(screen.getByTestId('org-model-pull-input'), 'qwen3:8b');
      await userEvent.click(screen.getByTestId('org-model-pull-start'));
      await userEvent.click(screen.getByTestId('org-model-pull-confirm'));

      const error = await screen.findByTestId('org-model-pull-error');
      expect(error).toHaveTextContent(/owner permission required/i);
    });
  });
});
