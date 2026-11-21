// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  providerCatalog: vi.fn(),
  listOrganizationKeys: vi.fn(),
  validateOrganizationKey: vi.fn(),
  setOrganizationKey: vi.fn(),
  deleteOrganizationKey: vi.fn(),
  listOrganizationLocalEndpoints: vi.fn(),
  listOrganizationEnvVars: vi.fn(),
  setOrganizationEnvVar: vi.fn(),
  deleteOrganizationEnvVar: vi.fn(),
  listApiKeys: vi.fn(),
  createApiKey: vi.fn(),
  revokeApiKey: vi.fn(),
}));

vi.mock('../../src/api/open', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../src/api/open')>(),
  ...api,
}));

import {
  OrganizationAiProvidersSettings,
  OrganizationApiKeysSettings,
  OrganizationSecretsSettings,
} from '../../src/settings/OrganizationSettings';
import type { ApiTokenInfo, OrganizationEnvInfo, OrganizationKeyInfo } from '../../src/api/types';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('OrganizationSettings', () => {
  it('validates before saving an organization provider key and never renders the raw key', async () => {
    let keys: OrganizationKeyInfo[] = [
      { provider: 'anthropic', hint: '...old1' },
    ];
    api.providerCatalog.mockResolvedValue({
      schemaVersion: 'frisket.provider_catalog.v1',
      providers: [
        { id: 'anthropic', label: 'Anthropic', secret_name: 'ANTHROPIC_API_KEY', kind: 'llm', policy_fields: ['spend_cap_usd'] },
        { id: 'openai', label: 'OpenAI', secret_name: 'OPENAI_API_KEY', kind: 'llm', policy_fields: ['spend_cap_usd'] },
      ],
    });
    api.listOrganizationLocalEndpoints.mockResolvedValue({
      schemaVersion: 'frisket.local_endpoints.v1',
      endpoints: [],
    });
    api.listOrganizationKeys.mockImplementation(async () => [...keys]);
    api.validateOrganizationKey.mockResolvedValue({
      ok: true,
      provider: 'anthropic',
      reachable: true,
      status: 200,
      validation_token: 'tok-org',
    });
    api.setOrganizationKey.mockImplementation(async (provider: string) => {
      keys = [{ provider, hint: '...cret' }];
      return { ok: true, provider };
    });

    const user = userEvent.setup();
    render(<OrganizationAiProvidersSettings />);

    const section = await screen.findByTestId('organization-ai-providers-settings');
    await waitFor(() => expect(section).toHaveTextContent('anthropic'));

    await user.click(screen.getByRole('button', { name: 'Add new key' }));
    await user.type(screen.getByLabelText('Provider key'), 'sk-new-secret');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();

    await user.click(screen.getByRole('button', { name: 'Test' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled());
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(api.setOrganizationKey).toHaveBeenCalledWith(
      'anthropic',
      'sk-new-secret',
      'tok-org',
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    ));
    await waitFor(() => expect(section).toHaveTextContent('...cret'));
    expect(screen.queryByText('sk-new-secret')).not.toBeInTheDocument();
  });

  it('saves organization secrets with uppercased names and without exposing raw values', async () => {
    let envVars: OrganizationEnvInfo[] = [
      { name: 'EXA_API_KEY', hint: '...demo', created_at: '2026-06-13T00:00:00Z' },
    ];
    api.listOrganizationEnvVars.mockImplementation(async () => [...envVars]);
    api.setOrganizationEnvVar.mockImplementation(async (name: string) => {
      const upper = name.toUpperCase();
      envVars = [...envVars, { name: upper, hint: '...cret', created_at: '2026-06-13T00:00:00Z' }];
      return { ok: true, name: upper };
    });

    const user = userEvent.setup();
    render(<OrganizationSecretsSettings />);

    await screen.findByText('EXA_API_KEY');
    await user.type(screen.getByLabelText('Secret name'), 'serp_api_key');
    await user.type(screen.getByLabelText('Secret value'), 'env-secret');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(screen.getByTestId('organization-secrets-settings')).toHaveTextContent('SERP_API_KEY'));
    expect(screen.queryByText('env-secret')).not.toBeInTheDocument();
  });

  it('reveals newly created organization API keys once and revokes existing tokens', async () => {
    let tokens: ApiTokenInfo[] = [
      {
        id: 1,
        user_id: 1,
        created_by: 'owner@example.org',
        name: 'existing token',
        prefix: 'frisket_pat_old',
        created_at: '2026-07-01T12:00:00Z',
        last_used_at: null,
        revoked: false,
      },
    ];
    api.listApiKeys.mockImplementation(async () => tokens.map((item) => ({ ...item })));
    api.createApiKey.mockImplementation(async (name: string) => {
      tokens = [
        ...tokens,
        {
          id: 2,
          user_id: 1,
          created_by: 'owner@example.org',
          name,
          prefix: 'frisket_pat_new',
          created_at: '2026-07-01T12:10:00Z',
          last_used_at: null,
          revoked: false,
        },
      ];
      return { id: 2, name, prefix: 'frisket_pat_new', token: 'frisket_pat_new_secret' };
    });
    api.revokeApiKey.mockImplementation(async (id: number) => {
      tokens = tokens.map((item) => (item.id === id ? { ...item, revoked: true } : item));
      return { ok: true, revoked: true };
    });

    const user = userEvent.setup();
    render(<OrganizationApiKeysSettings />);

    const section = await screen.findByTestId('organization-api-keys-settings');
    await waitFor(() => expect(section).toHaveTextContent('existing token'));

    await user.type(screen.getByLabelText('Token name'), 'automation token');
    await user.click(screen.getByRole('button', { name: 'Create' }));

    expect(await screen.findByTestId('api-key-created')).toHaveTextContent('frisket_pat_new_secret');
    expect(section).toHaveTextContent('automation token');

    await user.click(screen.getAllByRole('button', { name: 'Revoke' })[0]);
    await waitFor(() => expect(section).toHaveTextContent('Revoked'));
    expect(api.revokeApiKey).toHaveBeenCalledWith(1);
  });
});
