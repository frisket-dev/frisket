// @vitest-environment jsdom
//
// The AI Providers table is where the false-zero defect was caught in the
// live proof: the SPENT column read $0.00 against $0.001755 of real spend on
// the user's own OpenAI key, and a $0.0001 cap read "$0.00", which a reader
// takes for no cap at all. Both went through a two-decimal formatter.
//
// These are the three cells that must never look alike: a sub-cent amount, a
// real zero, and an amount that was never set.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type {
  ProjectInfo,
  ProjectProviderKeyInfo,
  ProjectProviderKeys,
} from '../../src/api/types';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getProjectProviderKeys: vi.fn(),
      validateProjectProviderKey: vi.fn(),
      setProjectProviderKey: vi.fn(),
      deleteProjectProviderKey: vi.fn(),
    },
  };
});

import { ProjectAiProvidersSettings } from '../../src/settings/SettingsSections';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';


const api = createProjectApi('test-project');


const getProjectProviderKeys = vi.spyOn(api, 'getProjectProviderKeys');
const mockApi = { getProjectProviderKeys };
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project', api: { projectApi: api },
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function providerRow(
  id: string,
  overrides: Partial<ProjectProviderKeyInfo> = {},
): ProjectProviderKeyInfo {
  return {
    id,
    label: id,
    secret_name: `${id.toUpperCase()}_API_KEY`,
    kind: 'api',
    policy_fields: ['spend_cap_usd'],
    configured: true,
    hint: '...efsA',
    spend_cap_usd: null,
    spent_usd: 0,
    unmetered_calls: 0,
    updated_at: null,
    ...overrides,
  };
}

function page(providers: ProjectProviderKeyInfo[]): ProjectProviderKeys {
  return {
    schemaVersion: 'frisket.project_provider_keys.v1',
    projectId: 'p1',
    providers,
  };
}

const OWNER = { id: 'p1', name: 'P', role: 'owner' } as unknown as ProjectInfo;

describe('AI Providers money cells', () => {
  it('shows sub-cent spend and caps instead of a false $0.00', async () => {
    mockApi.getProjectProviderKeys.mockResolvedValue(
      page([
        providerRow('openai', { spend_cap_usd: 0.0001, spent_usd: 0.001755 }),
      ]),
    );
    render(<ProjectAiProvidersSettings project={OWNER} />);

    await waitFor(() => expect(screen.getByText('$0.0018')).toBeInTheDocument());
    expect(screen.getByText('$0.0001')).toBeInTheDocument();
    expect(screen.queryByText('$0.00')).not.toBeInTheDocument();
  });

  it('keeps "no cap set" visually distinct from a cap of a hundredth of a cent', async () => {
    mockApi.getProjectProviderKeys.mockResolvedValue(
      page([
        providerRow('openai', { spend_cap_usd: 0.0001, spent_usd: 0 }),
        providerRow('gemini', { spend_cap_usd: null, spent_usd: 0 }),
      ]),
    );
    render(<ProjectAiProvidersSettings project={OWNER} />);

    // A tiny cap reads as money; an absent one reads as an em dash. Both rows
    // spent a real zero, which is the one case $0.00 is reserved for.
    await waitFor(() => expect(screen.getByText('$0.0001')).toBeInTheDocument());
    expect(screen.getAllByText('$0.00')).toHaveLength(2);
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });
});
