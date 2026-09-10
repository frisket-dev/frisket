// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi, type Mock } from 'vitest';

import { ModelPicker } from '../../src/components/ModelPicker';
import type { LocalEndpointCatalog, LocalHttpEndpointEntry } from '../../src/api/types';
import { installPopoverPolyfill } from '../support/domPolyfills';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    listProviders: vi.fn(),
    listOrgLocalEndpoints: vi.fn(),
  };
});

import { listOrgLocalEndpoints, listProviders } from '../../src/api/open';

beforeAll(() => installPopoverPolyfill());

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function endpoint(
  endpointId: string,
  label: string,
  models: string[],
  overrides: Partial<LocalHttpEndpointEntry> = {},
): LocalHttpEndpointEntry {
  return {
    endpoint_id: endpointId,
    label,
    kind: 'local_http',
    read_only: true,
    models: models.map((name) => ({
      id: `ollama/@${endpointId}/${name}`,
      label: name,
      price: null,
      local: true,
    })),
    reachable: true,
    origin: `https://${endpointId}.example.internal`,
    authority: 'organization',
    source: 'environment',
    detail: null,
    installed_models: models,
    protocol: 'ollama_native',
    auth_status: 'ok',
    token_configured: false,
    provisioning_token_configured: false,
    edge_auth: false,
    pull_enabled: true,
    ...overrides,
  };
}

function orgCatalog(endpoints: LocalHttpEndpointEntry[]): LocalEndpointCatalog {
  return { schemaVersion: 'frisket.local_endpoints.v1', endpoints };
}

async function renderHostedPicker(onChange = vi.fn()) {
  (listProviders as unknown as Mock).mockRejectedValue(
    Object.assign(new Error('not found'), { status: 404 }),
  );
  render(
    <>
      <span id="test-model-label">Model</span>
      <ModelPicker
        value="anthropic/claude-haiku-4-5"
        onChange={onChange}
        hosted
        ariaLabelledBy="test-model-label"
      />
    </>,
  );
  const button = await screen.findByTestId('model-picker-button');
  await waitFor(() => expect(listOrgLocalEndpoints).toHaveBeenCalled());
  await userEvent.click(button);
  await screen.findByTestId('model-picker-menu');
  return { button, onChange };
}

describe('ModelPicker (hosted) + organization local endpoints', () => {
  it('merges every endpoint as its own stable provider group', async () => {
    (listOrgLocalEndpoints as unknown as Mock).mockResolvedValue(orgCatalog([
      endpoint('local-a1b2c3d4e5f6', 'Reporting laptop', ['qwen3:8b']),
      endpoint('local-112233445566', 'GPU workstation', ['qwen3:8b', 'llama3:8b']),
    ]));

    await renderHostedPicker();

    expect(screen.getByTestId('model-provider-group-local-a1b2c3d4e5f6'))
      .toHaveTextContent('Reporting laptop');
    expect(screen.getByTestId('model-provider-group-local-112233445566'))
      .toHaveTextContent('GPU workstation');
  });

  it('selects the endpoint-qualified model id, preserving exact routing', async () => {
    (listOrgLocalEndpoints as unknown as Mock).mockResolvedValue(orgCatalog([
      endpoint('local-a1b2c3d4e5f6', 'Reporting laptop', ['qwen3:8b']),
    ]));
    const onChange = vi.fn();
    await renderHostedPicker(onChange);

    const group = screen.getByTestId('model-provider-group-local-a1b2c3d4e5f6');
    await userEvent.hover(group);
    await userEvent.click(
      await screen.findByTestId('model-option-ollama-local-a1b2c3d4e5f6-qwen3-8b'),
    );

    expect(onChange).toHaveBeenCalledWith('ollama/@local-a1b2c3d4e5f6/qwen3:8b');
  });

  it('adds no local provider group for an empty plural catalog', async () => {
    (listOrgLocalEndpoints as unknown as Mock).mockResolvedValue(orgCatalog([]));
    await renderHostedPicker();

    expect(screen.queryByTestId(/model-provider-group-local-/)).not.toBeInTheDocument();
  });

  it('keeps an offline endpoint attributed and visibly marked offline', async () => {
    (listOrgLocalEndpoints as unknown as Mock).mockResolvedValue(orgCatalog([
      endpoint('local-a1b2c3d4e5f6', 'Offline workstation', ['qwen3:8b'], {
        reachable: false,
      }),
    ]));
    await renderHostedPicker();

    const group = screen.getByTestId('model-provider-group-local-a1b2c3d4e5f6');
    expect(group).toBeInTheDocument();
    expect(screen.getByTestId('local-server-reachability-local-a1b2c3d4e5f6'))
      .toHaveTextContent('offline');
  });
});
