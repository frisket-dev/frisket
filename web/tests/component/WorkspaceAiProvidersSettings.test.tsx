// @vitest-environment jsdom
//
// Workspace AI-providers settings page (settings-workspace-ai-providers-v1):
// the workspace provider config moved from the ModelPicker's inline pane into
// /settings/personal/ai-providers, deliberately sharing the project-overrides
// page's interface (add-key validate-before-save flow + SettingsTable rows)
// plus canonical local-endpoint management.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor, render, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi, type Mock } from 'vitest';

import { WorkspaceAiProvidersSettings } from '../../src/settings/SettingsSections';
import type {
  LocalProviderCatalog,
  LocalProviderEntry,
  LocalHttpEndpointEntry,
  LocalProviderModel,
  ProviderValidateResult,
} from '../../src/api/types';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    listProviders: vi.fn(),
    providerStatus: vi.fn(),
    setProviderKey: vi.fn(),
    deleteProviderKey: vi.fn(),
    validateProviderKey: vi.fn(),
    createLocalEndpoint: vi.fn(),
    discoverLocalEndpoints: vi.fn(),
    updateLocalEndpoint: vi.fn(),
    deleteLocalEndpoint: vi.fn(),
  };
});

import { createLocalEndpoint, discoverLocalEndpoints, listProviders, providerStatus, setProviderKey, validateProviderKey } from '../../src/api/open';



afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function model(id: string, label: string): LocalProviderModel {
  return { id, label, price: null };
}

function providerEntry(overrides: Partial<LocalProviderEntry> & Pick<LocalProviderEntry, 'id' | 'label' | 'kind' | 'models'>): LocalProviderEntry {
  return { configured: false, source: null, hint: null, ...overrides };
}

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
    pull_enabled: false,
    ...overrides,
  };
}

function catalog(providers: LocalProviderEntry[]): LocalProviderCatalog {
  return { schemaVersion: 'frisket.providers.v1', tier: 'local', providers };
}

describe('WorkspaceAiProvidersSettings', () => {
  it('offers one-click Ollama and LM Studio localhost defaults', async () => {
    const ollama = localEndpoint({
      endpoint_id: 'local-ollama111111',
      label: 'Ollama',
      origin: 'http://localhost:11434',
    });
    const studio = localEndpoint({
      endpoint_id: 'local-studio111111',
      label: 'LM Studio',
      origin: 'http://localhost:1234',
    });
    (listProviders as unknown as Mock)
      .mockResolvedValueOnce(catalog([]))
      .mockResolvedValueOnce(catalog([ollama]))
      .mockResolvedValue(catalog([ollama, studio]));
    (createLocalEndpoint as unknown as Mock).mockResolvedValue(catalog([]));

    render(<WorkspaceAiProvidersSettings />);

    await userEvent.click(await screen.findByRole('button', { name: 'Add Ollama' }));
    await waitFor(() => expect(createLocalEndpoint).toHaveBeenLastCalledWith({
      display_name: 'Ollama',
      origin: 'http://localhost:11434',
    }));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Add Ollama' })).not.toBeInTheDocument());

    await userEvent.click(screen.getByRole('button', { name: 'Add LM Studio' }));
    await waitFor(() => expect(createLocalEndpoint).toHaveBeenLastCalledWith({
      display_name: 'LM Studio',
      origin: 'http://localhost:1234',
    }));
    await waitFor(() => expect(screen.queryByTestId('local-endpoint-suggestions')).not.toBeInTheDocument());
  });

  it('discovers fixed local servers explicitly and summarizes every outcome', async () => {
    const studio = localEndpoint({
      endpoint_id: 'local-studio111111',
      label: 'LM Studio',
      origin: 'http://localhost:1234',
    });
    (listProviders as unknown as Mock)
      .mockResolvedValueOnce(catalog([]))
      .mockResolvedValue(catalog([studio]));
    (discoverLocalEndpoints as unknown as Mock).mockResolvedValue({
      candidates: [
        { label: 'Ollama', origin: 'http://localhost:11434', outcome: 'not_found' },
        { label: 'LM Studio', origin: 'http://localhost:1234', outcome: 'added' },
        { label: 'llama.cpp', origin: 'http://localhost:8080', outcome: 'already_added' },
      ],
    });

    render(<WorkspaceAiProvidersSettings />);
    await userEvent.click(await screen.findByRole('button', { name: 'Find local servers' }));

    await waitFor(() => expect(discoverLocalEndpoints).toHaveBeenCalledTimes(1));
    const summary = await screen.findByTestId('local-endpoint-discovery-summary');
    expect(summary).toHaveTextContent('Nothing found at http://localhost:11434 (Ollama).');
    expect(summary).toHaveTextContent('Found LM Studio at http://localhost:1234 — added.');
    expect(summary).toHaveTextContent('llama.cpp at http://localhost:8080 — already added.');
  });

  it('surfaces discovery failure without an automatic retry', async () => {
    (listProviders as unknown as Mock).mockResolvedValue(catalog([]));
    (discoverLocalEndpoints as unknown as Mock).mockRejectedValue(new Error('Discovery failed'));

    render(<WorkspaceAiProvidersSettings />);
    await userEvent.click(await screen.findByRole('button', { name: 'Find local servers' }));

    expect(await screen.findByText('Discovery failed')).toBeInTheDocument();
    expect(discoverLocalEndpoints).toHaveBeenCalledTimes(1);
  });

  it('never exposes local discovery controls for a hostile hosted catalog', async () => {
    (listProviders as unknown as Mock).mockResolvedValue({
      schemaVersion: 'frisket.providers.v1',
      tier: 'organization',
      providers: [],
    });

    render(<WorkspaceAiProvidersSettings />);
    await waitFor(() => expect(listProviders).toHaveBeenCalledTimes(1));

    expect(screen.queryByTestId('local-endpoints')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Find local servers' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add Ollama' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add LM Studio' })).not.toBeInTheDocument();
    expect(discoverLocalEndpoints).not.toHaveBeenCalled();
    expect(createLocalEndpoint).not.toHaveBeenCalled();
  });

  it('adds and attributes a second local model server', async () => {
    const firstEndpoint = localEndpoint({
      endpoint_id: 'local-111111111111',
      label: 'Local server',
      origin: 'http://localhost:11434',
      models: [],
    });
    const extra = localEndpoint({
      endpoint_id: 'local-a1b2c3d4e5f6',
      label: 'LM Studio — gaming PC',
      origin: 'http://localhost:1234',
      models: [model('ollama/@local-a1b2c3d4e5f6/qwen', 'qwen')],
    });
    (listProviders as unknown as Mock)
      .mockResolvedValueOnce(catalog([firstEndpoint]))
      .mockResolvedValue(catalog([firstEndpoint, extra]));
    (createLocalEndpoint as unknown as Mock).mockResolvedValue(catalog([firstEndpoint, extra]));

    render(<WorkspaceAiProvidersSettings />);
    await userEvent.click(await screen.findByRole('button', { name: 'Add local server' }));
    await userEvent.type(screen.getByLabelText('Local server name'), 'LM Studio — gaming PC');
    await userEvent.type(screen.getByLabelText('Local server URL'), 'http://localhost:1234');
    await userEvent.click(screen.getByRole('button', { name: 'Add server' }));

    await waitFor(() => expect(createLocalEndpoint).toHaveBeenCalledWith(
      { display_name: 'LM Studio — gaming PC', origin: 'http://localhost:1234' },
    ));
    const row = await screen.findByTestId('local-endpoint-local-a1b2c3d4e5f6');
    expect(within(row).getByText('LM Studio — gaming PC')).toBeInTheDocument();
    expect(within(row).getByText('reachable')).toBeInTheDocument();

    (providerStatus as unknown as Mock).mockResolvedValue(extra);
    await userEvent.click(within(row).getAllByRole('button', { name: 'Recheck' })[0]);
    await waitFor(() =>
      expect(providerStatus).toHaveBeenCalledWith('local-a1b2c3d4e5f6'),
    );
  });

  it('add key: Save stays disabled until Test succeeds, and the raw key is never rendered back', async () => {
    const KEY = 'sk-e2e-openai-key-9999';
    const start = catalog([
      providerEntry({ id: 'openai', label: 'OpenAI', kind: 'platform_api', models: [] }),
    ]);
    const updated = catalog([
      providerEntry({
        id: 'openai',
        label: 'OpenAI',
        kind: 'platform_api',
        configured: true,
        hint: '...9999',
        source: 'local_file',
        models: [],
      }),
    ]);
    (listProviders as unknown as Mock).mockResolvedValue(start);
    const validateResult: ProviderValidateResult = {
      ok: true,
      reachable: true,
      status: 200,
      validation_token: 'e2e-token',
    };
    (validateProviderKey as unknown as Mock).mockResolvedValue(validateResult);
    (setProviderKey as unknown as Mock).mockResolvedValue(updated);

    render(<WorkspaceAiProvidersSettings />);
    await userEvent.click(await screen.findByRole('button', { name: /add new key/i }));
    await userEvent.selectOptions(screen.getByLabelText('Provider'), 'openai');
    await userEvent.type(screen.getByLabelText('Provider key'), KEY);

    const saveButton = screen.getByRole('button', { name: 'Save', exact: true });
    expect(saveButton).toBeDisabled();

    await userEvent.click(screen.getByRole('button', { name: 'Test', exact: true }));
    await waitFor(() => expect(validateProviderKey).toHaveBeenCalledWith('openai', KEY));
    const message = await screen.findByTestId('provider-validation-message');
    expect(message).toHaveTextContent(/valid/i);
    expect(saveButton).toBeEnabled();

    await userEvent.click(saveButton);
    await waitFor(() => expect(setProviderKey).toHaveBeenCalledWith('openai', KEY, 'e2e-token'));

    const status = await screen.findByTestId('provider-status-openai');
    expect(status).toHaveTextContent(/configured|\.\.\.9999/i);
    expect(status).not.toHaveTextContent(KEY);
  });

  // The key is not a browser-password-manager credential — the add-key form
  // must not trigger saved username/password autofill, and pasting a key
  // must keep working despite that.
  it('add key: the form and key input opt out of password-manager autofill without blocking paste', async () => {
    (listProviders as unknown as Mock).mockResolvedValue(
      catalog([providerEntry({ id: 'openai', label: 'OpenAI', kind: 'platform_api', models: [] })]),
    );

    render(<WorkspaceAiProvidersSettings />);
    await userEvent.click(await screen.findByRole('button', { name: /add new key/i }));

    const keyInput = screen.getByLabelText('Provider key') as HTMLInputElement;
    expect(keyInput.closest('form')).toHaveAttribute('autocomplete', 'off');
    expect(keyInput).toHaveAttribute('autocomplete', 'new-password');
    expect(keyInput).not.toHaveAttribute('name', expect.stringMatching(/password/i));
    expect(keyInput).not.toHaveAttribute('id', expect.stringMatching(/password/i));

    await userEvent.click(keyInput);
    await userEvent.paste('sk-pasted-key-1234');
    expect(keyInput.value).toBe('sk-pasted-key-1234');
  });

  it('an env-owned endpoint is read-only and says where it comes from', async () => {
    (listProviders as unknown as Mock).mockResolvedValue(
      catalog([
        localEndpoint({
          endpoint_id: 'local-env123456789',
          label: 'Ollama',
          read_only: true,
          reachable: false,
          origin: 'http://env-winner:11434',
          source: 'environment',
          models: [],
        }),
      ]),
    );

    render(<WorkspaceAiProvidersSettings />);

    const row = await screen.findByTestId('local-endpoint-local-env123456789');
    expect(within(row).getByDisplayValue('Ollama')).toBeDisabled();
    expect(within(row).getByDisplayValue('http://env-winner:11434')).toBeDisabled();
    expect(within(row).getByText(/managed by the server environment/i)).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: /delete/i })).not.toBeInTheDocument();
  });

  it('an unauthorized endpoint exposes an authentication-specific probe summary', async () => {
    // A 401/403 from the local server (e.g.
    // behind a bearer-checking front door) is an authentication state — the
    // chip must not claim plain "reachable" while listing zero models.
    (listProviders as unknown as Mock).mockResolvedValue(
      catalog([
        localEndpoint({
          label: 'Ollama',
          reachable: true,
          auth_status: 'unauthorized',
          detail: 'server requires authentication (HTTP 401)',
          origin: 'http://heavy-box:8443',
          source: 'environment',
          read_only: true,
          models: [],
        }),
      ]),
    );

    render(<WorkspaceAiProvidersSettings />);

    expect(await screen.findByTestId('local-endpoint-probe-summary-local-a1b2c3d4e5f6'))
      .toHaveTextContent(/rejected authentication/i);
  });

  it('ollama unreachable: the chip says "not running" and the errno stays one disclosure away', async () => {
    // 2026-07-26 hand-use pass: the chip rendered `detail` verbatim, and
    // .settings-meta-label uppercases, so a stopped server shouted
    // "OLLAMA [ERRNO 111] CONNECTION REFUSED" beside the heading.
    (listProviders as unknown as Mock).mockResolvedValue(
      catalog([
        localEndpoint({
          label: 'Ollama',
          reachable: false,
          detail: '[Errno 111] Connection refused',
          origin: 'http://localhost:11434',
          models: [],
        }),
      ]),
    );

    render(<WorkspaceAiProvidersSettings />);

    const badge = await screen.findByTestId('local-endpoint-status-local-a1b2c3d4e5f6');
    expect(badge).toHaveTextContent('not running');
    expect(badge).not.toHaveTextContent(/errno/i);

    // Humanized, and it names the URL that was probed.
    expect(screen.getByTestId('local-endpoint-probe-summary-local-a1b2c3d4e5f6')).toHaveTextContent(
      'Ollama is not running at http://localhost:11434',
    );
    // The transcript is still reachable — a refused connection reads
    // differently from a DNS failure.
    expect(screen.getByTestId('local-endpoint-probe-detail-text-local-a1b2c3d4e5f6')).toHaveTextContent(
      '[Errno 111] Connection refused',
    );
  });

  it('ollama unreachable: settings shows full install guidance with platform tabs', async () => {
    // Guidance targets the machine that will
    // run the model server, offered as selectable tabs — never sniffed from
    // the browser.
    (listProviders as unknown as Mock).mockResolvedValue(
      catalog([
        localEndpoint({
          label: 'Ollama',
          reachable: false,
          detail: 'not running',
          origin: 'http://localhost:11434',
          models: [],
        }),
      ]),
    );

    render(<WorkspaceAiProvidersSettings />);

    const guidance = await screen.findByTestId('local-server-guidance');
    expect(guidance).toBeInTheDocument();
    expect(screen.getByTestId('guidance-tab-macos')).toBeInTheDocument();
    expect(screen.getByTestId('guidance-tab-linux')).toBeInTheDocument();
    expect(screen.getByTestId('guidance-tab-windows')).toBeInTheDocument();
    expect(screen.getByTestId('guidance-recheck')).toBeInTheDocument();
  });

  it('rechecks only Ollama and reactively replaces its row without reloading the catalog', async () => {
    const unreachable = localEndpoint({
      label: 'Ollama', models: [],
      reachable: false, origin: 'http://localhost:11434',
    });
    const reachable = localEndpoint({
      ...unreachable, reachable: true, protocol: 'ollama_native', auth_status: 'ok',
      installed_models: ['qwen3:8b'], models: [model('ollama/@local-a1b2c3d4e5f6/qwen3:8b', 'qwen3:8b')],
    });
    (listProviders as unknown as Mock).mockResolvedValue(catalog([unreachable]));
    (providerStatus as unknown as Mock).mockResolvedValue(reachable);

    render(<WorkspaceAiProvidersSettings />);
    const button = await screen.findByTestId('guidance-recheck');
    await userEvent.click(button);

    await waitFor(() => expect(providerStatus).toHaveBeenCalledWith('local-a1b2c3d4e5f6'));
    expect(listProviders).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getByTestId('local-endpoint-status-local-a1b2c3d4e5f6')).toHaveTextContent('reachable'));
  });

  it('an endpoint with unenforced auth keeps the endpoint row and guidance visible', async () => {
    // A configured token plus a successful tokenless probe is a
    // misconfiguration; the workspace chip must surface it, not
    // render a healthy "reachable".
    (listProviders as unknown as Mock).mockResolvedValue(
      catalog([
        localEndpoint({
          label: 'Ollama',
          reachable: true,
          auth_status: 'unenforced',
          origin: 'https://llm.heavy.internal',
          source: 'environment',
          read_only: true,
          models: [],
        }),
      ]),
    );

    render(<WorkspaceAiProvidersSettings />);

    expect(await screen.findByTestId('local-endpoint-local-a1b2c3d4e5f6')).toBeInTheDocument();
    expect(screen.getByTestId('local-endpoint-guidance-local-a1b2c3d4e5f6')).toBeInTheDocument();
  });

  it('an env-sourced key row shows Environment as its source and offers no Delete', async () => {
    (listProviders as unknown as Mock).mockResolvedValue(
      catalog([
        providerEntry({
          id: 'anthropic',
          label: 'Anthropic',
          kind: 'platform_api',
          configured: true,
          hint: '...4242',
          source: 'env',
          models: [],
        }),
      ]),
    );

    render(<WorkspaceAiProvidersSettings />);

    const status = await screen.findByTestId('provider-status-anthropic');
    expect(status).toHaveTextContent(/configured/i);
    expect(screen.getByText('Environment')).toBeInTheDocument();
    expect(screen.getByTestId('provider-key-test-anthropic')).toBeEnabled();
    expect(screen.queryByTestId('provider-key-delete-anthropic')).not.toBeInTheDocument();
  });
});
